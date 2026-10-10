from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ALERT_BAD, ALERT_GOOD, check_ids, describe
from gitops_validators import alert_rules
from gitops_validators.alert_rules import AlertRuleOptions
from gitops_validators.common import LEVEL_WARNING, collect_files
from gitops_validators.durations import is_valid_duration


def run(paths, **options):
    files = collect_files([str(p) for p in paths])
    return alert_rules.check_files(files, AlertRuleOptions(**options))


# --------------------------------------------------------------------------
# Good samples
# --------------------------------------------------------------------------


def test_good_directory_is_clean():
    findings, exprs = run([ALERT_GOOD])
    assert findings == [], describe(findings)
    # 3 rules in rules.yaml, 1 in configmap.yaml, 3 in prometheusrule.yaml, 2 in multi-doc.yaml
    assert len(exprs) == 9


def test_good_directory_is_clean_with_allowed_datasources():
    findings, _ = run([ALERT_GOOD], datasource_uids=frozenset({"prometheus-main"}))
    assert findings == [], describe(findings)


def test_configmap_embedded_rules_are_labelled_with_name_and_key():
    _, exprs = run([ALERT_GOOD / "configmap.yaml"])
    assert len(exprs) == 1
    assert exprs[0].path.endswith("configmap.yaml::monitoring/grafana-alert-rules/node.yaml")
    assert exprs[0].ident == "cm-node-memory-high"
    assert exprs[0].datasource_uid == "prometheus-main"


def test_multi_document_stream_covers_configmap_and_prometheusrule():
    findings, exprs = run([ALERT_GOOD / "multi-doc.yaml"])
    assert findings == [], describe(findings)
    assert {e.ident for e in exprs} == {"multi-doc-disk-full", "TargetMissing"}


def test_prometheusrule_expressions_have_no_datasource_uid():
    _, exprs = run([ALERT_GOOD / "prometheusrule.yaml"])
    assert len(exprs) == 3
    assert all(e.datasource_uid is None for e in exprs)
    assert {e.ident for e in exprs} == {
        "instance:node_cpu_utilisation:rate5m",
        "NodeCPUHigh",
        "NodeDown",
    }


# --------------------------------------------------------------------------
# One failing fixture per check id
# --------------------------------------------------------------------------

BAD_CASES = [
    ("ar001_invalid_yaml.yaml", "AR001"),
    ("ar001_configmap_invalid_yaml.yaml", "AR001"),
    ("ar002_uid_invalid.yaml", "AR002"),
    ("ar003_duplicate_uid.yaml", "AR003"),
    ("ar004_title_missing.yaml", "AR004"),
    ("ar005_duplicate_title.yaml", "AR005"),
    ("ar006_bad_duration.yaml", "AR006"),
    ("ar007_condition_missing_ref.yaml", "AR007"),
    ("ar008_datasource_missing.yaml", "AR008"),
    ("ar009_severity.yaml", "AR009"),
    ("ar010_annotation_missing.yaml", "AR010"),
    ("ar011_states.yaml", "AR011"),
    ("ar012_structure.yaml", "AR012"),
    ("ar013_empty_group.yaml", "AR013"),
    ("ar014_for_missing.yaml", "AR014"),
    ("pr001_alert_and_record.yaml", "PR001"),
    ("pr002_expr_missing.yaml", "PR002"),
    ("pr003_duplicate_alert.yaml", "PR003"),
    ("pr004_bad_duration.yaml", "PR004"),
    ("pr005_severity.yaml", "PR005"),
]


@pytest.mark.parametrize(("name", "check"), BAD_CASES)
def test_bad_fixture_reports_exactly_its_check_id(name, check):
    findings, _ = run([ALERT_BAD / name])
    assert check_ids(findings) == {check}, describe(findings)


def test_ar001_message_says_provisioning_fails():
    findings, _ = run([ALERT_BAD / "ar001_invalid_yaml.yaml"])
    assert len(findings) == 1
    assert "alert provisioning fails as a whole" in findings[0].message
    assert "line " in findings[0].message


def test_ar002_reports_each_bad_uid_once():
    findings, _ = run([ALERT_BAD / "ar002_uid_invalid.yaml"])
    messages = sorted(f.message for f in findings)
    assert len(messages) == 3
    assert any("missing" in m for m in messages)
    assert any("50 characters" in m for m in messages)
    assert any("letters, digits" in m for m in messages)


def test_ar003_duplicate_uid_across_files_reports_both_locations():
    a = ALERT_GOOD / "rules.yaml"
    b = ALERT_BAD / "ar003_duplicate_uid_other.yaml"
    findings, _ = run([a, b])
    dupes = [f for f in findings if f.check == "AR003"]
    assert {Path(f.path).name for f in dupes} == {a.name, b.name}
    assert all(f.ident == "node-cpu-high" for f in dupes)
    for f in dupes:
        other = b.name if Path(f.path).name == a.name else a.name
        assert other in f.message


def test_ar005_duplicate_title_only_within_same_folder(tmp_path):
    text = (ALERT_BAD / "ar005_duplicate_title.yaml").read_text()
    # Move the second rule into a group with another folder: no longer a duplicate.
    marker = "      - uid: title-dup-b"
    head, second_rule = text.split(marker)
    other_group = (
        "  - orgId: 1\n    name: other\n    folder: other-folder\n    interval: 1m\n    rules:\n"
    )
    p = tmp_path / "rules.yaml"
    p.write_text(head + other_group + marker + second_rule)
    findings, _ = run([p])
    assert findings == [], describe(findings)


def test_ar003_same_file_message_counts_occurrences():
    findings, _ = run([ALERT_BAD / "ar003_duplicate_uid.yaml"])
    assert len(findings) == 2
    assert all("used 2 times in this file" in f.message for f in findings)


def test_ar006_covers_group_interval_and_rule_for():
    findings, _ = run([ALERT_BAD / "ar006_bad_duration.yaml"])
    messages = [f.message for f in findings]
    assert any("interval" in m for m in messages)
    assert any("'for'" in m for m in messages)


def test_ar006_group_without_interval_is_an_error(tmp_path):
    # Grafana parses the group interval unconditionally; an empty one fails the file.
    text = (ALERT_GOOD / "rules.yaml").read_text().replace("    interval: 30s\n", "")
    p = tmp_path / "rules.yaml"
    p.write_text(text)
    findings, _ = run([p])
    assert [(f.check, f.ident, f.level) for f in findings] == [("AR006", "ingress", "error")]
    assert "no 'interval'" in findings[0].message


def test_ar013_group_without_rules_is_a_warning_not_an_error():
    findings, _ = run([ALERT_BAD / "ar013_empty_group.yaml"])
    assert [(f.ident, f.level) for f in findings] == [
        ("rendered-empty", LEVEL_WARNING),
        ("empty-list", LEVEL_WARNING),
        ("no-rules-key", LEVEL_WARNING),
    ]


def test_ar012_rules_of_the_wrong_type_is_still_an_error():
    findings, _ = run([ALERT_BAD / "ar012_structure.yaml"])
    assert any("'rules' must be a list" in f.message and f.level == "error" for f in findings)


def test_ar013_applies_to_prometheusrule_groups(tmp_path):
    text = (ALERT_GOOD / "prometheusrule.yaml").read_text()
    p = tmp_path / "pr.yaml"
    p.write_text(text + "    - name: empty-group\n      rules: []\n")
    findings, _ = run([p])
    assert [(f.check, f.level) for f in findings] == [("AR013", LEVEL_WARNING)]


def test_ar014_missing_for_is_a_warning():
    findings, _ = run([ALERT_BAD / "ar014_for_missing.yaml"])
    assert [(f.check, f.ident, f.level) for f in findings] == [("AR014", "no-for", LEVEL_WARNING)]


def test_ar008_allowed_set_rejects_other_datasources():
    clean, _ = run([ALERT_BAD / "ar008_datasource_not_allowed.yaml"])
    assert clean == []
    findings, _ = run(
        [ALERT_BAD / "ar008_datasource_not_allowed.yaml"],
        datasource_uids=frozenset({"prometheus-main"}),
    )
    assert check_ids(findings) == {"AR008"}
    assert "loki-main" in findings[0].message


def test_ar009_severity_set_is_configurable():
    findings, _ = run([ALERT_BAD / "ar009_severity.yaml"], severities=("page", "ticket"))
    assert findings == []


def test_ar010_required_annotations_are_configurable():
    findings, _ = run([ALERT_GOOD / "rules.yaml"], required_annotations=("summary", "runbook_url"))
    assert check_ids(findings) == {"AR010"}
    assert {f.ident for f in findings} == {"node-down", "ingress-5xx-ratio"}


def test_pr005_severity_set_applies_to_prometheusrule_too():
    findings, _ = run([ALERT_BAD / "pr005_severity.yaml"], severities=("page",))
    # NodeDown now passes; NodeNoLabels still has no labels at all.
    assert [f.ident for f in findings] == ["NodeNoLabels"]


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------


def test_directory_walk_skips_hidden_directories_and_other_extensions(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "rules.yaml").write_text("groups: [")
    (tmp_path / "notes.txt").write_text("groups: [")
    (tmp_path / "ok.yaml").write_text((ALERT_GOOD / "rules.yaml").read_text())
    findings, _ = run([tmp_path])
    assert findings == [], describe(findings)


def test_json_rules_file_is_accepted(tmp_path):
    import json

    import yaml

    doc = yaml.safe_load((ALERT_GOOD / "rules.yaml").read_text())
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(doc))
    findings, exprs = run([p])
    assert findings == []
    assert len(exprs) == 3


def test_unrelated_yaml_is_ignored(tmp_path):
    (tmp_path / "values.yaml").write_text("replicaCount: 2\nimage:\n  tag: v1\n")
    findings, _ = run([tmp_path])
    assert findings == []


# --------------------------------------------------------------------------
# Durations
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["30s", "5m", "1h", "1h30m", "2d", "1w", "1y", "500ms", "0", 0, "1d12h5m"]
)
def test_valid_durations(value):
    assert is_valid_duration(value)


@pytest.mark.parametrize(
    "value", ["5 minutes", "5", "", "m5", "1.5h", "30S", None, 5, True, "5m30m", "1h2d"]
)
def test_invalid_durations(value):
    assert not is_valid_duration(value)
