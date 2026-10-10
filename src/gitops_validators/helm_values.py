"""Render a chart with each values file and run the dashboard and alert-rule
checks on the output."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import alert_rules, dashboards
from .alert_rules import AlertRuleOptions, ExprRef
from .common import Finding, UsageError
from .dashboards import DashboardOptions


@dataclass(frozen=True)
class HelmOptions:
    release: str = "release"
    namespace: str | None = None
    kube_version: str | None = None
    dependency_update: bool = False
    extra_set: tuple[str, ...] = ()
    helm_binary: str = "helm"
    timeout: float = 300.0


def helm_template_command(chart: str, values_file: str, opts: HelmOptions) -> list[str]:
    cmd = [opts.helm_binary, "template", opts.release, chart, "-f", values_file]
    if opts.namespace:
        cmd += ["--namespace", opts.namespace]
    if opts.kube_version:
        cmd += ["--kube-version", opts.kube_version]
    if opts.dependency_update:
        cmd.append("--dependency-update")
    for item in opts.extra_set:
        cmd += ["--set", item]
    return cmd


def render(chart: str, values_file: str, opts: HelmOptions) -> tuple[bool, str]:
    """Run ``helm template``. Returns (ok, stdout-or-stderr)."""
    if shutil.which(opts.helm_binary) is None:
        raise UsageError(
            f"'{opts.helm_binary}' not found in PATH; install Helm 3 or pass --helm-binary"
        )
    cmd = helm_template_command(chart, values_file, opts)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=opts.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"helm template timed out after {opts.timeout:.0f}s"
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        return False, err
    return True, proc.stdout


def check_helm_values(
    chart: str,
    values_files: Sequence[str],
    helm_opts: HelmOptions | None = None,
    dashboard_opts: DashboardOptions | None = None,
    alert_opts: AlertRuleOptions | None = None,
) -> tuple[list[Finding], list[ExprRef]]:
    """Render ``chart`` once per values file and check the output.

    Findings are labelled ``<values file> (rendered)`` so the reader knows which
    input produced them. Each values file is a separate release, so uid
    uniqueness is checked per values file, not across them.
    """
    helm_opts = helm_opts or HelmOptions()
    chart_path = Path(chart)
    if not chart_path.is_dir() or not (chart_path / "Chart.yaml").is_file():
        raise UsageError(f"not a chart directory (no Chart.yaml): {chart}")
    if not values_files:
        raise UsageError("no VALUES_FILE given")
    findings: list[Finding] = []
    exprs: list[ExprRef] = []
    for values_file in values_files:
        if not Path(values_file).is_file():
            raise UsageError(f"values file does not exist: {values_file}")
        ok, output = render(chart, values_file, helm_opts)
        if not ok:
            findings.append(Finding(values_file, "HV001", f"helm template failed: {output}"))
            continue
        source = f"{values_file} (rendered)"
        findings.extend(dashboards.check_rendered(output, source, dashboard_opts))
        rule_findings, rule_exprs = alert_rules.check_rendered(output, source, alert_opts)
        findings.extend(rule_findings)
        exprs.extend(rule_exprs)
    return findings, exprs
