# HTTP API

`argus-scout serve` runs the scheduler and the HTTP API (install the `server` extra:
`pip install "argus-scout[server]"`). Reaching the port is host access, so the defaults are closed: scout binds to
`127.0.0.1:8400`, `server.read_only: true` refuses every write, and with no token set every write is refused too.
Nothing on the API moves a recommendation past `approved`: there is no deploy route.

## Routes

| Method | Path | Auth | What |
|---|---|---|---|
| POST | `/api/events/{source}` | the source's `token_env`, else the server token | Push one status event or a list of up to 100 (256 KiB). All or nothing. Returns each event's inbox outcome. See [sources.md](sources.md#webhook). |
| POST | `/api/events/{source}/{event_id}/ack` | server token | Body `{"by": "<NAME>"}`. 404 unknown event, 409 already acked. |
| POST | `/api/recommendations/{id}/approve` | server token | Body `{"by": "<NAME>", "reason": "..."}`. A manual approval, recorded in the audit trail. Proposed only. |
| POST | `/api/recommendations/{id}/reject` | server token | The same body. The reason feeds rejection memory and the briefings. Proposed only. |
| GET | `/api/events` | read auth | Open events, grouped by fingerprint |
| GET | `/api/recommendations` | read auth | Recommendations, oldest first. `?status=proposed` (repeatable), `?limit=` (1-500, default 100). |
| GET | `/api/recommendations/{id}` | read auth | One recommendation with its transition history |
| GET | `/api/reports/latest` | read auth | The newest report; `?kind=morning` for the newest of one kind. 404 when there is none. |
| GET | `/api/policy/explain` | read auth | The effective heartbeat policy and what decides each open item now |
| GET | `/wall` | read auth | The latest report, open items and pending decisions as plain text |
| GET | `/health` | none | `{"status": "ok", "version": "..."}` |
| GET | `/metrics` | none | Prometheus exposition of scout's own state (below) |

FastAPI's own `/docs` and `/openapi.json` are served too.

```bash
curl -s localhost:8400/wall
curl -s 'localhost:8400/api/recommendations?status=proposed'
curl -s -X POST localhost:8400/api/recommendations/<REC_ID>/reject \
  -H "Authorization: Bearer $SCOUT_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"by": "<NAME>", "reason": "That volume is meant to be full"}'
```

## Auth model

Every write passes two independent layers.

1. **The write guards** (argus-cortex's `WriteGuard`, on every POST, PUT, PATCH and DELETE, so a route added later
   is covered too):
   - `server.read_only: true` (the default) refuses every write with 403, before anything else looks at it.
   - A browser write from an origin not in `server.allowed_origins` is a 403. A request with no `Origin` header
     (curl, scripts, producers) passes, as does a same-host IP or `localhost` origin.
2. **A bearer token, failing closed.** `Authorization: Bearer <TOKEN>`, where the token is the value of the variable
   `server.token_env` names (`SCOUT_API_TOKEN` by default). Unset or empty is 503 (the route is closed), missing or
   wrong is 401. `token_env: null` keeps every write route closed for good. A webhook source with its own
   `token_env` accepts only that token, and that token is good for pushes as that source only.

To turn writes on: set the token in `.env` beside `scout.yaml`, set `server.read_only: false`, restart. Tokens are
read on every request, so rotating one needs no restart.

**Reads** (`/api/*` GETs and `/wall`) are open by default, which is fine on loopback. Set `server.read_auth: true`
to require the same bearer token on them, and do that before binding anywhere but `127.0.0.1`; `serve` warns if you
don't. `/health` and `/metrics` stay open either way: they hold no event or report content. The container image
binds `0.0.0.0` inside the container, so publish its port on loopback (`-p 127.0.0.1:8400:8400`) or turn on
`read_auth`.

**Bodies are capped**: 256 KiB for an event push, 8 KiB for an ack, approve or reject (413 over it, 400 for a bad
`Content-Length`). Write routes read the body only after auth, so an unauthenticated caller can't make scout buffer
one. Action bodies refuse unknown keys; `by` is 1-100 characters and `reason` up to 2000, with no control
characters.

| Status | Meaning |
|---|---|
| 400 | Body isn't JSON, or a malformed `Content-Length` |
| 401 | Missing or wrong bearer token |
| 403 | `server.read_only`, or an untrusted browser origin |
| 404 | No such source, event, recommendation or report |
| 405 | A push to a polled source |
| 409 | The event is already acked; the recommendation isn't `proposed`, or changed since it was read (reload and decide again) |
| 413 | Body over the cap |
| 422 | Body failed validation |
| 503 | No token configured: the route is closed |

A test walks every route the app serves and fails on a state-changing one without the guards and a token check.

## `/wall`

A plain-text page for a terminal or a kiosk: the latest report, the open items (severity, source, summary, count,
whether an ack is needed) and up to 20 recommendations awaiting a decision, with their ids. Times are in
`schedule.timezone`. Everything on it came from producers or the model, so control and format characters
(terminal escapes, bidi overrides) are stripped first. It reads the store, so it works with or without a text
channel.

```bash
watch -n 60 curl -s localhost:8400/wall
```

## `/api/policy/explain`

What the status cycle would decide if it ran now: `now`, `timezone`, whether rest or quiet hours are on, the
effective heartbeat `policy` (each key with its `origin`, `default` or `file`, and the file's error if it didn't
load), and for each open item `broadcast`, `report` and `voice` plus the `rule` that decided, such as
`report.min_severity=notice; voice.min_severity=warning` or `sources.ci.channel=briefing`. Use it to answer "why
did (or didn't) scout say that?".

## `/metrics`

Read from the store and the policy files at scrape time, so the values survive restarts.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `scout_usage_used` | gauge | `kind` | Units used today against a daily cap |
| `scout_usage_limit` | gauge | `kind` | The daily cap; 0 is unlimited |
| `scout_tariff_loaded` | gauge | | 1 if a tariff is configured and loads |
| `scout_tariff_period` | gauge | `plan`, `period` | 1 for the active period |
| `scout_tariff_rate` | gauge | `plan`, `period`, `currency`, `unit` | The active rate, in the currency's minor unit |
| `scout_tariff_next_change_seconds` | gauge | `next_period` | Seconds until the next period |
| `scout_recommendations` | gauge | `status` | Recommendations in each status |
| `scout_approvals_total` | counter | `source` | Approvals in the audit trail, `manual` or `standing` |
| `scout_inbox_open_events` | gauge | `severity` | Open (unacked, unexpired) status events |
| `scout_job_runs_total` | counter | `job` | Scheduler job runs |
| `scout_job_errors_total` | counter | `job` | Runs that raised |
| `scout_job_last_success_timestamp_seconds` | gauge | `job` | When a job last finished cleanly |
| `scout_job_running` | gauge | `job` | 1 while a job runs |
| `scout_deliveries_total` | counter | `channel`, `result` | Delivery attempts, `sent` or `failed` |

Jobs are named `status`, `analysis`, `redeliver`, `briefing:<kind>@<HH:MM>` and `poll:<source>`. A useful alert:
`time() - scout_job_last_success_timestamp_seconds{job="analysis"} > 3600`.
