"""Optional semantic check: ask a live Prometheus whether each PromQL expression parses.

Each expression is sent to ``POST /api/v1/query`` with ``time=now``. Prometheus
answers 400 (``bad_data``) for expressions it cannot parse and 422 for
expressions that fail during evaluation (for example many-to-many matching).
Both mean the rule can never fire. Anything else (connection refused, 5xx,
authentication) is reported as a warning unless ``strict`` is set.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

import requests

from .alert_rules import ExprRef
from .common import LEVEL_ERROR, LEVEL_WARNING, Finding, UsageError

# Grafana macros that Grafana itself interpolates before the query reaches
# Prometheus. They are replaced with plausible constants so the expression parses.
GRAFANA_MACROS: dict[str, str] = {
    "${__rate_interval}": "5m",
    "$__rate_interval": "5m",
    "${__interval_ms}": "60000",
    "$__interval_ms": "60000",
    "${__interval}": "1m",
    "$__interval": "1m",
    "${__range_ms}": "3600000",
    "$__range_ms": "3600000",
    "${__range_s}": "3600",
    "$__range_s": "3600",
    "${__range}": "1h",
    "$__range": "1h",
}

_VARIABLE_RE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\[\[[A-Za-z_][A-Za-z0-9_]*\]\]")

REJECTED_STATUSES = (400, 422)


def display_url(url: str) -> str:
    """The URL without ``user:password@``, for use in findings and logs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<prometheus-url>"
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def _scrub(text: str, url: str) -> str:
    """Remove the credentials part of ``url`` from an error text."""
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        return text
    if "@" in netloc:
        text = text.replace(netloc.rsplit("@", 1)[0] + "@", "")
    return text


def substitute_macros(expr: str) -> str:
    for macro, value in GRAFANA_MACROS.items():
        expr = expr.replace(macro, value)
    return expr


def has_template_variables(expr: str) -> bool:
    return _VARIABLE_RE.search(expr) is not None


def query_expressions(
    exprs: Iterable[ExprRef],
    prometheus_url: str,
    *,
    query_datasource_uids: frozenset[str] = frozenset(),
    timeout: float = 10.0,
    bearer_token_env: str | None = None,
    strict: bool = False,
    session: requests.Session | None = None,
) -> list[Finding]:
    """Send each expression to Prometheus and report rejected ones.

    ``query_datasource_uids`` limits Grafana rule expressions to those whose
    ``datasourceUid`` is in the set; an empty set means every expression.
    Expressions from PrometheusRule objects (``datasource_uid`` is None) are
    always queried. Expressions that still contain Grafana template variables
    after macro substitution are skipped because Prometheus cannot parse them.
    """
    headers = {}
    if bearer_token_env:
        token = os.environ.get(bearer_token_env)
        if not token:
            raise UsageError(f"environment variable {bearer_token_env} is not set or empty")
        headers["Authorization"] = f"Bearer {token}"

    endpoint = prometheus_url.rstrip("/") + "/api/v1/query"
    http = session or requests.Session()
    findings: list[Finding] = []
    cache: dict[str, tuple[int | None, str]] = {}
    transport_failed = False

    for ref in exprs:
        if (
            ref.datasource_uid is not None
            and query_datasource_uids
            and ref.datasource_uid not in query_datasource_uids
        ):
            continue
        expr = substitute_macros(ref.expr)
        if has_template_variables(expr):
            continue
        if transport_failed:
            break
        if expr not in cache:
            try:
                resp = http.post(
                    endpoint,
                    data={"query": expr, "time": str(time.time())},
                    headers=headers,
                    timeout=timeout,
                )
            except requests.RequestException as err:
                transport_failed = True
                findings.append(
                    Finding(
                        display_url(prometheus_url),
                        "AQ002",
                        f"could not query Prometheus: {_scrub(str(err), prometheus_url)}; "
                        "expression check skipped",
                        None,
                        LEVEL_ERROR if strict else LEVEL_WARNING,
                    )
                )
                break
            cache[expr] = (resp.status_code, _error_text(resp))
        status, detail = cache[expr]
        if status in REJECTED_STATUSES:
            findings.append(
                Finding(
                    ref.path, "AQ001", f"expression rejected by Prometheus: {detail}", ref.ident
                )
            )
        elif status is not None and status >= 300:
            findings.append(
                Finding(
                    ref.path,
                    "AQ002",
                    f"Prometheus returned HTTP {status} for the expression: {detail}",
                    ref.ident,
                    LEVEL_ERROR if strict else LEVEL_WARNING,
                )
            )
    return findings


def _error_text(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("error"):
        kind = body.get("errorType")
        return f"{kind}: {body['error']}" if kind else str(body["error"])
    text = " ".join(resp.text.split())
    return text[:300] if text else f"HTTP {resp.status_code}"
