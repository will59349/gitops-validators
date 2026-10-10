# Check reference

Every finding carries one of the ids below. Output format:

```
<path>: [<rule uid | dashboard uid | ->] <ID>: <message>
<path>: [<ident>] <ID> (warning): <message>
```

For content embedded in a Kubernetes object the path is
`<file>::<namespace/name>/<key>`; for rendered Helm output it is
`<values file> (rendered)::<name>/<key>`.

Errors set exit code 1. Warnings are printed but do not change the exit code.

## Grafana alert rules (`check-alert-rules`)

Applies to Grafana provisioning files (`apiVersion: 1` + `groups`), the same
content as a `.yaml`/`.yml` value inside a ConfigMap, and rendered manifests.

| ID | What it catches | Why it matters |
|----|-----------------|----------------|
| AR001 | The file (or the ConfigMap value) is not valid YAML/JSON. | Grafana's file provisioner loads a rule file as one unit. A syntax error anywhere skips the whole file: every rule in it disappears, and the only trace is a log line at startup. |
| AR002 | `uid` missing, empty, longer than 40 characters, or containing characters other than `A-Z a-z 0-9 - _`. | Provisioned rules must have a uid; an invalid one fails validation of the file. Over 40 characters is rejected by Grafana's uid validator. |
| AR003 | The same `uid` appears more than once across all files in the run. Both locations are reported. | Grafana keeps one rule per uid. The second file to load overwrites the first, or the provisioning run fails; either way one rule silently vanishes. |
| AR004 | `title` missing or blank. | Required by Grafana; the file fails validation. |
| AR005 | The same `title` appears twice in the same folder (and org). | Grafana requires unique titles per folder; the file fails validation. |
| AR006 | `for` (rule) or `interval` (group) is not a Prometheus-style duration (`30s`, `5m`, `1h30m`). | `5 minutes` or a bare `5` cannot be parsed, and the whole file is rejected. |
| AR007 | `condition` missing, or not equal to any `refId` in `data`. | The rule would evaluate a query that does not exist. Grafana rejects it. |
| AR008 | A `data` item has no `datasourceUid`, or (with `--datasource-uid`) uses one outside the allowed set. `__expr__` is always allowed. | A datasource uid that does not exist in the target Grafana makes every evaluation fail with `Error` state; it is the most common copy-paste mistake between environments. |
| AR009 | `labels.severity` missing or not in the allowed set (`--severity`, default `critical,warning,info`). | Routing in Alertmanager and on-call policies key on this label. A typo (`crtical`) routes the alert nowhere. |
| AR010 | A required annotation is missing (`--require-annotation`, default `summary`). | Notifications without a summary are not actionable. |
| AR011 | `noDataState` or `execErrState` is not one of the values Grafana accepts (`NoData`, `Alerting`, `OK`, `KeepLast` / `Error`, `Alerting`, `OK`, `KeepLast`). | Rejected at validation time; the whole file is skipped. |
| AR012 | Structural problems: `groups` is not a list, a group has no `name`, `folder` or `rules` list, a rule or data item is not a mapping, `data` is empty, a data item has no `refId`. | Each of these fails Grafana's validation of the file. `folder` is easy to forget and is mandatory for file provisioning. |

## Prometheus Operator `PrometheusRule` (`check-alert-rules`)

| ID | What it catches | Why it matters |
|----|-----------------|----------------|
| PR001 | A rule has both `alert` and `record`, or neither. | The operator rejects the object; Prometheus never loads the group. |
| PR002 | `expr` missing or empty. | Same: the group fails to load. |
| PR003 | The same `alert` name appears more than once in one group. | Not rejected by Prometheus, but the two alerts share a name in Alertmanager and are grouped and silenced together; usually a copy-paste error. |
| PR004 | `for`, `keep_firing_for` or group `interval` is not a valid duration. | The group fails to load. |
| PR005 | An alert has no `labels.severity`, or its value is not in the allowed set. | Same reason as AR009. |

## Prometheus query check (`--prometheus-url`)

Optional. Each PromQL expression is sent to `POST <url>/api/v1/query` with
`time=now`. Grafana macros (`$__rate_interval`, `$__interval`, `$__range`,
...) are replaced with constants first; expressions that still contain
template variables are skipped. With `--query-datasource-uid` only Grafana
rules whose `datasourceUid` is in that set are queried (default: the
`--datasource-uid` set, or everything). `PrometheusRule` expressions are
always queried. Identical expressions are sent once.

| ID | What it catches | Why it matters |
|----|-----------------|----------------|
| AQ001 | Prometheus answered 400 or 422: the expression does not parse or cannot be evaluated (unknown function, unbalanced parentheses, many-to-many matching). | The rule will sit in `Error` state forever. Static checks cannot see this; a real Prometheus can. |
| AQ002 | Prometheus could not be reached, or returned another error (401, 5xx). Warning, or error with `--strict-query`. | A broken check should not block merges by default, but a pipeline that must prove the query ran can opt in. |

A token for Prometheus is read from the environment variable named by
`--bearer-token-env`; the value never appears on the command line.

## Grafana dashboards (`check-dashboards`)

Applies to dashboard JSON files, ConfigMaps carrying the sidecar label
(`--dashboard-label`, default `grafana_dashboard`) or any `data` key ending
in `.json`, and rendered manifests. API-export wrappers
(`{"dashboard": {...}, "overwrite": true}`) are unwrapped.

| ID | What it catches | Why it matters |
|----|-----------------|----------------|
| DB001 | The file or ConfigMap value is not valid JSON (or the manifest is not valid YAML). | The sidecar writes the file as-is; Grafana's provisioner fails to parse it and logs an error. The dashboard is simply absent, and `kubectl apply` was happy because a ConfigMap value is just a string. |
| DB002 | `uid` missing, not a string, or longer than 40 characters. | Without a uid Grafana generates one at load time, so every URL and every alert link to the dashboard changes when it is re-provisioned. Over 40 characters is rejected. |
| DB003 | The same `uid` in more than one dashboard across the run. | Grafana keeps one dashboard per uid; the loser disappears without an error in the UI. |
| DB004 | `title` missing. | Rejected by Grafana. |
| DB005 | `id` is set to something other than `null`. | Numeric ids are database-local. An exported id collides with an unrelated dashboard on import and can overwrite it. |
| DB006 | A panel id is used more than once, including inside rows and collapsed rows. | Panels with duplicate ids render on top of each other or vanish; panel links and alert annotations point at the wrong panel. |
| DB007 | `__inputs` is present, or `${DS_...}` placeholders remain in the JSON. | This is the "export for sharing externally" format. The placeholders are only resolved by the import dialog, never by file provisioning, so every panel shows "datasource not found". |
| DB008 | `schemaVersion` missing. | Grafana's migration code decides from `schemaVersion` how to upgrade the model; without it older panel formats are mis-migrated. |
| DB009 | With `--datasource-uid`: a panel, target or query variable references a datasource uid outside the allowed set. Templated (`${datasource}`, `$ds`) and built-in (`-- Grafana --`, `-- Mixed --`) references are allowed. | A uid copied from another environment makes every panel show "datasource not found". |
| DB010 | ConfigMap data over 1 MiB (error) or over 900 KiB (warning). | The Kubernetes API rejects ConfigMaps above 1 MiB; a large dashboard plus a second key crosses the limit on the next edit. |
| DB011 | A labelled dashboard ConfigMap has a key that does not end in `.json`. | The sidecar writes every key to disk, but Grafana's dashboard provisioner only reads `*.json`. The dashboard is never loaded. |

## Helm values (`check-helm-values`)

| ID | What it catches | Why it matters |
|----|-----------------|----------------|
| HV001 | `helm template CHART -f VALUES` failed. The message contains Helm's stderr. | A values file that does not render is only noticed when the GitOps controller fails the sync, usually after the merge. |

After a successful render, all DB and AR/PR checks above run on the output.
Findings are labelled with the values file name.
