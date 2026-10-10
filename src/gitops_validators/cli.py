"""Command-line entry points: check-alert-rules, check-dashboards, check-helm-values."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__, alert_rules, dashboards, helm_values, promquery
from .alert_rules import AlertRuleOptions, ExprRef
from .common import EXIT_USAGE, Finding, UsageError, collect_files, emit
from .dashboards import DashboardOptions
from .helm_values import HelmOptions

# --------------------------------------------------------------------------
# Shared argument groups
# --------------------------------------------------------------------------


def _add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text, one finding per line)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")


def _add_datasource_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--datasource-uid",
        action="append",
        default=[],
        metavar="UID",
        help="allowed datasource uid (repeatable); when given, every datasource "
        "reference must be in this set",
    )


def _add_alert_rule_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--severity",
        default=",".join(alert_rules.DEFAULT_SEVERITIES),
        metavar="LIST",
        help="comma-separated allowed values of labels.severity "
        f"(default: {','.join(alert_rules.DEFAULT_SEVERITIES)})",
    )
    parser.add_argument(
        "--require-annotation",
        action="append",
        default=None,
        metavar="NAME",
        help="annotation that every Grafana rule must set (repeatable; default: summary)",
    )


def _add_query_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("Prometheus query check (optional)")
    group.add_argument(
        "--prometheus-url",
        metavar="URL",
        help="send every PromQL expression to URL/api/v1/query and report rejected ones",
    )
    group.add_argument(
        "--query-datasource-uid",
        action="append",
        default=[],
        metavar="UID",
        help="only query expressions whose datasourceUid is in this set "
        "(default: the --datasource-uid set; PrometheusRule objects are always queried)",
    )
    group.add_argument(
        "--strict-query",
        action="store_true",
        help="treat an unreachable Prometheus or non-400 errors as failures",
    )
    group.add_argument(
        "--bearer-token-env",
        metavar="VAR",
        help="name of an environment variable holding a bearer token for Prometheus",
    )
    group.add_argument(
        "--query-timeout",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="HTTP timeout per query (default: 10)",
    )


def _alert_options(args: argparse.Namespace) -> AlertRuleOptions:
    severities = tuple(s.strip() for s in args.severity.split(",") if s.strip())
    if not severities:
        raise UsageError("--severity must list at least one value")
    annotations = (
        tuple(args.require_annotation)
        if args.require_annotation
        else alert_rules.DEFAULT_REQUIRED_ANNOTATIONS
    )
    return AlertRuleOptions(
        datasource_uids=frozenset(args.datasource_uid),
        severities=severities,
        required_annotations=annotations,
    )


def _dashboard_options(args: argparse.Namespace) -> DashboardOptions:
    return DashboardOptions(
        datasource_uids=frozenset(args.datasource_uid),
        label=args.dashboard_label,
    )


def _run_query_check(args: argparse.Namespace, exprs: list[ExprRef]) -> list[Finding]:
    if not args.prometheus_url:
        return []
    query_set = frozenset(args.query_datasource_uid) or frozenset(args.datasource_uid)
    return promquery.query_expressions(
        exprs,
        args.prometheus_url,
        query_datasource_uids=query_set,
        timeout=args.query_timeout,
        bearer_token_env=args.bearer_token_env,
        strict=args.strict_query,
    )


def _usage_error(parser: argparse.ArgumentParser, err: Exception) -> int:
    print(f"{parser.prog}: error: {err}", file=sys.stderr)
    return EXIT_USAGE


# --------------------------------------------------------------------------
# check-alert-rules
# --------------------------------------------------------------------------


def build_alert_rules_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check-alert-rules",
        description="Check Grafana alert rule provisioning files, rule ConfigMaps and "
        "PrometheusRule objects for mistakes that make Grafana skip the whole file.",
    )
    parser.add_argument("paths", nargs="+", metavar="PATH", help="file or directory")
    _add_output_args(parser)
    _add_datasource_arg(parser)
    _add_alert_rule_args(parser)
    _add_query_args(parser)
    return parser


def main_alert_rules(argv: Sequence[str] | None = None) -> int:
    parser = build_alert_rules_parser()
    args = parser.parse_args(argv)
    try:
        files = collect_files(args.paths)
        findings, exprs = alert_rules.check_files(files, _alert_options(args))
        findings.extend(_run_query_check(args, exprs))
    except UsageError as err:
        return _usage_error(parser, err)
    return emit(findings, args.format)


# --------------------------------------------------------------------------
# check-dashboards
# --------------------------------------------------------------------------


def build_dashboards_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check-dashboards",
        description="Check Grafana dashboard JSON files and dashboard ConfigMaps for "
        "mistakes that make the sidecar or Grafana drop them.",
    )
    parser.add_argument("paths", nargs="+", metavar="PATH", help="file or directory")
    _add_output_args(parser)
    _add_datasource_arg(parser)
    parser.add_argument(
        "--dashboard-label",
        default=dashboards.DEFAULT_LABEL,
        metavar="NAME",
        help="ConfigMap label that marks dashboards for the sidecar "
        f"(default: {dashboards.DEFAULT_LABEL})",
    )
    return parser


def main_dashboards(argv: Sequence[str] | None = None) -> int:
    parser = build_dashboards_parser()
    args = parser.parse_args(argv)
    try:
        files = collect_files(args.paths)
        findings = dashboards.check_files(files, _dashboard_options(args))
    except UsageError as err:
        return _usage_error(parser, err)
    return emit(findings, args.format)


# --------------------------------------------------------------------------
# check-helm-values
# --------------------------------------------------------------------------


def build_helm_values_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check-helm-values",
        description="Render a chart with each values file (helm template) and run the "
        "dashboard and alert-rule checks on the output.",
    )
    parser.add_argument("chart", metavar="CHART_PATH", help="chart directory")
    parser.add_argument("values", nargs="+", metavar="VALUES_FILE", help="values file")
    _add_output_args(parser)
    _add_datasource_arg(parser)
    _add_alert_rule_args(parser)
    _add_query_args(parser)
    parser.add_argument(
        "--dashboard-label",
        default=dashboards.DEFAULT_LABEL,
        metavar="NAME",
        help=f"ConfigMap label that marks dashboards (default: {dashboards.DEFAULT_LABEL})",
    )
    helm = parser.add_argument_group("helm template")
    helm.add_argument("--release", default="release", help="release name (default: release)")
    helm.add_argument("--namespace", help="release namespace")
    helm.add_argument("--kube-version", metavar="VERSION", help="passed to helm template")
    helm.add_argument(
        "--dependency-update",
        action="store_true",
        help="pass --dependency-update to helm template",
    )
    helm.add_argument(
        "--extra-set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="extra --set value passed to helm template (repeatable)",
    )
    helm.add_argument("--helm-binary", default="helm", metavar="PATH", help="helm executable")
    helm.add_argument(
        "--helm-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="timeout for one helm template run (default: 300)",
    )
    return parser


def main_helm_values(argv: Sequence[str] | None = None) -> int:
    parser = build_helm_values_parser()
    args = parser.parse_args(argv)
    helm_opts = HelmOptions(
        release=args.release,
        namespace=args.namespace,
        kube_version=args.kube_version,
        dependency_update=args.dependency_update,
        extra_set=tuple(args.extra_set),
        helm_binary=args.helm_binary,
        timeout=args.helm_timeout,
    )
    try:
        findings, exprs = helm_values.check_helm_values(
            args.chart,
            args.values,
            helm_opts,
            _dashboard_options(args),
            _alert_options(args),
        )
        findings.extend(_run_query_check(args, exprs))
    except UsageError as err:
        return _usage_error(parser, err)
    return emit(findings, args.format)


# --------------------------------------------------------------------------
# python -m gitops_validators <command> ...
# --------------------------------------------------------------------------

COMMANDS = {
    "check-alert-rules": main_alert_rules,
    "check-dashboards": main_dashboards,
    "check-helm-values": main_helm_values,
}


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print(
            "usage: python -m gitops_validators {" + "|".join(COMMANDS) + "} [options] PATH...",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return COMMANDS[argv[0]](argv[1:])
