# gitops-validators

Pre-merge checks for a GitOps repository that carries Grafana alert rules,
Grafana dashboards and kube-prometheus-stack values. They catch the mistakes
that make Grafana refuse provisioned alert rules or drop a dashboard, before
the change is merged.

## Status

Version 0.1, early. The method comes from production use; this code is a generic rewrite of it and has not itself run in production. The checks are
exercised by the test suite against the fixtures in `tests/fixtures` (a failing
sample for every AR, PR and DB id; the one for DB010 is generated in the test),
a real `helm template` of the fixture chart, and a local fake Prometheus for
the query check. They read files and exit non-zero; they change nothing.
`check-helm-values` runs `helm template`, and with `--dependency-update` Helm
downloads the chart's dependencies into the chart directory.

Version 0.1.0 was run once against a real multi-environment GitOps chart and
rendered every values file. Its findings there led to the levels in 0.1.1:
groups without rules, dashboards without a uid and exported dashboard ids are
warnings, because Grafana loads all three.

What the checks say about Grafana was read from the Grafana source of v10.4.0
and v11.6.0; [docs/checks.md](docs/checks.md) names the version wherever
behaviour differs, and says which statements were not checked. No check was
run against a live Grafana. A run without findings does not mean a rule is
correct, and it does not prove that Grafana will load it: the checks cover the
mistakes listed in docs/checks.md, not all of Grafana's validation.

## Why

Grafana file provisioning is unforgiving about alert rule files. In the
versions checked, one YAML syntax error, one missing `folder` or one invalid
duration in any file under `provisioning/alerting` makes the whole alerting
provisioning run fail: none of the rule files is applied, a reload returns an
error that only shows up in a log, and a restarted Grafana does not start.
Dashboards shipped as ConfigMaps fail more quietly when the JSON inside the
ConfigMap is not valid: `kubectl apply` accepts any string, the file lands on
disk, Grafana logs a parse error and moves on without the dashboard. Helm
values that do not render are only noticed when the sync fails. In all three
cases the pipeline of the pull request stays green.

These checks run on the pull request instead. They are static (no Grafana
needed), and report one finding per line with a stable id.

## Install

There is no PyPI package; install from the repository:

```bash
pipx install "git+https://github.com/will59349/gitops-validators@v0.1.1"
# or
pip install "git+https://github.com/will59349/gitops-validators@v0.1.1"
```

Requires Python 3.9 or newer (CI runs the tests on 3.9 and 3.12) and `git`
for the install. `check-helm-values` also needs `helm` 3 in `PATH`.

As pre-commit hooks:

```yaml
repos:
  - repo: https://github.com/will59349/gitops-validators
    rev: v0.1.1
    hooks:
      - id: check-alert-rules
        files: ^grafana/alert-rules/
        args: [--datasource-uid, prometheus-main]
      - id: check-dashboards
        files: ^grafana/dashboards/
        args: [--datasource-uid, prometheus-main]
      - id: check-helm-values
        args: [charts/monitoring, charts/monitoring/values.yaml]
```

`check-alert-rules` and `check-dashboards` receive the changed YAML and JSON
files; restrict them with `files:` to where rules and dashboards live. Because
uid uniqueness is checked only among the files in one run, use
`pass_filenames: false` with the directory in `args` when the whole tree must
be checked on every commit. `check-helm-values` runs on every commit and needs
its chart and values files in `args`.

## Commands

`check-alert-rules` and `check-dashboards` take files or directories
(directories are walked recursively for `.yaml`, `.yml` and `.json`; hidden
directories are skipped). `check-helm-values` takes a chart directory and one
or more values files. All three print one finding per line as
`path: [rule-or-dashboard-id] ID: message`, then a count line when there are
findings, and exit with

| Exit code | Meaning |
|-----------|---------|
| 0 | no errors (warnings may have been printed) |
| 1 | at least one error-level finding |
| 2 | usage error: path does not exist, helm missing, bad option, unknown id in `--disable` |

`--format json` prints `{"findings": [...], "errors": n, "warnings": n}`.
`--disable ID[,ID]` (repeatable) leaves the named checks out of the report and
the exit code.

### Convention checks

Three checks enforce a house convention, not a Grafana or Prometheus
requirement. They are errors by default; each can be changed or turned off:

| ID | Convention | Change it | Turn it off |
|----|------------|-----------|-------------|
| AR009 | every Grafana rule has `labels.severity` with one of `critical`, `warning`, `info` | `--severity page,ticket` | `--disable AR009` |
| AR010 | every Grafana rule has a `summary` annotation | `--require-annotation NAME` (repeatable, replaces the default) | `--disable AR010` |
| PR005 | every `PrometheusRule` alert has `labels.severity` from the same set | `--severity` | `--disable PR005` |

### check-alert-rules

```bash
check-alert-rules grafana/alert-rules/ \
  --datasource-uid prometheus-main --datasource-uid loki-main \
  --severity critical,warning,info \
  --require-annotation summary --require-annotation runbook_url
```

Accepts, in any mix:

* Grafana provisioning YAML (`apiVersion: 1`, `groups: [...]`);
* the same content embedded in a ConfigMap `data` key ending in `.yaml`/`.yml`,
  including multi-document files and rendered Helm output;
* Prometheus Operator `PrometheusRule` objects.

Example output:

```
grafana/alert-rules/broken.yaml: [-] AR001: file is not valid YAML (mapping values are not allowed here, line 17, column 19); Grafana cannot parse it, and alert provisioning fails as a whole until it is fixed
grafana/alert-rules/node.yaml: [node-cpu-high] AR003: uid 'node-cpu-high' is also used in grafana/alert-rules/old.yaml; Grafana keeps only one of them
grafana/alert-rules/node.yaml: [node-cpu-high] AR006: 'for' value '5 minutes' is not a valid duration (e.g. 5m, 1h30m)
grafana/alert-rules/old.yaml: [node-cpu-high] AR003: uid 'node-cpu-high' is also used in grafana/alert-rules/node.yaml; Grafana keeps only one of them
4 error(s), 0 warning(s)
```

Optional semantic check against a live Prometheus:

```bash
check-alert-rules grafana/alert-rules/ \
  --prometheus-url https://prometheus.staging.example.com \
  --bearer-token-env PROMETHEUS_TOKEN \
  --query-datasource-uid prometheus-main \
  --strict-query
```

Every rule expression is sent to `/api/v1/query`; an expression Prometheus
rejects (400/422) is reported as `AQ001`. Connection problems are warnings
unless `--strict-query`. The token is read from the named environment
variable, never from the command line. Rule files that also contain Loki or
other non-Prometheus queries need `--query-datasource-uid` with the Prometheus
datasource uids, as above; without it every query is sent to Prometheus.

### check-dashboards

```bash
check-dashboards grafana/dashboards/ --datasource-uid prometheus-main
```

Accepts dashboard JSON files, ConfigMaps labelled `grafana_dashboard` (or any
ConfigMap with a `data` key ending in `.json`), and rendered multi-document
manifests. Files found by walking a directory are only checked when they look
like a dashboard (`panels`, `rows` or `schemaVersion` present); files named
explicitly are always checked, unless `--skip-non-dashboards` is given (the
pre-commit hook sets it).

```
grafana/dashboards/api.json: [api-latency] DB005 (warning): id is 42; file provisioning ignores it, but an import through the HTTP API addresses the dashboard with that database id; set it to null
grafana/dashboards/node.yaml::monitoring/grafana-dashboards/node.json: [-] DB001: ConfigMap value is not valid JSON (Expecting property name enclosed in double quotes at line 9, column 7); the dashboard is dropped at load time
1 error(s), 1 warning(s)
```

### check-helm-values

```bash
check-helm-values charts/monitoring charts/monitoring/values-*.yaml \
  --dependency-update --kube-version 1.30.0 \
  --extra-set grafana.enabled=true \
  --datasource-uid prometheus-main
```

Runs `helm template <release> CHART_PATH -f VALUES` for each values file
(`--release`, `--namespace`, `--kube-version`, `--dependency-update` and
`--extra-set KEY=VALUE` are passed through), reports a render failure with
Helm's stderr as `HV001`, and otherwise runs the dashboard and alert-rule
checks on the rendered output. The datasource, severity, annotation, label,
query and `--disable` options of the other two commands apply here too.
Findings are prefixed with the values file:

```
charts/monitoring/values-prod.yaml (rendered)::monitoring-dashboards/node-overview.json: [-] DB001: ConfigMap value is not valid JSON (...); the dashboard is dropped at load time
```

A chart that renders a group without rules for some environment gets an
`AR013` warning for it; that does not fail the run.

## Check ids

Full descriptions, with the level of each check, what it catches and what the
statement about Grafana is based on, are in [docs/checks.md](docs/checks.md).

| Range | Scope | Examples |
|-------|-------|----------|
| AR001-AR014 | Grafana alert rules | file not parseable (AR001), uid invalid (AR002) or duplicate (AR003), bad `for`/`interval` (AR006), condition refId missing (AR007), datasource uid unknown (AR008), noData/execErr states (AR011), structure (AR012); convention: severity (AR009), annotations (AR010); warnings: group without rules (AR013), rule without `for` (AR014) |
| PR001-PR005 | `PrometheusRule` objects | alert xor record, expr present, duplicate alert names, durations; convention: severity (PR005) |
| AQ001-AQ002 | Prometheus query check | expression rejected by Prometheus; Prometheus unreachable |
| DB001-DB011 | Grafana dashboards | JSON not valid (DB001), uid too long or malformed (DB002) or duplicate (DB003), title missing (DB004), duplicate panel ids (DB006), export placeholders (DB007), datasource uid unknown (DB009), ConfigMap size (DB010), key not `.json` (DB011); warnings: uid missing (DB002), `id` not null (DB005), `schemaVersion` missing (DB008) |
| HV001 | Helm values | `helm template` failed |

## CI examples

* [examples/gitlab-ci.yml](examples/gitlab-ci.yml): three jobs on merge requests, plus an optional semantic job.
* [examples/github-actions.yml](examples/github-actions.yml): the same as a GitHub Actions workflow.

Both assume a layout like `grafana/alert-rules/`, `grafana/dashboards/` and
`charts/monitoring/`; adjust the paths. Neither example has been run in a real
pipeline.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
tests/run.sh          # ruff check, ruff format --check, pytest, bash -n, shellcheck
```

`tests/run.sh` reports tools that are not installed as skipped. The Helm tests
are skipped when `helm` is not in `PATH`. The CI workflow runs ruff and pytest
on Python 3.9 and 3.12, shellcheck, and `check-helm-values` on the fixture
chart with a real Helm.

## Limitations

* The checks validate structure, not logic. A rule whose threshold is wrong,
  or a dashboard that queries the wrong metric, passes.
* They are not a reimplementation of Grafana's validation. A file can pass
  here and still be refused by Grafana (for example for a missing
  `relativeTimeRange` on a query).
* The Prometheus query check (`--prometheus-url`) needs network access to a
  Prometheus that has the same metric names and labels as production;
  otherwise an expression can be accepted here and return nothing there. It
  skips expressions with unresolved template variables, and it cannot tell
  PromQL from other query languages.
* Grafana's own validation changes between versions. What is encoded here
  (40-character uids, the state names, the duration grammar, mandatory
  fields) was read from the Grafana 10.4.0 and 11.6.0 sources; other versions
  may accept more or less.
* Only ConfigMap values under keys ending in `.yaml`/`.yml` (rules) or
  `.json` (dashboards) are inspected. Secrets and `binaryData` are ignored.
* A plain Prometheus rule file (top-level `groups:`, no `kind`) is read as a
  Grafana provisioning file. Only `PrometheusRule` objects are supported for
  Prometheus rules.
* `check-helm-values` needs the chart's dependencies to be resolvable
  (`--dependency-update` fetches them) and does not evaluate `lookup` calls
  against a cluster.

## License

MIT. See [LICENSE](LICENSE).
