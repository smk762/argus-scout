# Suite status

Scout can report on the Argus suite itself: which services are up, what each one's `/health` says, and which
version each runs. It reads all of it from Prometheus. Scout never probes a service: Prometheus scrapes and probes,
and scout asks it with PromQL, as it does for everything else. Nothing here acts on a service either. Scout
reports that `lens` is down; restarting it is a person's job.

What you get:

- **Status events** when a service goes down, stays degraded, changes version (a deploy happened), or runs
  different versions on two hosts. They are ordinary inbox items: the heartbeat policy decides which are worth a
  status update or a spoken one, and each closes by itself when the condition clears.
- **A suite status report** on request: what is down, degraded or out of date, then every service with its
  version.

## What Prometheus needs to hold

Scout reads three things, for every series that carries an `argus_peer` label:

| Series | From | Tells scout |
|---|---|---|
| `up` | the scrape of each service's `/metrics` | whether the service answers |
| `probe_success`, `probe_http_status_code` | a blackbox exporter probing each service's `/health` | `ok`, `degraded`, `starting`, or no answer |
| `argus_build_info{service, version}` | each service's `/metrics` (argus-cortex serves the gauge) | which version runs where |

The `argus_peer` label is what makes a target a suite service. [argus-iris](https://github.com/smk762/argus-iris)
sets it: its `GET /directory/sd` lists every running container whose compose file labels it `argus.peer`, in
Prometheus's HTTP service-discovery format, one target per service with the labels `argus_peer`, `argus_service`,
`node`, `container` and `argus_health_path`. The route needs no token.

### Scrape config

Two jobs over the same discovery, plus a blackbox module. Replace `<IRIS_HOST>` with your broker's address and
`<BLACKBOX_HOST>` with the blackbox exporter's.

```yaml
scrape_configs:
  # /metrics of every suite service: `up` and argus_build_info.
  - job_name: argus
    metrics_path: /metrics
    http_sd_configs:
      - url: http://<IRIS_HOST>:8120/directory/sd
        refresh_interval: 60s

  # /health of every suite service, through the blackbox exporter.
  - job_name: argus-health
    metrics_path: /probe
    params:
      module: [argus_health]
    http_sd_configs:
      - url: http://<IRIS_HOST>:8120/directory/sd
        refresh_interval: 60s
    relabel_configs:
      # What to probe: <scheme>://<host:port><argus_health_path>.
      - source_labels: [__scheme__, __address__, argus_health_path]
        regex: (.+);(.+);(.*)
        target_label: __param_target
        replacement: $1://$2$3
      # Keep the service's address as `instance`, then scrape the blackbox exporter instead.
      - source_labels: [__address__]
        target_label: instance
      - target_label: __address__
        replacement: <BLACKBOX_HOST>:9115
      - target_label: __scheme__
        replacement: http
```

```yaml
# blackbox.yml
modules:
  argus_health:
    prober: http
    timeout: 5s
    http:
      method: GET
      valid_status_codes: [200]
      follow_redirects: false
      fail_if_body_not_matches_regexp:
        - '"status"\s*:\s*"ok"'
```

The module succeeds only when `/health` answers `200` and its body says `"status": "ok"`. A service on
argus-cortex's `ServiceHealth` answers `200` for `ok` and `degraded` and `503` while `starting`, so the two series
tell the four cases apart:

| `probe_success` | `probe_http_status_code` | State |
|---|---|---|
| 1 | 200 | `ok` |
| 0 | 200 | `degraded` (a required dependency check failed) |
| 0 | 503 | `starting` |
| 0 | another status | `degraded`, with the status in the detail |
| 0 | 0 | no answer |

The quickstart ships all of this: [`prometheus.iris.yml`](../deploy/quickstart/prometheus.iris.yml),
[`blackbox.yml`](../deploy/quickstart/blackbox.yml) and the override file
[`compose.iris.yaml`](../deploy/quickstart/compose.iris.yaml)
(see [the quickstart](../deploy/quickstart/README.md#the-whole-suite-through-argus-iris)).

Without argus-iris, label the targets yourself: any static target with an `argus_peer` label counts, and `node` and
`container` are used when present. Without the health probe, scout falls back to `up`, and lists a service that
answers as `up` rather than `ok`. A service that serves no `/metrics` then reads as down, so probe it.

## The source

```yaml
services:
  prometheus_url: http://prometheus:9090

sources:
  - kind: suite_status
    name: suite
```

| Key | Default | Meaning |
|---|---|---|
| `name` | required | The `source` of every event it raises, and what the heartbeat policy's `sources:` routing matches |
| `prometheus_url` | `services.prometheus_url` | The Prometheus to ask. With neither set, the source is refused. |
| `interval` | `60` | Seconds between readings |
| `health_job` | `argus-health` | The probe job's name. Its own `up` says the blackbox exporter answered, not the service, so `up` is read from every other job. A probe job under another name is still recognised by its probe series, so a mismatch can't hide a dead service. |
| `down_for` | `120` | Seconds without any answer before a service is reported down. At least 60; keep it at least twice the scrape interval. |
| `degraded_for` | `600` | Seconds without an `ok` from `/health` before a service is reported degraded |
| `deploy_window` | `3600` | Seconds a version change stays open (and in the report) |
| `forget_after` | `86400` | Seconds a service that is no longer a scrape target is still reported down |
| `severity` | see below | The severity of each condition's event |
| `resolve_missing` | `true` | Close an event once a reading no longer finds its condition |

Each reading is twelve instant queries against Prometheus, run together. `argus-scout poll --config scout.yaml
--source suite` runs one and prints the events without storing anything.

### The events

| Condition | Raised when | Summary | Default severity |
|---|---|---|---|
| down | nothing answered for `down_for`, and Prometheus has watched it that long: no `/health` answer and no `/metrics` scrape, or the service is no longer a target | `lens on alpha is down` | `warning` |
| degraded | `/health` has answered, but never `ok`, for `degraded_for` | `lens on alpha is degraded`, or `is still starting` | `warning` |
| version change | the service reports a version it did not report earlier within `deploy_window` | `lens on alpha changed version from 0.14.2 to 0.15.0` | `info` |
| version skew | two hosts report different versions of the same peer | `lens runs different versions: 0.14.2 on beta, 0.15.0 on alpha` | `notice` |

```yaml
  - kind: suite_status
    name: suite
    severity:
      down: critical
      version_change: notice
```

Things worth knowing:

- **A stopped container disappears.** argus-iris lists only running containers, so a service that stops is dropped
  from Prometheus's targets instead of reading `up == 0`. Scout treats a service Prometheus held within
  `forget_after` and no longer does as down ("no longer a scrape target"). After `forget_after` it is forgotten:
  scout takes it as removed on purpose. Acknowledge the event to quiet it sooner.
- **Starting is not degraded, until it lasts.** A service restarting through `starting` raises nothing. One that
  has not answered `ok` for `degraded_for` raises the degraded event, worded "is still starting".
- **A new service gets its full window.** A service probed for the first time is not "degraded for ten minutes":
  the degraded event waits until it has been probed for `degraded_for`.
- **A version change is an event for `deploy_window`,** then it resolves. A second deploy in that time is a new
  event. A service that is down reports no version, so it raises no version event.
- **Which host is behind** is said only when the versions can be ordered (`0.14.2`, `0.2.3.dev38`,
  `0.1.5.dev1+g9b71751`). For anything else scout says the versions differ and leaves it there. "Out of date"
  means behind another host running the same peer; scout does not look up releases.
- **Unknown is never all clear.** If Prometheus can't answer any one of the queries, the reading fails: no event
  is raised and none is resolved.
- **Label values are text.** Peer, host and container names come from Prometheus. Each is cut to 80 characters,
  with control characters and line breaks removed, before it reaches a summary or a report.

Events follow the heartbeat policy like any other source's (rest and quiet hours, re-announce, per-source
routing). With the default policy and the default severities:

| Condition | In a status update (`report.min_severity: notice`) | Spoken (`voice.min_severity: warning`) |
|---|---|---|
| down, degraded (`warning`) | yes | yes |
| version skew (`notice`) | yes | no |
| version change (`info`) | no: it shows in briefings and at `/wall` | no |

To hear about deploys as they happen, raise `severity.version_change` to `notice`, or lower the threshold for
this source alone in the heartbeat file:

```yaml
sources:
  suite:
    min_severity: info      # every suite event goes into status updates
    channel: text           # and none of them is spoken
```

## The report

`POST /api/reports` with `{"kind": "suite"}` (or `argus-scout brief --config scout.yaml --kind suite`) reads
Prometheus then and writes the summary:

```text
Suite status at 2026-06-01 12:00 UTC: 5 services.

Down (1)
- forge on alpha: down: no answer, no version

Degraded or starting (1)
- curator on alpha: degraded: /health does not answer ok, 0.2.7

Out of date (1)
- lens on beta: 0.14.2, behind 0.15.0 running elsewhere

Changed version in the last 1h (1)
- lens on alpha: 0.14.2 to 0.15.0

Services
- forge on alpha: down: no answer, no version
- curator on alpha: degraded: /health does not answer ok, 0.2.7
- atlas on alpha: ok, 0.5.0
- lens on alpha: ok, 0.15.0
- lens on beta: ok, 0.14.2
```

- **Always a listing.** No model writes it, so no state or version in it is a model's guess, `no_llm` changes
  nothing and no `llm_calls` is charged. `meta.path` is `template` and `meta.fallback_reason` is `listing`.
- **Stored and delivered like any report** (`kind: suite`), through the same writer and channels, held to the
  speech caps. Its severity is the worst condition it holds, so a channel's `min_severity` applies. Voice channels
  speak a one-line count ("Suite status: 5 services. 1 down: forge on alpha."), not the table.
- **On request only.** `suite` is not a `schedule.briefings` kind.
- **No source, no report.** Without a `suite_status` source the request is a `409`. When Prometheus can't answer
  it is a `503` and nothing is written: there is no "unknown" report to mistake for a clean one.
- **No services is not an all-clear.** If Prometheus holds no series with an `argus_peer` label, the report says
  so and points here.

The settings (windows, `health_job`) are the first `suite_status` source's.

## What scout does not do

- It never connects to a service, an exporter or argus-iris. The only host this feature contacts is the
  configured Prometheus, with read-only instant queries.
- It never restarts, redeploys or changes anything. There is no recommendation here to approve, and no enforce
  mode anywhere in scout.
- It keeps no state of its own about the suite. "Down for two minutes" and "changed in the last hour" are asked
  of Prometheus, so a restart of scout neither forgets nor repeats an event.
