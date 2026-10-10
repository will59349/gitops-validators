from __future__ import annotations

import json
import re

import pytest

from conftest import ALERT_BAD, ALERT_GOOD, DASH_BAD, DASH_GOOD
from gitops_validators.cli import main, main_alert_rules, main_dashboards, main_helm_values

LINE_RE = re.compile(r"^.+: \[[^\]]+\] (AR|PR|DB|AQ|HV)\d{3}( \(warning\))?: .+$")


# --------------------------------------------------------------------------
# Exit codes
# --------------------------------------------------------------------------


def test_alert_rules_clean_exit_0(capsys):
    assert main_alert_rules([str(ALERT_GOOD)]) == 0
    assert capsys.readouterr().out == ""


def test_alert_rules_findings_exit_1(capsys):
    assert main_alert_rules([str(ALERT_BAD / "ar004_title_missing.yaml")]) == 1
    out = capsys.readouterr().out.splitlines()
    assert LINE_RE.match(out[0]), out[0]
    assert "[no-title] AR004: title is missing" in out[0]
    assert out[-1] == "1 error(s), 0 warning(s)"


def test_dashboards_clean_exit_0(capsys):
    assert main_dashboards([str(DASH_GOOD)]) == 0
    assert capsys.readouterr().out == ""


def test_dashboards_findings_exit_1(capsys):
    assert main_dashboards([str(DASH_BAD / "db004_title_missing.json")]) == 1
    out = capsys.readouterr().out.splitlines()
    assert LINE_RE.match(out[0]), out[0]
    assert "DB004: title is missing" in out[0]


def test_dashboards_warning_only_exit_0(capsys):
    # An exported id is ignored by file provisioning: reported, but not a failure.
    assert main_dashboards([str(DASH_BAD / "db005_id_set.json")]) == 0
    out = capsys.readouterr().out.splitlines()
    assert LINE_RE.match(out[0]), out[0]
    assert "[id-set] DB005 (warning)" in out[0]
    assert out[-1] == "0 error(s), 1 warning(s)"


def test_missing_path_exit_2(capsys):
    assert main_alert_rules(["/definitely/not/here.yaml"]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert main_dashboards(["/definitely/not/here.json"]) == 2


def test_no_arguments_exit_2():
    with pytest.raises(SystemExit) as exc:
        main_alert_rules([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main_dashboards([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main_helm_values([])
    assert exc.value.code == 2


def test_bad_option_value_exit_2(capsys):
    assert main_alert_rules([str(ALERT_GOOD), "--severity", " , "]) == 2
    assert "--severity" in capsys.readouterr().err


def test_help_exit_0(capsys):
    for entry in (main_alert_rules, main_dashboards, main_helm_values):
        with pytest.raises(SystemExit) as exc:
            entry(["--help"])
        assert exc.value.code == 0
        assert "usage:" in capsys.readouterr().out


def test_helm_values_missing_binary_exit_2(capsys):
    code = main_helm_values(
        ["tests/fixtures/chart", "tests/fixtures/chart/values-good.yaml", "--helm-binary", "nope-x"]
    )
    assert code == 2
    assert "not found in PATH" in capsys.readouterr().err


# --------------------------------------------------------------------------
# JSON output
# --------------------------------------------------------------------------


def test_json_output_alert_rules(capsys):
    code = main_alert_rules([str(ALERT_BAD / "ar009_severity.yaml"), "--format", "json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == 1
    assert payload["warnings"] == 0
    assert len(payload["findings"]) == 1
    finding = payload["findings"][0]
    assert set(finding) == {"path", "check", "message", "ident", "level"}
    assert finding["check"] == "AR009"
    assert finding["ident"] == "severity-unknown"
    assert finding["level"] == "error"
    assert finding["path"].endswith("ar009_severity.yaml")


def test_json_output_clean_is_empty_list(capsys):
    assert main_dashboards([str(DASH_GOOD), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"findings": [], "errors": 0, "warnings": 0}


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------


def test_severity_and_annotation_options(capsys):
    args = [
        str(ALERT_BAD / "ar009_severity.yaml"),
        "--severity",
        "page,ticket",
        "--require-annotation",
        "summary",
        "--require-annotation",
        "runbook_url",
    ]
    assert main_alert_rules(args) == 1
    out = capsys.readouterr().out
    assert "AR009" not in out
    assert "AR010: annotations.runbook_url is missing" in out


def test_disable_turns_off_convention_checks(capsys):
    sev = str(ALERT_BAD / "ar009_severity.yaml")
    ann = str(ALERT_BAD / "ar010_annotation_missing.yaml")
    assert main_alert_rules([sev, ann]) == 1
    out = capsys.readouterr().out
    assert "AR009" in out and "AR010" in out
    assert main_alert_rules([sev, ann, "--disable", "AR009,AR010"]) == 0
    assert capsys.readouterr().out == ""
    # Repeatable, case-insensitive, and each id only silences itself.
    assert main_alert_rules([sev, ann, "--disable", "ar009", "--disable", "PR005"]) == 1
    out = capsys.readouterr().out
    assert "AR009" not in out and "AR010" in out


def test_disable_applies_to_prometheusrule_and_dashboards(capsys):
    assert main_alert_rules([str(ALERT_BAD / "pr005_severity.yaml"), "--disable", "PR005"]) == 0
    assert main_dashboards([str(DASH_BAD / "db005_id_set.json"), "--disable", "DB005"]) == 0
    assert capsys.readouterr().out == ""


def test_disable_unknown_id_exit_2(capsys):
    assert main_alert_rules([str(ALERT_GOOD), "--disable", "AR099"]) == 2
    assert "unknown check id 'AR099'" in capsys.readouterr().err
    assert main_dashboards([str(DASH_GOOD), "--disable", "severity"]) == 2


def test_disable_is_accepted_by_helm_values(capsys):
    args = ["tests/fixtures/chart", "tests/fixtures/chart/values-good.yaml", "--disable", "XX001"]
    assert main_helm_values(args) == 2
    assert "unknown check id" in capsys.readouterr().err


def test_skip_non_dashboards_for_files_passed_by_another_tool(tmp_path, capsys):
    # pre-commit names every changed YAML/JSON file; those are not all dashboards.
    other = tmp_path / "package.json"
    other.write_text('{"name": "x", "version": "1.0.0"}')
    values = tmp_path / "values.yaml"
    values.write_text("replicaCount: 2\n")
    bad = str(DASH_BAD / "db004_title_missing.json")
    assert main_dashboards([str(other), str(values)]) == 1  # named explicitly: checked
    capsys.readouterr()
    assert main_dashboards([str(other), str(values), "--skip-non-dashboards"]) == 0
    assert capsys.readouterr().out == ""
    assert main_dashboards([str(other), bad, "--skip-non-dashboards"]) == 1
    assert "DB004" in capsys.readouterr().out


def test_datasource_uid_option_is_repeatable(capsys):
    args = [str(DASH_GOOD), "--datasource-uid", "prometheus-main", "--datasource-uid", "loki-main"]
    assert main_dashboards(args) == 0
    assert main_dashboards([str(DASH_GOOD), "--datasource-uid", "prometheus-main"]) == 1
    assert "DB009" in capsys.readouterr().out


def test_multiple_paths_and_directories(capsys):
    code = main_alert_rules(
        [str(ALERT_GOOD / "rules.yaml"), str(ALERT_BAD / "ar003_duplicate_uid_other.yaml")]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert out.count("AR003") == 2


# --------------------------------------------------------------------------
# python -m gitops_validators dispatcher
# --------------------------------------------------------------------------


def test_module_dispatcher(capsys):
    assert main(["check-dashboards", str(DASH_GOOD)]) == 0
    assert main(["check-alert-rules", str(ALERT_BAD / "ar004_title_missing.yaml")]) == 1
    capsys.readouterr()
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err
    assert main(["not-a-command"]) == 2
