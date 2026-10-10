from __future__ import annotations

import json
import shutil

import pytest
import yaml

from conftest import DASH_BAD, DASH_GOOD, check_ids, describe
from gitops_validators import dashboards
from gitops_validators.common import LEVEL_WARNING, collect_files
from gitops_validators.dashboards import DashboardOptions


def run(paths, **options):
    files = collect_files([str(p) for p in paths])
    return dashboards.check_files(files, DashboardOptions(**options))


# --------------------------------------------------------------------------
# Good samples
# --------------------------------------------------------------------------


def test_good_directory_is_clean():
    findings = run([DASH_GOOD])
    assert findings == [], describe(findings)


def test_good_directory_is_clean_with_allowed_datasources():
    findings = run([DASH_GOOD], datasource_uids=frozenset({"prometheus-main", "loki-main"}))
    assert findings == [], describe(findings)


def test_wrapped_api_export_is_unwrapped():
    findings = run([DASH_GOOD / "wrapped-export.json"], datasource_uids=frozenset({"x"}))
    # Only the datasource check fires, proving the inner dashboard was inspected.
    assert check_ids(findings) == {"DB009"}
    assert findings[0].ident == "api-latency"


# --------------------------------------------------------------------------
# One failing fixture per check id
# --------------------------------------------------------------------------

BAD_CASES = [
    ("db001_invalid_json.json", "DB001"),
    ("db001_configmap_invalid_json.yaml", "DB001"),
    ("db002_uid_too_long.json", "DB002"),
    ("db002_uid_missing.json", "DB002"),
    ("db002_uid_invalid_chars.json", "DB002"),
    ("db003_duplicate_uid.yaml", "DB003"),
    ("db004_title_missing.json", "DB004"),
    ("db005_id_set.json", "DB005"),
    ("db006_duplicate_panel_id.json", "DB006"),
    ("db007_export_placeholders.json", "DB007"),
    ("db008_schema_version_missing.json", "DB008"),
    ("db011_key_not_json.yaml", "DB011"),
]


@pytest.mark.parametrize(("name", "check"), BAD_CASES)
def test_bad_fixture_reports_exactly_its_check_id(name, check):
    findings = run([DASH_BAD / name])
    assert check_ids(findings) == {check}, describe(findings)


def test_db002_missing_uid_is_a_warning_and_bad_uid_an_error():
    # Grafana generates a uid for a dashboard without one; it rejects a long or malformed one.
    assert [f.level for f in run([DASH_BAD / "db002_uid_missing.json"])] == [LEVEL_WARNING]
    assert [f.level for f in run([DASH_BAD / "db002_uid_too_long.json"])] == ["error"]
    assert [f.level for f in run([DASH_BAD / "db002_uid_invalid_chars.json"])] == ["error"]


def test_db005_exported_id_is_a_warning():
    findings = run([DASH_BAD / "db005_id_set.json"])
    assert [f.level for f in findings] == [LEVEL_WARNING]
    assert "file provisioning ignores it" in findings[0].message


def test_db001_configmap_message_names_configmap_and_key():
    findings = run([DASH_BAD / "db001_configmap_invalid_json.yaml"])
    assert len(findings) == 1
    assert findings[0].path.endswith("::grafana-dashboards-broken/broken.json")
    assert "dropped" in findings[0].message


def test_db003_duplicate_uid_across_files(tmp_path):
    shutil.copy(DASH_GOOD / "node-overview.json", tmp_path / "a.json")
    shutil.copy(DASH_GOOD / "node-overview.json", tmp_path / "b.json")
    findings = run([tmp_path])
    assert check_ids(findings) == {"DB003"}
    assert {f.path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for f in findings} == {
        "a.json",
        "b.json",
    }
    assert all(f.ident == "node-overview" for f in findings)


def test_db006_lists_the_duplicate_id():
    findings = run([DASH_BAD / "db006_duplicate_panel_id.json"])
    assert "1" in findings[0].message


def test_db007_reports_both_inputs_and_placeholder():
    findings = run([DASH_BAD / "db007_export_placeholders.json"])
    messages = " ".join(f.message for f in findings)
    assert "__inputs" in messages
    assert "${DS_PROMETHEUS}" in messages


def test_db009_only_with_allowed_set():
    clean = run([DASH_BAD / "db009_datasource_not_allowed.json"])
    assert clean == []
    findings = run(
        [DASH_BAD / "db009_datasource_not_allowed.json"],
        datasource_uids=frozenset({"prometheus-main"}),
    )
    assert check_ids(findings) == {"DB009"}
    assert "prometheus-old" in findings[0].message
    assert "${datasource}" not in findings[0].message


def _configmap_with_dashboard(size_bytes: int) -> str:
    dashboard = {
        "uid": "big",
        "title": "Big",
        "id": None,
        "schemaVersion": 39,
        "description": "",
        "panels": [],
    }
    base = len(json.dumps(dashboard))
    dashboard["description"] = "x" * max(0, size_bytes - base)
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "big", "labels": {"grafana_dashboard": "1"}},
        "data": {"big.json": json.dumps(dashboard)},
    }
    return yaml.safe_dump(cm)


def test_db010_warns_above_900_kib_and_fails_above_1_mib(tmp_path):
    warn = tmp_path / "warn.yaml"
    warn.write_text(_configmap_with_dashboard(950 * 1024))
    findings = run([warn])
    assert check_ids(findings) == {"DB010"}
    assert findings[0].level == LEVEL_WARNING

    fail = tmp_path / "fail.yaml"
    fail.write_text(_configmap_with_dashboard(1100 * 1024))
    findings = run([fail])
    assert check_ids(findings) == {"DB010"}
    assert findings[0].level == "error"

    small = tmp_path / "small.yaml"
    small.write_text(_configmap_with_dashboard(100 * 1024))
    assert run([small]) == []


def test_db011_with_custom_label(tmp_path):
    text = (DASH_BAD / "db011_key_not_json.yaml").read_text()
    custom = tmp_path / "cm.yaml"
    custom.write_text(text.replace("grafana_dashboard:", "my_dashboard:"))
    assert run([custom]) == []  # default label not present, key not .json: ignored
    findings = run([custom], label="my_dashboard")
    assert check_ids(findings) == {"DB011"}


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------


def test_walked_json_that_is_not_a_dashboard_is_ignored(tmp_path):
    (tmp_path / "renovate.json").write_text('{"extends": ["config:base"]}')
    assert run([tmp_path]) == []


def test_explicit_json_that_is_not_a_dashboard_is_checked(tmp_path):
    p = tmp_path / "renovate.json"
    p.write_text('{"extends": ["config:base"]}')
    findings = run([p])
    assert {"DB002", "DB004", "DB008"} <= check_ids(findings)


def test_invalid_yaml_manifest_is_db001(tmp_path):
    p = tmp_path / "cm.yaml"
    p.write_text("kind: ConfigMap\ndata: [\n")
    findings = run([p])
    assert check_ids(findings) == {"DB001"}
