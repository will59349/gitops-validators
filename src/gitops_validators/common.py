"""Shared types and helpers: findings, file discovery, parsing, output."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

YAML_EXTENSIONS = (".yaml", ".yml")
JSON_EXTENSIONS = (".json",)
EXTENSIONS = YAML_EXTENSIONS + JSON_EXTENSIONS

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

# Every check id the tools can report (docs/checks.md). Used to validate --disable.
CHECK_IDS: frozenset[str] = frozenset(
    [f"AR{n:03d}" for n in range(1, 15)]
    + [f"PR{n:03d}" for n in range(1, 6)]
    + ["AQ001", "AQ002"]
    + [f"DB{n:03d}" for n in range(1, 12)]
    + ["HV001"]
)


class UsageError(Exception):
    """Wrong invocation (missing path, missing tool). Exit code 2."""


@dataclass
class Finding:
    """One problem found in one input.

    ``path`` is the file (or ``file::configmap/key`` for embedded content),
    ``ident`` the rule uid / dashboard uid when known, ``check`` the id listed
    in docs/checks.md.
    """

    path: str
    check: str
    message: str
    ident: str | None = None
    level: str = LEVEL_ERROR

    def format_text(self) -> str:
        ident = self.ident if self.ident else "-"
        level = "" if self.level == LEVEL_ERROR else f" ({self.level})"
        return f"{self.path}: [{ident}] {self.check}{level}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InputFile:
    """A file to check. ``explicit`` is True when it was named on the command line
    rather than found by walking a directory."""

    path: Path
    explicit: bool

    @property
    def is_json(self) -> bool:
        return self.path.suffix.lower() in JSON_EXTENSIONS


def collect_files(paths: Sequence[str | Path]) -> list[InputFile]:
    """Expand the positional PATH arguments into files.

    Files are taken as given (any extension). Directories are walked
    recursively; only ``.yaml``, ``.yml`` and ``.json`` files are kept and
    hidden directories (``.git`` and friends) are skipped.
    """
    if not paths:
        raise UsageError("no PATH given")
    files: list[InputFile] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            if p not in seen:
                seen.add(p)
                files.append(InputFile(p, explicit=True))
        elif p.is_dir():
            for child in sorted(p.rglob("*")):
                if not child.is_file():
                    continue
                if any(part.startswith(".") for part in child.relative_to(p).parts):
                    continue
                if child.suffix.lower() not in EXTENSIONS:
                    continue
                if child not in seen:
                    seen.add(child)
                    files.append(InputFile(child, explicit=False))
        else:
            raise UsageError(f"path does not exist: {raw}")
    return files


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def yaml_error_message(err: BaseException) -> str:
    """Short, single-line description of a YAML parse error."""
    if isinstance(err, yaml.MarkedYAMLError):
        parts = []
        if err.problem:
            parts.append(err.problem)
        if err.problem_mark is not None:
            parts.append(f"line {err.problem_mark.line + 1}, column {err.problem_mark.column + 1}")
        if parts:
            return ", ".join(parts)
    return " ".join(str(err).split())


def parse_yaml_documents(text: str) -> list[Any]:
    """Parse a (possibly multi-document) YAML string. Raises yaml.YAMLError."""
    return [doc for doc in yaml.safe_load_all(text) if doc is not None]


def parse_json(text: str) -> Any:
    """Parse JSON. Raises json.JSONDecodeError."""
    return json.loads(text)


def json_error_message(err: json.JSONDecodeError) -> str:
    return f"{err.msg} at line {err.lineno}, column {err.colno}"


def embedded_source(path: str, name: str, key: str) -> str:
    """Path label for content embedded in a Kubernetes object."""
    return f"{path}::{name}/{key}"


def object_name(doc: dict[str, Any]) -> str:
    meta = doc.get("metadata")
    if not isinstance(meta, dict):
        return "<unnamed>"
    name = meta.get("name") or "<unnamed>"
    namespace = meta.get("namespace")
    return f"{namespace}/{name}" if namespace else str(name)


def iter_kubernetes_objects(doc: Any) -> Iterable[dict[str, Any]]:
    """Yield Kubernetes objects from a document, unwrapping ``kind: List``."""
    if not isinstance(doc, dict):
        return
    if doc.get("kind") == "List" and isinstance(doc.get("items"), list):
        for item in doc["items"]:
            if isinstance(item, dict):
                yield item
        return
    yield doc


def parse_disabled(values: Sequence[str] | None) -> frozenset[str]:
    """Turn ``--disable AR009,AR010 --disable db005`` into a set of check ids.

    Raises UsageError for an id that does not exist, so a typo cannot silently
    leave a check enabled.
    """
    disabled: set[str] = set()
    for raw in values or ():
        for item in raw.split(","):
            check = item.strip().upper()
            if not check:
                continue
            if check not in CHECK_IDS:
                raise UsageError(f"--disable: unknown check id '{item.strip()}'")
            disabled.add(check)
    return frozenset(disabled)


def drop_disabled(findings: Iterable[Finding], disabled: frozenset[str]) -> list[Finding]:
    return [f for f in findings if f.check not in disabled]


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(f.level == LEVEL_ERROR for f in findings)


def emit(findings: list[Finding], fmt: str, stream: TextIO | None = None) -> int:
    """Print findings in the requested format and return the exit code."""
    out = stream or sys.stdout
    ordered = sorted(findings, key=lambda f: (f.path, f.check, f.ident or "", f.message))
    errors = sum(1 for f in ordered if f.level == LEVEL_ERROR)
    warnings = len(ordered) - errors
    if fmt == "json":
        payload = {
            "findings": [f.to_dict() for f in ordered],
            "errors": errors,
            "warnings": warnings,
        }
        out.write(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    else:
        for f in ordered:
            out.write(f.format_text() + "\n")
        if ordered:
            out.write(f"{errors} error(s), {warnings} warning(s)\n")
    return EXIT_FINDINGS if errors else EXIT_CLEAN
