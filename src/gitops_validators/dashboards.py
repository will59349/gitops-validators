"""Checks for Grafana dashboards: JSON files, dashboard ConfigMaps and rendered manifests.

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
    LEVEL_WARNING,
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

UID_MAX_LENGTH = 40
DEFAULT_LABEL = "grafana_dashboard"
SIZE_WARN_BYTES = 900 * 1024
SIZE_LIMIT_BYTES = 1024 * 1024

# Datasource references that are not real datasource uids.
BUILTIN_DATASOURCES = frozenset(
    {"-- Grafana --", "-- Mixed --", "-- Dashboard --", "grafana", "mixed", "dashboard"}
)
_EXPORT_PLACEHOLDER_RE = re.compile(r"\$\{DS_[A-Za-z0-9_]+\}")


@dataclass(frozen=True)
class DashboardOptions:
    datasource_uids: frozenset[str] = frozenset()
    label: str = DEFAULT_LABEL
    size_warn_bytes: int = SIZE_WARN_BYTES
    size_limit_bytes: int = SIZE_LIMIT_BYTES


@dataclass
class _RunContext:
    options: DashboardOptions
    findings: list[Finding] = field(default_factory=list)
    uids: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

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
        for uid, paths in self.uids.items():
            if len(paths) < 2:
                continue
            for path in paths:
                others = (
                    ", ".join(p for p in paths if p != path) or "another dashboard in this file"
                )
                self.add(
                    path,
                    "DB003",
                    f"uid '{uid}' is also used by {others}; Grafana keeps one dashboard per uid",
                    uid,
                )


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def check_files(
    files: Sequence[InputFile], options: DashboardOptions | None = None
) -> list[Finding]:
    ctx = _RunContext(options or DashboardOptions())
    for f in files:
        try:
            text = read_text(f.path)
        except (OSError, UnicodeDecodeError) as err:
            ctx.add(str(f.path), "DB001", f"cannot read file: {err}")
            continue
        check_text(text, str(f.path), ctx, is_json=f.is_json, explicit=f.explicit)
    ctx.finalize()
    return ctx.findings


def check_rendered(
    text: str, source: str, options: DashboardOptions | None = None
) -> list[Finding]:
    """Check one rendered manifest stream (for example ``helm template`` output)."""
    ctx = _RunContext(options or DashboardOptions())
    check_text(text, source, ctx, is_json=False, explicit=False)
    ctx.finalize()
    return ctx.findings


def check_text(text: str, source: str, ctx: _RunContext, *, is_json: bool, explicit: bool) -> None:
    if is_json:
        try:
            obj = parse_json(text)
        except json.JSONDecodeError as err:
            ctx.add(
                source,
                "DB001",
                f"file is not valid JSON ({json_error_message(err)}); Grafana cannot load it",
            )
            return
        if explicit or looks_like_dashboard(obj):
            _check_dashboard(obj, source, text, ctx)
        return
    try:
        docs = parse_yaml_documents(text)
    except yaml.YAMLError as err:
        ctx.add(source, "DB001", f"file is not valid YAML ({yaml_error_message(err)})")
        return
    for doc in docs:
        for obj in iter_kubernetes_objects(doc):
            if obj.get("kind") == "ConfigMap":
                _check_configmap(obj, source, ctx)
            elif "kind" not in obj and (explicit or looks_like_dashboard(obj)):
                # A dashboard written as YAML (rare, but Grafana accepts JSON only).
                _check_dashboard(obj, source, text, ctx)


def looks_like_dashboard(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get("dashboard"), dict):
        return True
    return any(key in obj for key in ("panels", "rows", "schemaVersion"))


# --------------------------------------------------------------------------
# ConfigMaps
# --------------------------------------------------------------------------


def _check_configmap(obj: dict[str, Any], source: str, ctx: _RunContext) -> None:
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
    labelled = ctx.options.label in labels
    data = obj.get("data")
    if not isinstance(data, dict):
        data = {}
    name = object_name(obj)

    for key, value in data.items():
        key = str(key)
        is_json_key = key.lower().endswith(".json")
        if not labelled and not is_json_key:
            continue
        label = embedded_source(source, name, key)
        if labelled and not is_json_key:
            ctx.add(
                label,
                "DB011",
                f"key '{key}' does not end with .json; the sidecar writes it to disk but "
                "Grafana's dashboard provisioner only loads *.json",
            )
        if not isinstance(value, str):
            ctx.add(label, "DB001", "ConfigMap value is not a string")
            continue
        try:
            dashboard = parse_json(value)
        except json.JSONDecodeError as err:
            ctx.add(
                label,
                "DB001",
                f"ConfigMap value is not valid JSON ({json_error_message(err)}); "
                "the dashboard is dropped at load time",
            )
            continue
        _check_dashboard(dashboard, label, value, ctx)

    if labelled or any(str(k).lower().endswith(".json") for k in data):
        _check_size(obj, data, source, name, ctx)


def _check_size(
    obj: dict[str, Any], data: dict[str, Any], source: str, name: str, ctx: _RunContext
) -> None:
    total = 0
    for key, value in data.items():
        total += len(str(key).encode("utf-8"))
        total += len(value.encode("utf-8")) if isinstance(value, str) else 0
    binary = obj.get("binaryData")
    if isinstance(binary, dict):
        for key, value in binary.items():
            total += len(str(key).encode("utf-8"))
            total += len(str(value).encode("utf-8"))
    label = f"{source}::{name}"
    kib = total / 1024
    if total > ctx.options.size_limit_bytes:
        ctx.add(
            label,
            "DB010",
            f"ConfigMap data is {kib:.0f} KiB; the Kubernetes limit is 1024 KiB "
            "and the apply fails",
        )
    elif total > ctx.options.size_warn_bytes:
        ctx.add(
            label,
            "DB010",
            f"ConfigMap data is {kib:.0f} KiB, close to the 1024 KiB Kubernetes limit",
            None,
            LEVEL_WARNING,
        )


# --------------------------------------------------------------------------
# Dashboard model
# --------------------------------------------------------------------------


def _check_dashboard(obj: Any, source: str, raw: str, ctx: _RunContext) -> None:
    if not isinstance(obj, dict):
        ctx.add(source, "DB001", "dashboard JSON must be an object")
        return
    if isinstance(obj.get("dashboard"), dict) and "panels" not in obj:
        obj = obj["dashboard"]
    opts = ctx.options

    uid = obj.get("uid")
    title = obj.get("title")
    ident = uid if isinstance(uid, str) and uid else (title if isinstance(title, str) else None)

    # DB002 / DB003: uid
    if uid is None or uid == "":
        ctx.add(
            source,
            "DB002",
            "uid is missing; Grafana assigns a random one, so links change on every re-provision",
            ident,
        )
    elif not isinstance(uid, str):
        ctx.add(source, "DB002", f"uid must be a string, got {type(uid).__name__}", ident)
    elif len(uid) > UID_MAX_LENGTH:
        ctx.add(
            source,
            "DB002",
            f"uid is {len(uid)} characters, the maximum is {UID_MAX_LENGTH}",
            ident,
        )
    else:
        ctx.uids[uid].append(source)

    # DB004: title
    if not isinstance(title, str) or not title.strip():
        ctx.add(source, "DB004", "title is missing", ident)

    # DB005: id
    if obj.get("id") is not None:
        ctx.add(
            source,
            "DB005",
            f"id is {obj['id']!r}; exported numeric ids collide on import, set it to null",
            ident,
        )

    # DB006: panel ids
    counts: dict[Any, int] = defaultdict(int)
    for panel in _iter_panels(obj):
        if "id" in panel:
            counts[panel["id"]] += 1
    dupes = sorted((str(k) for k, v in counts.items() if v > 1), key=str)
    if dupes:
        ctx.add(
            source,
            "DB006",
            f"panel id(s) used more than once: {', '.join(dupes)}",
            ident,
        )

    # DB007: export leftovers
    inputs = obj.get("__inputs")
    if isinstance(inputs, list) and inputs:
        ctx.add(
            source,
            "DB007",
            "'__inputs' is present; this is an export for sharing, not a provisionable dashboard",
            ident,
        )
    placeholders = sorted(set(_EXPORT_PLACEHOLDER_RE.findall(raw)))
    if placeholders:
        ctx.add(
            source,
            "DB007",
            f"unresolved export placeholder(s): {', '.join(placeholders)}",
            ident,
        )

    # DB008: schemaVersion
    if "schemaVersion" not in obj:
        ctx.add(source, "DB008", "schemaVersion is missing", ident)

    # DB009: datasource references
    if opts.datasource_uids:
        bad = sorted(
            {
                ref
                for ref in _iter_datasource_refs(obj)
                if ref not in opts.datasource_uids
                and ref not in BUILTIN_DATASOURCES
                and not _is_templated(ref)
            }
        )
        if bad:
            ctx.add(
                source,
                "DB009",
                f"datasource uid(s) not in the allowed set: {', '.join(bad)}",
                ident,
            )


def _iter_panels(obj: dict[str, Any]):
    stack: list[Any] = []
    stack.extend(obj.get("panels") or [])
    for row in obj.get("rows") or []:
        if isinstance(row, dict):
            stack.extend(row.get("panels") or [])
    while stack:
        panel = stack.pop()
        if not isinstance(panel, dict):
            continue
        yield panel
        stack.extend(panel.get("panels") or [])


def _datasource_uid(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        uid = value.get("uid")
        if isinstance(uid, str):
            return uid
    return None


def _iter_datasource_refs(obj: dict[str, Any]):
    for panel in _iter_panels(obj):
        ref = _datasource_uid(panel.get("datasource"))
        if ref:
            yield ref
        for target in panel.get("targets") or []:
            if isinstance(target, dict):
                ref = _datasource_uid(target.get("datasource"))
                if ref:
                    yield ref
    templating = obj.get("templating")
    if isinstance(templating, dict):
        for var in templating.get("list") or []:
            if isinstance(var, dict) and var.get("type") == "query":
                ref = _datasource_uid(var.get("datasource"))
                if ref:
                    yield ref


def _is_templated(ref: str) -> bool:
    return ref.startswith("$") or "${" in ref or ref.startswith("[[")
