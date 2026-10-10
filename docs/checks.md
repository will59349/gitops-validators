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
`--disable ID[,ID]` (repeatable, on all three commands) removes the named ids
from the report and from the exit code; an id that does not exist is a usage
error (exit code 2).

## What the levels mean

The "Level (basis)" column says why a check has its level:

| Basis | Meaning |
|-------|---------|
| rejected | Grafana, Prometheus or Kubernetes refuses the input. Error. |
| broken | The input loads, but the result does not work as written. Error. |
| convention | A house rule, not a requirement of Grafana or Prometheus. Error by default so that it can gate a merge; change the rule with its option or turn it off with `--disable`. |
| warning | The input loads and works; the finding is something to look at. |

The convention checks are AR009, AR010 and PR005.

## Where the statements about Grafana come from

Statements of the form "Grafana does X" below were checked against the Grafana
source code of **v10.4.0 and v11.6.0** (file provisioning of alert rules and
dashboards), and the ConfigMap size limit against the Kubernetes
documentation. Other versions were not checked unless a row names them.
Grafana's validation changes between releases, so compare with the version you
run. Statements that were not checked against a source say so.

What happens when an alert rule file is wrong, in those two versions:

* Grafana first reads and parses **every** file in `provisioning/alerting`.
  If one file fails to parse (YAML syntax, a missing mandatory field, a bad
  duration, an unknown state), the provisioning run returns that error and
  **no** file is applied, including the correct ones.
* If all files parse, rules are written one at a time. A rule that is refused
  at this stage (invalid uid, duplicate title in a folder) stops the run;
  rules before it are already applied, rules after it are not.
* At startup the provisioning service returns the error and Grafana does not
  finish starting. A reload through the admin API
  (`/api/admin/provisioning/alerting/reload`, for example from a sidecar)
  answers with HTTP 500 and the rules stay as they were before the reload.

In neither case does the pull request pipeline see it, which is what these
checks are for.

## Grafana alert rules (`check-alert-rules`)

Applies to Grafana provisioning files (`apiVersion: 1` + `groups`), the same
content as a `.yaml`/`.yml` value inside a ConfigMap, and rendered manifests.

| ID | Level (basis) | What it catches | Why it matters |
|----|---------------|-----------------|----------------|
| AR001 | error (rejected) | The file (or the ConfigMap value) is not valid YAML/JSON, or cannot be read. | Parse failure: alert provisioning fails as described above. |
| AR002 | error (rejected) | `uid` missing, empty, not a string, longer than 40 characters, or containing characters other than `A-Z a-z 0-9 - _`. | A rule without a uid is a parse failure. Length and characters are checked by Grafana's uid validator when the rule is created. |
| AR003 | error (broken) | The same `uid` appears more than once across all files in the run. Both locations are reported. | Grafana looks a rule up by uid and updates it if it exists, so the later definition overwrites the earlier one without any error. One rule is lost. |
| AR004 | error (rejected) | `title` missing or blank. | Parse failure. |
| AR005 | error (rejected) | The same `title` appears twice in the same folder (and org). | The database has a unique index on organisation, folder and title; the second rule is refused when it is created. Folders are compared by the `folder` string as written. |
| AR006 | error (rejected) | Group `interval` missing or not a Prometheus-style duration (`30s`, `5m`, `1h30m`); rule `for` present but not such a duration. | Grafana parses both with the Prometheus duration parser. `5 minutes` or a bare `5` fails, and so does a group without `interval`. |
| AR007 | error (rejected / broken) | `condition` missing, or not equal to any `refId` in `data`. | A missing condition is a parse failure. What Grafana does with a condition that names a refId that does not exist was not checked against the source; the rule has nothing to evaluate either way. |
| AR008 | error (broken) | A `data` item has no `datasourceUid`, or (with `--datasource-uid`) uses one outside the allowed set. `__expr__` is always allowed. | Not checked by Grafana when the file is loaded. A uid that does not exist in the target Grafana cannot be queried when the rule is evaluated; it is a common copy-paste mistake between environments. Without `--datasource-uid` only the missing case is reported. |
| AR009 | error (**convention**) | `labels.severity` missing or not in the allowed set (`--severity`, default `critical,warning,info`). | Grafana does not require a severity label. Where notification policies route on it, a typo (`crtical`) matches no route. Change the set with `--severity`, or turn the check off with `--disable AR009`. |
| AR010 | error (**convention**) | A required annotation is missing (`--require-annotation`, default `summary`). | Grafana does not require any annotation. Giving `--require-annotation` replaces the default list; `--disable AR010` turns the check off. |
| AR011 | error (rejected) | `noDataState` is not one of `NoData`, `Alerting`, `OK`, `KeepLast`, or `execErrState` is not one of `Error`, `Alerting`, `OK`, `KeepLast`. | Parse failure. `KeepLast` is accepted by Grafana 10.4.1 and later; 10.4.0 rejects it, and this check does not report that. |
| AR012 | error (rejected) | Structural problems: `groups` or `rules` is not a list, a group has no `name` or no `folder`, a rule or data item is not a mapping, `data` is empty, a data item has no `refId`. | Missing name, missing folder and empty `data` are parse failures, and a value of the wrong type cannot be unmarshalled. The missing `refId` case was not checked against the Grafana source. |
| AR013 | warning | A group has no rules: `rules` is missing, null or an empty list. Also reported for `PrometheusRule` groups. | Grafana accepts such a group and provisions nothing from it, so it is not an error. It is what a chart renders for an environment whose values define no rules for that group, and it is also what a misspelt `rules:` key looks like, hence a warning rather than silence. Use `--disable AR013` if empty groups are normal in your chart. |
| AR014 | warning | A rule has no `for`. | Grafana 11.5.0 and later treat a missing `for` as 0s. Earlier versions (checked: 10.4.0 and 11.0.0 to 11.4.0) parse it unconditionally and fail on the empty value. Write `for: 0s` explicitly, or use `--disable AR014` when every target runs 11.5 or later. |

The checks do not reproduce all of Grafana's validation. Among the things
Grafana 11.6 also refuses and these checks do not look at: a missing or
inverted `relativeTimeRange` on a query, reserved label names, and a group
`interval` that does not fit the scheduler's base interval.

A plain Prometheus rule file (top-level `groups:` without `kind`) is not a
supported input: it is read as Grafana provisioning format and reported as
such. Pass Grafana provisioning files, ConfigMaps or `PrometheusRule` objects.

## Prometheus Operator `PrometheusRule` (`check-alert-rules`)

These rows describe Prometheus rule semantics. What the operator does with an
invalid object (reject it in the admission webhook, or leave it out of the
generated rule files) depends on the installation and was not checked.

| ID | Level (basis) | What it catches | Why it matters |
|----|---------------|-----------------|----------------|
| PR001 | error (rejected) | A rule has both `alert` and `record`, or neither. | Not a valid Prometheus rule. |
| PR002 | error (rejected) | `expr` missing or empty. | Not a valid Prometheus rule. |
| PR003 | error (broken) | The same `alert` name appears more than once in one group. | Prometheus accepts it. The alerts share a name and are usually grouped and silenced together; often a copy-paste error. Use `--disable PR003` if it is intended (the same alert with a threshold per severity). |
| PR004 | error (rejected) | `for`, `keep_firing_for` or group `interval` is not a valid duration. | Not a valid Prometheus rule. |
| PR005 | error (**convention**) | An alert has no `labels.severity`, or its value is not in the allowed set. | Same convention as AR009 and the same `--severity` option; `--disable PR005` turns it off. |

A `PrometheusRule` without `spec.groups`, or with a malformed group, is
reported as AR012; a group without rules as AR013.

## Prometheus query check (`--prometheus-url`)

Optional. Each expression is sent to `POST <url>/api/v1/query` with
`time=now`. Grafana macros (`$__rate_interval`, `$__interval`, `$__range`,
...) are replaced with constants first; expressions that still contain
template variables are skipped. With `--query-datasource-uid` only Grafana
rules whose `datasourceUid` is in that set are queried (default: the
`--datasource-uid` set, or everything). `PrometheusRule` expressions are
always queried. Identical expressions are sent once.

The check cannot tell PromQL from another query language. If the rule files
also contain Loki or other non-Prometheus queries, name the Prometheus
datasource uids with `--query-datasource-uid`; otherwise those queries are
sent to Prometheus too and come back as `AQ001`.

| ID | Level | What it catches | Why it matters |
|----|-------|-----------------|----------------|
| AQ001 | error | Prometheus answered 400 or 422: the expression does not parse or could not be executed (unknown function, unbalanced parentheses, many-to-many matching). | The same expression fails when the rule is evaluated. Static checks cannot see this. A 422 can depend on the data in the Prometheus that was asked. |
| AQ002 | warning; error with `--strict-query` | Prometheus could not be reached, or returned another error (401, 5xx). After a connection failure the remaining expressions are not sent. | A check that cannot run should not block merges by default; a pipeline that must prove the queries ran can opt in. |

A token for Prometheus is read from the environment variable named by
`--bearer-token-env`; the value never appears on the command line.
Credentials written into the URL itself (`https://user:pass@host`) are on the
command line by definition. They are removed from findings, but prefer the
token variable.

## Grafana dashboards (`check-dashboards`)

Applies to dashboard JSON files, ConfigMaps carrying the sidecar label
(`--dashboard-label`, default `grafana_dashboard`) or any `data` key ending
in `.json`, and rendered manifests. API-export wrappers
(`{"dashboard": {...}, "overwrite": true}`) are unwrapped.

The rows describe **file provisioning**: a dashboard provider reading a
directory, which is also how dashboards delivered by a sidecar reach Grafana.
Where importing through the HTTP API behaves differently, the row says so.

| ID | Level (basis) | What it catches | Why it matters |
|----|---------------|-----------------|----------------|
| DB001 | error (rejected) | The file or ConfigMap value is not valid JSON (or the manifest is not valid YAML), or the JSON is not an object. | Grafana's provisioner logs `failed to load dashboard from` and continues with the next file. The dashboard is absent; `kubectl apply` accepted it because a ConfigMap value is just a string. |
| DB002 | warning, or error (rejected) | Warning: `uid` missing, empty or not a string. Error: `uid` longer than 40 characters, or containing characters other than `A-Z a-z 0-9 - _`. | A dashboard without a uid is loaded: Grafana generates one when it first saves the dashboard. The generated uid differs in every Grafana database (each environment, and each start of a Grafana without persistent storage), so URLs and uid references to the dashboard are not stable. A uid that is too long or has other characters is refused when the dashboard is saved. |
| DB003 | error (broken) | The same `uid` in more than one dashboard across the run. | The provisioner logs `the same UID is used more than once` and stops writing to the database for the provider involved (`no database write permissions because of duplicates`), so later changes to that provider's dashboards are not applied. |
| DB004 | error (rejected) | `title` missing. | Refused when the dashboard is saved (`Dashboard title cannot be empty`). |
| DB005 | warning | `id` is set to something other than `null`. | File provisioning ignores the value: the provisioner clears `id` before saving. It matters when the same JSON is sent to the HTTP API, where `id` addresses a dashboard by its database id, which differs in every Grafana. Use `--disable DB005` if the files are only ever provisioned. |
| DB006 | error (broken) | A panel id is used more than once, including inside rows and collapsed rows. | Not checked by Grafana when the file is loaded. Panel ids are what `viewPanel` links and the `panelId` of alert rules refer to; with duplicates these are ambiguous. How the UI renders such a dashboard was not checked. |
| DB007 | error (broken) | `__inputs` is present, or `${DS_...}` placeholders remain in the JSON. | This is the "export for sharing externally" format. The placeholders are filled in by the import dialog; the file provisioner saves the JSON as it is, so the panels refer to a datasource literally called `${DS_...}`. |
| DB008 | warning | `schemaVersion` missing. | Grafana loads the dashboard. `schemaVersion` tells Grafana which model migrations to run; a dashboard without it is usually hand-trimmed or not an export. The effect on rendering was not checked. |
| DB009 | error (broken) | With `--datasource-uid`: a panel, target or query variable references a datasource uid outside the allowed set. Templated (`${datasource}`, `$ds`) and built-in (`-- Grafana --`, `-- Mixed --`) references are allowed. | A uid copied from another environment does not exist in this one. Not reported without `--datasource-uid`. |
| DB010 | error above 1 MiB (rejected), warning above 900 KiB | Total size of the keys and values of a dashboard ConfigMap. | Kubernetes documentation: "The data stored in a ConfigMap cannot exceed 1 MiB." `binaryData` is counted by its base64 length, which overestimates. Client-side `kubectl apply` additionally stores the object in an annotation, and annotations have their own, much lower size limit; this check does not look at that. |
| DB011 | error (broken) | A labelled dashboard ConfigMap has a key that does not end in `.json`. | Grafana's dashboard file reader only considers files whose name ends in `.json`. A sidecar writes the key as the file name (not checked against the sidecar source), so such a key is never loaded. |

## Helm values (`check-helm-values`)

| ID | Level | What it catches | Why it matters |
|----|-------|-----------------|----------------|
| HV001 | error | `helm template CHART -f VALUES` failed or timed out. The message contains Helm's stderr. | A values file that does not render is otherwise noticed when the GitOps controller fails the sync, after the merge. |

After a successful render, all DB and AR/PR checks above run on the output.
Findings are labelled with the values file name. Uid uniqueness is checked
within one values file, not across them.
