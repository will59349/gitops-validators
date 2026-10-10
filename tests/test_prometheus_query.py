"""Tests for --prometheus-url against a threaded fake Prometheus."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from conftest import ALERT_GOOD
from gitops_validators.alert_rules import ExprRef
from gitops_validators.cli import main_alert_rules
from gitops_validators.common import LEVEL_ERROR, LEVEL_WARNING, UsageError
from gitops_validators.promquery import query_expressions, substitute_macros

REJECTED = {
    "this is not promql": 'parse error at char 6: unexpected identifier "is"',
    "sum(rate(http_requests_total[5m])": "parse error: unclosed left parenthesis",
}
SERVER_ERROR = "boom"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        query = parse_qs(body).get("query", [""])[0]
        self.server.requests.append(
            {"path": self.path, "query": query, "auth": self.headers.get("Authorization")}
        )
        if self.path != "/api/v1/query":
            self._send(404, {"status": "error", "error": "not found"})
        elif query in REJECTED:
            self._send(400, {"status": "error", "errorType": "bad_data", "error": REJECTED[query]})
        elif query == SERVER_ERROR:
            self._send(500, {"status": "error", "errorType": "internal", "error": "boom"})
        else:
            self._send(200, {"status": "success", "data": {"resultType": "vector", "result": []}})

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence the test output
        pass


@pytest.fixture
def prometheus():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()


def ref(expr, ident="rule", ds="prometheus-main", path="rules.yaml"):
    return ExprRef(path, ident, expr, ds)


def test_rejected_expression_is_a_finding(prometheus):
    url, _ = prometheus
    findings = query_expressions([ref("this is not promql", ident="bad-rule")], url)
    assert len(findings) == 1
    f = findings[0]
    assert f.check == "AQ001"
    assert f.ident == "bad-rule"
    assert f.level == LEVEL_ERROR
    assert "expression rejected by Prometheus" in f.message
    assert "unexpected identifier" in f.message


def test_accepted_expression_is_clean(prometheus):
    url, server = prometheus
    assert query_expressions([ref("up == 0")], url) == []
    assert server.requests[0]["path"] == "/api/v1/query"
    assert server.requests[0]["query"] == "up == 0"


def test_grafana_macros_are_substituted_before_sending(prometheus):
    url, server = prometheus
    query_expressions([ref("rate(x[$__rate_interval]) / ${__interval_ms}")], url)
    assert server.requests[0]["query"] == "rate(x[5m]) / 60000"


def test_expressions_with_template_variables_are_skipped(prometheus):
    url, server = prometheus
    assert query_expressions([ref('up{job="$job"}'), ref('up{job=~"${job:regex}"}')], url) == []
    assert server.requests == []


def test_identical_expressions_are_queried_once(prometheus):
    url, server = prometheus
    findings = query_expressions(
        [ref("this is not promql", ident="a"), ref("this is not promql", ident="b")], url
    )
    assert [f.ident for f in findings] == ["a", "b"]
    assert len(server.requests) == 1


def test_query_datasource_filter(prometheus):
    url, server = prometheus
    findings = query_expressions(
        [
            ref("this is not promql", ident="loki-rule", ds="loki-main"),
            ref("this is not promql", ident="prom-rule", ds="prometheus-main"),
            ref("this is not promql", ident="operator-rule", ds=None),
        ],
        url,
        query_datasource_uids=frozenset({"prometheus-main"}),
    )
    assert [f.ident for f in findings] == ["prom-rule", "operator-rule"]
    assert len(server.requests) == 1  # identical expression, cached


def test_bearer_token_is_read_from_environment(prometheus, monkeypatch):
    url, server = prometheus
    monkeypatch.setenv("PROM_TOKEN", "secret-token-value")
    query_expressions([ref("up")], url, bearer_token_env="PROM_TOKEN")
    assert server.requests[0]["auth"] == "Bearer secret-token-value"


def test_missing_token_variable_is_a_usage_error(prometheus, monkeypatch):
    url, _ = prometheus
    monkeypatch.delenv("PROM_TOKEN", raising=False)
    with pytest.raises(UsageError, match="PROM_TOKEN"):
        query_expressions([ref("up")], url, bearer_token_env="PROM_TOKEN")


def test_server_error_is_warning_unless_strict(prometheus):
    url, _ = prometheus
    findings = query_expressions([ref(SERVER_ERROR)], url)
    assert [f.check for f in findings] == ["AQ002"]
    assert findings[0].level == LEVEL_WARNING
    strict = query_expressions([ref(SERVER_ERROR)], url, strict=True)
    assert strict[0].level == LEVEL_ERROR


def test_unreachable_prometheus_is_warning_unless_strict():
    url = "http://127.0.0.1:1"  # nothing listens on port 1
    findings = query_expressions([ref("up"), ref("up == 0")], url, timeout=2)
    assert len(findings) == 1
    assert findings[0].check == "AQ002"
    assert findings[0].level == LEVEL_WARNING
    strict = query_expressions([ref("up")], url, timeout=2, strict=True)
    assert strict[0].level == LEVEL_ERROR


def test_url_credentials_do_not_reach_findings():
    url = "http://scraper:hunter2@127.0.0.1:1"
    findings = query_expressions([ref("up")], url, timeout=2)
    assert findings[0].check == "AQ002"
    assert findings[0].path == "http://127.0.0.1:1"
    assert "hunter2" not in findings[0].format_text()


def test_substitute_macros_handles_braced_and_bare_forms():
    assert substitute_macros("a[$__rate_interval] b[${__rate_interval}] $__range") == (
        "a[5m] b[5m] 1h"
    )


# --------------------------------------------------------------------------
# Through the CLI
# --------------------------------------------------------------------------


def _rules_file(tmp_path, expr: str):
    text = (ALERT_GOOD / "rules.yaml").read_text()
    p = tmp_path / "rules.yaml"
    p.write_text(text.replace('expr: up{job="node-exporter"} == 0', f"expr: {expr}"))
    return p


def test_cli_reports_rejected_expression(prometheus, tmp_path, capsys):
    url, _ = prometheus
    p = _rules_file(tmp_path, "sum(rate(http_requests_total[5m])")
    code = main_alert_rules([str(p), "--prometheus-url", url])
    out = capsys.readouterr().out
    assert code == 1
    assert "[node-down] AQ001: expression rejected by Prometheus" in out
    assert "unclosed left parenthesis" in out


def test_cli_clean_when_prometheus_accepts_everything(prometheus, tmp_path, capsys):
    url, server = prometheus
    p = _rules_file(tmp_path, "up == 0")
    code = main_alert_rules([str(p), "--prometheus-url", url])
    assert code == 0
    assert capsys.readouterr().out == ""
    # One rule in rules.yaml uses $__rate_interval (substituted), none use dashboard variables.
    assert len(server.requests) == 3


def test_cli_unreachable_prometheus_exit_code(tmp_path, capsys):
    p = _rules_file(tmp_path, "up == 0")
    args = [str(p), "--prometheus-url", "http://127.0.0.1:1", "--query-timeout", "2"]
    assert main_alert_rules(args) == 0
    assert "AQ002 (warning)" in capsys.readouterr().out
    assert main_alert_rules([*args, "--strict-query"]) == 1
