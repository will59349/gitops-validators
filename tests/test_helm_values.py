from __future__ import annotations

import shutil

import pytest

from conftest import CHART, check_ids, describe
from gitops_validators.common import UsageError
from gitops_validators.helm_values import HelmOptions, check_helm_values, helm_template_command

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def run(values_name: str, **helm_kwargs):
    return check_helm_values(str(CHART), [str(CHART / values_name)], HelmOptions(**helm_kwargs))


def test_good_values_render_clean():
    findings, exprs = run("values-good.yaml")
    assert findings == [], describe(findings)
    assert [e.ident for e in exprs] == ["node-cpu-high"]
    assert exprs[0].path.endswith("values-good.yaml (rendered)::release-alert-rules/rules.yaml")


def test_invalid_dashboard_json_in_values_is_caught_by_real_helm_template():
    findings, _ = run("values-bad-json.yaml")
    assert check_ids(findings) == {"DB001"}, describe(findings)
    f = findings[0]
    assert f.path.endswith(
        "values-bad-json.yaml (rendered)::release-dashboard-node-overview/node-overview.json"
    )
    assert "not valid JSON" in f.message


def test_invalid_alert_rule_in_values_is_caught():
    findings, _ = run("values-bad-rule.yaml")
    assert check_ids(findings) == {"AR002"}, describe(findings)
    assert findings[0].path.startswith(str(CHART / "values-bad-rule.yaml"))


def test_render_error_is_reported_with_helm_stderr():
    findings, _ = run("values-render-error.yaml")
    assert check_ids(findings) == {"HV001"}
    assert findings[0].path == str(CHART / "values-render-error.yaml")
    assert "not-a-map" in findings[0].message


def test_extra_set_is_passed_through():
    findings, _ = run(
        "values-bad-rule.yaml",
        extra_set=("alertRules.groups[0].rules[0].uid=short-uid",),
    )
    assert findings == [], describe(findings)


def test_release_name_changes_rendered_object_names():
    findings, _ = run("values-bad-json.yaml", release="grafana")
    assert "::grafana-dashboard-node-overview/" in findings[0].path


def test_missing_helm_binary_is_a_usage_error():
    with pytest.raises(UsageError, match="not found in PATH"):
        run("values-good.yaml", helm_binary="definitely-not-helm-xyz")


def test_not_a_chart_directory_is_a_usage_error(tmp_path):
    with pytest.raises(UsageError, match="Chart.yaml"):
        check_helm_values(str(tmp_path), [str(CHART / "values-good.yaml")])


def test_missing_values_file_is_a_usage_error():
    with pytest.raises(UsageError, match="does not exist"):
        check_helm_values(str(CHART), [str(CHART / "nope.yaml")])


def test_helm_command_line_builder():
    cmd = helm_template_command(
        "charts/x",
        "v.yaml",
        HelmOptions(
            release="rel",
            namespace="monitoring",
            kube_version="1.30.0",
            dependency_update=True,
            extra_set=("a=1", "b=2"),
            helm_binary="/usr/local/bin/helm",
        ),
    )
    assert cmd == [
        "/usr/local/bin/helm",
        "template",
        "rel",
        "charts/x",
        "-f",
        "v.yaml",
        "--namespace",
        "monitoring",
        "--kube-version",
        "1.30.0",
        "--dependency-update",
        "--set",
        "a=1",
        "--set",
        "b=2",
    ]
