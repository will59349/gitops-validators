"""Checks for Grafana alert rule provisioning files and PrometheusRule objects.

Input forms handled:

* Grafana provisioning YAML (``apiVersion: 1`` + ``groups``), as a file or as
  a ``.yaml``/``.yml`` value inside a Kubernetes ConfigMap.
* Multi-document YAML and rendered Helm output containing such ConfigMaps.
* Prometheus Operator ``PrometheusRule`` objects.

Check ids are documented in docs/checks.md.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml

from .common import (
    LEVEL_ERROR,
    YAML_EXTENSIONS,
    Finding,
    InputFile,
    embedded_source,
    iter_kubernetes_objects,
    json_error_message,
    object_name,
    parse_json,
    parse_yaml_documents,
    read_text,
    yaml_error_message,
)
from .durations import is_valid_duration

UID_MAX_LENGTH = 40
UID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

DEFAULT_SEVERITIES: tuple[str, ...] = ("critical", "warning", "info")
DEFAULT_REQUIRED_ANNOTATIONS: tuple[str, ...] = ("summary",)

VALID_NO_DATA_STATES = frozenset({"NoData", "Alerting", "OK", "KeepLast"})
VALID_EXEC_ERR_STATES = frozenset({"Error", "Alerting", "OK", "KeepLast"})

EXPRESSION_DATASOURCE = "__expr__"


@dataclass(frozen=True)
class AlertRuleOptions:
    """Tunables for the alert-rule checks."""

    datasource_uids: frozenset[str] = frozenset()
    severities: tuple[str, ...] = DEFAULT_SEVERITIES
    required_annotations: tuple[str, ...] = DEFAULT_REQUIRED_ANNOTATIONS


@dataclass(frozen=True)
class ExprRef:
    """A PromQL expression found in a rule, for the optional Prometheus query check."""

    path: str
    ident: str
    expr: str
    datasource_uid: str | None  # None for PrometheusRule objects


@dataclass
class _Location:
    path: str
    ident: str


@dataclass
class _RunContext:
    options: AlertRuleOptions
    findings: list[Finding] = field(default_factory=list)
    exprs: list[ExprRef] = field(default_factory=list)
    uids: dict[str, list[_Location]] = field(default_factory=lambda: defaultdict(list))
    titles: dict[tuple[Any, Any, str], list[_Location]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def add(
        self,
        path: str,
        check: str,
        message: str,
        ident: str | None = None,
        level: str = LEVEL_ERROR,
    ) -> None:
        self.findings.append(Finding(path, check, message, ident, level))

    def finalize(self) -> None:
        for uid, locations in self.uids.items():
            if len(locations) < 2:
                continue
            for loc in locations:
                other_paths = sorted({o.path for o in locations if o.path != loc.path})
                if other_paths:
                    where = f"also used in {', '.join(other_paths)}"
                else:
                    where = f"used {len(locations)} times in this file"
                self.add(
                    loc.path,
                    "AR003",
                    f"uid '{uid}' is {where}; Grafana keeps only one of them",
                    uid,
                )
        for (org, folder, title), locations in self.titles.items():
            if len(locations) < 2:
                continue
            for loc in locations:
                others = ", ".join(f"{o.path} [{o.ident}]" for o in locations if o is not loc)
                self.add(
                    loc.path,
                    "AR005",
                    f"title '{title}' is also used in folder '{folder}' (org {org}) by {others}",
                    loc.ident,
                )


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def check_files(
    files: Sequence[InputFile], options: AlertRuleOptions | None = None
) -> tuple[list[Finding], list[ExprRef]]:
    """Check files; returns findings and the PromQL expressions seen."""
    ctx = _RunContext(options or AlertRuleOptions())
    for f in files:
        try:
            text = read_text(f.path)
        except (OSError, UnicodeDecodeError) as err:
            ctx.add(str(f.path), "AR001", f"cannot read file: {err}")
            continue
        check_text(text, str(f.path), ctx, is_json=f.is_json)
    ctx.finalize()
    return ctx.findings, ctx.exprs


def check_rendered(
    text: str, source: str, options: AlertRuleOptions | None = None
) -> tuple[list[Finding], list[ExprRef]]:
    """Check one rendered manifest stream (for example ``helm template`` output)."""
    ctx = _RunContext(options or AlertRuleOptions())
    check_text(text, source, ctx, is_json=False)
    ctx.finalize()
    return ctx.findings, ctx.exprs


def check_text(text: str, source: str, ctx: _RunContext, *, is_json: bool) -> None:
    if is_json:
        try:
            docs = [parse_json(text)]
        except json.JSONDecodeError as err:
            ctx.add(
                source,
                "AR001",
                f"file is not valid JSON ({json_error_message(err)}); "
                "Grafana skips the whole file and every rule in it",
            )
            return
    else:
        try:
            docs = parse_yaml_documents(text)
        except yaml.YAMLError as err:
            ctx.add(
                source,
                "AR001",
                f"file is not valid YAML ({yaml_error_message(err)}); "
                "Grafana skips the whole file and every rule in it",
            )
            return
    for doc in docs:
        _check_document(doc, source, ctx)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def _check_document(doc: Any, source: str, ctx: _RunContext) -> None:
    if not isinstance(doc, dict):
        return
    for obj in iter_kubernetes_objects(doc):
        kind = obj.get("kind")
        if kind == "ConfigMap":
            _check_configmap(obj, source, ctx)
        elif kind == "PrometheusRule":
            _check_prometheus_rule(obj, source, ctx)
        elif kind is None and isinstance(obj.get("groups"), list):
            _check_provisioning_doc(obj, source, ctx)
        elif kind is None and "groups" in obj:
            ctx.add(source, "AR012", "'groups' must be a list")


def _check_configmap(obj: dict[str, Any], source: str, ctx: _RunContext) -> None:
    data = obj.get("data")
    if not isinstance(data, dict):
        return
    name = object_name(obj)
    for key, value in data.items():
        if not str(key).lower().endswith(YAML_EXTENSIONS):
            continue
        label = embedded_source(source, name, str(key))
        if not isinstance(value, str):
            ctx.add(label, "AR001", "ConfigMap value is not a string")
            continue
        try:
            docs = parse_yaml_documents(value)
        except yaml.YAMLError as err:
            ctx.add(
                label,
                "AR001",
                f"ConfigMap value is not valid YAML ({yaml_error_message(err)}); "
                "Grafana skips the whole file and every rule in it",
            )
            continue
        for doc in docs:
            if isinstance(doc, dict) and "groups" in doc:
                if isinstance(doc["groups"], list):
                    _check_provisioning_doc(doc, label, ctx)
                else:
                    ctx.add(label, "AR012", "'groups' must be a list")


# --------------------------------------------------------------------------
# Grafana provisioning format
# --------------------------------------------------------------------------


def _check_provisioning_doc(doc: dict[str, Any], source: str, ctx: _RunContext) -> None:
    for gi, group in enumerate(doc["groups"]):
        if not isinstance(group, dict):
            ctx.add(source, "AR012", f"group #{gi} is not a mapping", f"group#{gi}")
            continue
        gname = group.get("name")
        gident = str(gname) if gname else f"group#{gi}"
        if not gname or not isinstance(gname, str):
            ctx.add(source, "AR012", f"group #{gi} has no name", gident)
        folder = group.get("folder")
        if not folder or not isinstance(folder, str):
            ctx.add(
                source,
                "AR012",
                "group has no 'folder'; Grafana file provisioning requires one",
                gident,
            )
        org_id = group.get("orgId", 1)
        if "interval" in group and not is_valid_duration(group["interval"]):
            ctx.add(
                source,
                "AR006",
                f"group interval {group['interval']!r} is not a valid duration (e.g. 1m, 30s)",
                gident,
            )
        rules = group.get("rules")
        if not isinstance(rules, list):
            ctx.add(source, "AR012", "group has no 'rules' list", gident)
            continue
        for ri, rule in enumerate(rules):
            if not isinstance(rule, dict):
                ctx.add(source, "AR012", f"rule #{ri} is not a mapping", f"{gident}#{ri}")
                continue
            _check_grafana_rule(rule, source, ctx, gident, ri, org_id, folder)


def _check_grafana_rule(
    rule: dict[str, Any],
    source: str,
    ctx: _RunContext,
    gident: str,
    index: int,
    org_id: Any,
    folder: Any,
) -> None:
    opts = ctx.options
    uid = rule.get("uid")
    ident = str(uid) if isinstance(uid, str) and uid else f"{gident}#{index}"

    # AR002 / AR003: uid
    if uid is None or uid == "":
        ctx.add(source, "AR002", "uid is missing; provisioned rules must set one", ident)
    elif not isinstance(uid, str):
        ctx.add(source, "AR002", f"uid must be a string, got {type(uid).__name__}", ident)
    elif len(uid) > UID_MAX_LENGTH:
        ctx.add(
            source,
            "AR002",
            f"uid is {len(uid)} characters, the maximum is {UID_MAX_LENGTH}",
            ident,
        )
    elif not UID_RE.match(uid):
        ctx.add(
            source,
            "AR002",
            "uid may only contain letters, digits, '-' and '_'",
            ident,
        )
    else:
        ctx.uids[uid].append(_Location(source, ident))

    # AR004 / AR005: title
    title = rule.get("title")
    if not isinstance(title, str) or not title.strip():
        ctx.add(source, "AR004", "title is missing", ident)
    else:
        ctx.titles[(org_id, folder, title.strip())].append(_Location(source, ident))

    # AR006: for
    if "for" in rule and not is_valid_duration(rule["for"]):
        ctx.add(
            source,
            "AR006",
            f"'for' value {rule['for']!r} is not a valid duration (e.g. 5m, 1h30m)",
            ident,
        )

    # AR012 / AR007 / AR008: data and condition
    data = rule.get("data")
    ref_ids: set[str] = set()
    if not isinstance(data, list) or not data:
        ctx.add(source, "AR012", "'data' must be a non-empty list of queries", ident)
    else:
        for di, item in enumerate(data):
            if not isinstance(item, dict):
                ctx.add(source, "AR012", f"data item #{di} is not a mapping", ident)
                continue
            ref_id = item.get("refId")
            if not isinstance(ref_id, str) or not ref_id:
                ctx.add(source, "AR012", f"data item #{di} has no refId", ident)
            else:
                ref_ids.add(ref_id)
            ds_uid = item.get("datasourceUid")
            if not isinstance(ds_uid, str) or not ds_uid:
                ctx.add(
                    source,
                    "AR008",
                    f"data item {ref_id or '#' + str(di)} has no datasourceUid",
                    ident,
                )
                ds_uid = None
            elif (
                opts.datasource_uids
                and ds_uid != EXPRESSION_DATASOURCE
                and ds_uid not in opts.datasource_uids
            ):
                ctx.add(
                    source,
                    "AR008",
                    f"data item {ref_id or '#' + str(di)} uses datasourceUid '{ds_uid}' "
                    f"which is not in the allowed set ({', '.join(sorted(opts.datasource_uids))})",
                    ident,
                )
            model = item.get("model")
            if (
                ds_uid is not None
                and ds_uid != EXPRESSION_DATASOURCE
                and isinstance(model, dict)
                and isinstance(model.get("expr"), str)
                and model["expr"].strip()
            ):
                ctx.exprs.append(ExprRef(source, ident, model["expr"], ds_uid))

    condition = rule.get("condition")
    if not isinstance(condition, str) or not condition:
        ctx.add(source, "AR007", "condition is missing", ident)
    elif condition not in ref_ids and ref_ids:
        ctx.add(
            source,
            "AR007",
            f"condition '{condition}' does not match any refId in data "
            f"({', '.join(sorted(ref_ids))})",
            ident,
        )

    # AR009: severity
    labels = rule.get("labels")
    severity = labels.get("severity") if isinstance(labels, dict) else None
    if severity is None or severity == "":
        ctx.add(source, "AR009", "labels.severity is missing", ident)
    elif str(severity) not in opts.severities:
        ctx.add(
            source,
            "AR009",
            f"labels.severity '{severity}' is not one of {', '.join(opts.severities)}",
            ident,
        )

    # AR010: annotations
    annotations = rule.get("annotations")
    for required in opts.required_annotations:
        value = annotations.get(required) if isinstance(annotations, dict) else None
        if not isinstance(value, str) or not value.strip():
            ctx.add(source, "AR010", f"annotations.{required} is missing", ident)

    # AR011: states
    nds = rule.get("noDataState")
    if nds is not None and nds not in VALID_NO_DATA_STATES:
        ctx.add(
            source,
            "AR011",
            f"noDataState '{nds}' is not one of {', '.join(sorted(VALID_NO_DATA_STATES))}",
            ident,
        )
    ees = rule.get("execErrState")
    if ees is not None and ees not in VALID_EXEC_ERR_STATES:
        ctx.add(
            source,
            "AR011",
            f"execErrState '{ees}' is not one of {', '.join(sorted(VALID_EXEC_ERR_STATES))}",
            ident,
        )


# --------------------------------------------------------------------------
# Prometheus Operator PrometheusRule
# --------------------------------------------------------------------------


def _check_prometheus_rule(obj: dict[str, Any], source: str, ctx: _RunContext) -> None:
    name = object_name(obj)
    spec = obj.get("spec")
    groups = spec.get("groups") if isinstance(spec, dict) else None
    if not isinstance(groups, list):
        ctx.add(source, "AR012", "PrometheusRule has no spec.groups list", name)
        return
    for gi, group in enumerate(groups):
        if not isinstance(group, dict):
            ctx.add(source, "AR012", f"group #{gi} is not a mapping", name)
            continue
        gname = group.get("name") or f"group#{gi}"
        if not group.get("name"):
            ctx.add(source, "AR012", f"group #{gi} has no name", name)
        if "interval" in group and not is_valid_duration(group["interval"]):
            ctx.add(
                source,
                "PR004",
                f"group interval {group['interval']!r} is not a valid duration",
                f"{name}/{gname}",
            )
        rules = group.get("rules")
        if not isinstance(rules, list):
            ctx.add(source, "AR012", f"group '{gname}' has no 'rules' list", name)
            continue
        seen_alerts: dict[str, int] = defaultdict(int)
        for ri, rule in enumerate(rules):
            if not isinstance(rule, dict):
                ctx.add(source, "AR012", f"rule #{ri} is not a mapping", f"{name}/{gname}")
                continue
            alert = rule.get("alert")
            record = rule.get("record")
            ident = str(alert or record or f"{gname}#{ri}")
            if bool(alert) == bool(record):
                ctx.add(
                    source,
                    "PR001",
                    "rule must have exactly one of 'alert' or 'record'",
                    ident,
                )
            expr = rule.get("expr")
            if isinstance(expr, (int, float)) and not isinstance(expr, bool):
                expr = str(expr)
            if not isinstance(expr, str) or not expr.strip():
                ctx.add(source, "PR002", "expr is missing or empty", ident)
            else:
                ctx.exprs.append(ExprRef(source, ident, expr, None))
            if alert:
                seen_alerts[str(alert)] += 1
            for key in ("for", "keep_firing_for"):
                if key in rule and not is_valid_duration(rule[key]):
                    ctx.add(
                        source,
                        "PR004",
                        f"'{key}' value {rule[key]!r} is not a valid duration",
                        ident,
                    )
            if alert:
                labels = rule.get("labels")
                severity = labels.get("severity") if isinstance(labels, dict) else None
                if severity is None or severity == "":
                    ctx.add(source, "PR005", "labels.severity is missing", ident)
                elif str(severity) not in ctx.options.severities:
                    ctx.add(
                        source,
                        "PR005",
                        f"labels.severity '{severity}' is not one of "
                        f"{', '.join(ctx.options.severities)}",
                        ident,
                    )
        for alert_name, count in seen_alerts.items():
            if count > 1:
                ctx.add(
                    source,
                    "PR003",
                    f"alert '{alert_name}' is defined {count} times in group '{gname}'",
                    alert_name,
                )
