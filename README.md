# gitops-validators

Pre-merge checks for a GitOps repository that carries Grafana alert rules,
Grafana dashboards and kube-prometheus-stack values. They catch the mistakes
that make a whole file silently disappear from Grafana.

## Why

Grafana file provisioning loads a rule file as a unit. One YAML syntax error,
one duplicate uid or one uid over 40 characters, and the whole file is skipped
at load time with a log line nobody reads; the alerts vanish while the
pipeline stays green. Dashboards shipped as ConfigMaps fail the same way when
the JSON inside the ConfigMap is not valid: `kubectl apply` accepts any
string, the sidecar writes it to disk, Grafana logs a parse error and moves
on. Helm values that do not render are only noticed when the sync fails.

These checks run on the pull request instead. They are static (no Grafana
needed), fast, and report one finding per line with a stable id.

## Install

```bash
pipx install git+https://github.com/will59349/gitops-validators
# or
pip install git+https://github.com/will59349/gitops-validators
```

Requires Python 3.9+. `check-helm-values` also needs `helm` 3 in `PATH`.

As pre-commit hooks:

```yaml
repos:
  - repo: https://github.com/will59349/gitops-validators
    rev: v0.1.0
    hooks:
      - id: check-alert-rules
        args: [--datasource-uid, prometheus-main]
      - id: check-dashboards
        args: [--datasource-uid, prometheus-main]
      - id: check-helm-values
        args: [charts/monitoring, charts/monitoring/values.yaml]
```

`check-alert-rules` and `check-dashboards` receive the changed files. Because
uid uniqueness is checked only among the files in one run, use
`pass_filenames: false` with the directory in `args` when the whole tree must
be checked on every commit.

## Commands

All three commands take files or directories (recursive, `.yaml`, `.yml`,
`.json`), print one finding per line as
`path: [rule-or-dashboard-id] ID: message`, and exit with

| Exit code | Meaning |
|-----------|---------|
| 0 | no errors (warnings may have been printed) |
| 1 | at least one error-level finding |
| 2 | usage error: path does not exist, helm missing, bad option |

`--format json` prints `{"findings": [...], "errors": n, "warnings": n}`.

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
grafana/alert-rules/node.yaml: [node-cpu-high] AR006: 'for' value '5 minutes' is not a valid duration (e.g. 5m, 1h30m)
grafana/alert-rules/node.yaml: [node-cpu-high] AR003: uid 'node-cpu-high' is also used in grafana/alert-rules/old.yaml; Grafana keeps only one of them
grafana/alert-rules/broken.yaml: [-] AR001: file is not valid YAML (mapping values are not allowed here, line 17, column 19); Grafana skips the whole file and every rule in it
3 error(s), 0 warning(s)
```

Optional semantic check against a live Prometheus:

```bash
check-alert-rules grafana/alert-rules/ \
  --prometheus-url https://prometheus.staging.example.com \
  --bearer-token-env PROMETHEUS_TOKEN \
  --query-datasource-uid prometheus-main \
  --strict-query
```

Every PromQL expression is sent to `/api/v1/query`; an expression Prometheus
rejects (400/422) is reported as `AQ001`. Connection problems are warnings
unless `--strict-query`. The token is read from the named environment
variable, never from the command line.

### check-dashboards

```bash
check-dashboards grafana/dashboards/ --datasource-uid prometheus-main
```

Accepts dashboard JSON files, ConfigMaps labelled `grafana_dashboard` (or any
ConfigMap with a `data` key ending in `.json`), and rendered multi-document
manifests. Files found by walking a directory are only checked when they look
like a dashboard (`panels`, `rows` or `schemaVersion` present); files named
explicitly are always checked.

```
grafana/dashboards/node.yaml::monitoring/grafana-dashboards/node.json: [-] DB001: ConfigMap value is not valid JSON (Expecting property name enclosed in double quotes at line 9, column 7); the dashboard is dropped at load time
grafana/dashboards/api.json: [api-latency] DB005: id is 42; exported numeric ids collide on import, set it to null
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
checks on the rendered output. Findings are prefixed with the values file:

```
charts/monitoring/values-prod.yaml (rendered)::monitoring-dashboards/node-overview.json: [-] DB001: ConfigMap value is not valid JSON (...)
```

## Check ids

Full descriptions, with what each check catches and why it matters, are in
[docs/checks.md](docs/checks.md).

| Range | Scope | Examples |
|-------|-------|----------|
| AR001-AR012 | Grafana alert rules | file not parseable (AR001), uid invalid (AR002) or duplicate (AR003), bad `for`/`interval` (AR006), condition refId missing (AR007), datasource uid unknown (AR008), severity (AR009), annotations (AR010), noData/execErr states (AR011) |
| PR001-PR005 | `PrometheusRule` objects | alert xor record, expr present, duplicate alert names, durations, severity |
| AQ001-AQ002 | Prometheus query check | expression rejected by Prometheus; Prometheus unreachable |
| DB001-DB011 | Grafana dashboards | JSON not valid (DB001), uid missing/long (DB002) or duplicate (DB003), `id` not null (DB005), duplicate panel ids (DB006), export placeholders (DB007), `schemaVersion` (DB008), datasource uid unknown (DB009), ConfigMap size (DB010), key not `.json` (DB011) |
| HV001 | Helm values | `helm template` failed |

## CI examples

* [examples/gitlab-ci.yml](examples/gitlab-ci.yml): three jobs on merge requests, plus an optional semantic job.
* [examples/github-actions.yml](examples/github-actions.yml): the same as a GitHub Actions workflow.

Both assume a layout like `grafana/alert-rules/`, `grafana/dashboards/` and
`charts/monitoring/`; adjust the paths.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
tests/run.sh          # ruff check, ruff format --check, pytest, bash -n, shellcheck
```

`tests/run.sh` reports tools that are not installed as skipped; CI runs all of
them. The Helm tests are skipped when `helm` is not in `PATH`.

## Limitations

* The checks validate structure, not logic. A rule whose threshold is wrong,
  or a dashboard that queries the wrong metric, passes.
* The Prometheus query check (`--prometheus-url`) needs network access to a
  Prometheus that has the same metric names and labels as production;
  otherwise an expression can be accepted here and return nothing there. It
  also only sees expressions without unresolved template variables.
* Grafana's own validation changes between versions. The limits encoded here
  (40-character uids, the state names, duration grammar) match Grafana 10 and
  11; a newer Grafana may accept more or less.
* Only ConfigMap values under keys ending in `.yaml`/`.yml` (rules) or
  `.json` (dashboards) are inspected. Secrets and `binaryData` are ignored.
* `check-helm-values` needs the chart's dependencies to be resolvable
  (`--dependency-update` fetches them) and does not evaluate `lookup` calls
  against a cluster.

## License

MIT. See [LICENSE](LICENSE).
