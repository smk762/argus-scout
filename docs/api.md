# HTTP API

`argus-scout serve` runs the scheduler and the HTTP API (install the `server` extra:
`pip install "argus-scout[server]"`). Reaching the port is host access, so the defaults are closed: scout binds to
`127.0.0.1:8400`, `server.read_only: true` refuses every write, and with no token set every write is refused too.
Nothing on the API moves a recommendation past `approved`: there is no deploy route.

## Routes

| Method | Path | Auth | What |
|---|---|---|---|
| POST | `/api/events/{source}` | the source's `token_env`, else the server token | Push one status event or a list of up to 100 (256 KiB). All or nothing: validated whole, then stored in one transaction. Returns each event's inbox outcome. See [sources.md](sources.md#webhook). |
| POST | `/api/events/{source}/{event_id}/ack` | server token | Body `{"by": "<NAME>"}`. 404 unknown event, 409 already acked. |
| POST | `/api/recommendations/{id}/approve` | server token | Body `{"by": "<NAME>", "reason": "...", "revision": <N>}`. A manual approval, recorded in the audit trail. Proposed only, and only at the `revision` you viewed (see [Decisions](#decisions)). |
| POST | `/api/recommendations/{id}/reject` | server token | The same body. The reason feeds rejection memory and the briefings. Proposed only. |
| GET | `/api/events` | read auth | Open events, grouped by fingerprint |
| GET | `/api/recommendations` | read auth | Recommendations, oldest first, each with its `revision`. `?status=proposed` (repeatable), `?limit=` (1-500, default 100). |
| GET | `/api/recommendations/{id}` | read auth | One recommendation with its `revision` and transition history |
| GET | `/api/reports/latest` | read auth | The newest report; `?kind=morning` for the newest of one kind. 404 when there is none. |
| GET | `/api/policy/explain` | read auth | The effective heartbeat policy and what decides each open item now |
| GET | `/wall` | read auth | The latest report, open items and pending decisions as plain text |
| GET | `/health` | none | `{"status": "ok", "version": "..."}` |
| GET | `/metrics` | none | Prometheus exposition of scout's own state (below) |

FastAPI's own `/docs`, `/redoc` and `/openapi.json` are served too, unless `server.read_auth` is on: they sit outside
the bearer check, so `read_auth` turns them off.

```bash
curl -s localhost:8400/wall
curl -s 'localhost:8400/api/recommendations?status=proposed'
curl -s localhost:8400/api/recommendations/<REC_ID>          # read it: note "revision"
curl -s -X POST localhost:8400/api/recommendations/<REC_ID>/reject \
  -H "Authorization: Bearer $SCOUT_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"by": "<NAME>", "reason": "That volume is meant to be full", "revision": <N>}'
```

## Decisions

While a recommendation is `proposed`, every analysis cycle that still sees its signal rewrites it: the title,
action, severity, evidence and expiry can all change. So a decision names what it is a decision on. Every save bumps
the recommendation's `revision` (shown by both GETs and on `/wall` as `[<id> rev <N>]`), and an approve or reject
must carry the `revision` you viewed. If the recommendation has changed since, the answer is 409 "changed since you
viewed it; reload": read it again and decide on what it says now. The check and the write are one transaction, and
the decision is applied to the recommendation as stored, so it can never undo a change the engine made, nor approve
wording you didn't see. The engine's own writes carry the revision they read too, so a cycle can't overwrite a
decision either.

## Auth model

Every write passes two independent layers.

1. **The write guards** (argus-cortex's `WriteGuard`, on every POST, PUT, PATCH and DELETE, so a route added later
   is covered too):
   - `server.read_only: true` (the default) refuses every write with 403, before anything else looks at it.
   - A browser write from another origin is a 403 unless that origin is in `server.allowed_origins`. A request with
     no `Origin` header (curl, scripts, producers) passes, as does a same-host origin whose host is an IP,
     `localhost` or a `server.trusted_hosts` name.
2. **A bearer token, failing closed.** `Authorization: Bearer <TOKEN>`, where the token is the value of the variable
   `server.token_env` names (`SCOUT_API_TOKEN` by default). Unset or empty is 503 (the route is closed), missing or
   wrong is 401. `token_env: null` keeps every write route closed for good. A webhook source with its own
   `token_env` accepts only that token, and that token is good for pushes as that source only. A push to a name
   that isn't a push source (unknown, or a poller) says 404 or 405 only to a caller holding the server token; anyone
   else gets the 401 (or 503) a push source would give, so the source list can't be probed without credentials.

To turn writes on: set the token in `.env` beside `scout.yaml`, set `server.read_only: false`, restart. Tokens are
read on every request, so rotating one needs no restart.

**Reads** (`/api/*` GETs and `/wall`) are open by default, which is fine on loopback. Set `server.read_auth: true`
to require the same bearer token on them, and do that before binding anywhere but `127.0.0.1`; `serve` warns if you
don't. `/health` and `/metrics` stay open either way: they hold no event or report content. The container image
binds `0.0.0.0` inside the container, so publish its port on loopback (`-p 127.0.0.1:8400:8400`) or turn on
`read_auth`.

### Host names: `trusted_hosts` and `allowed_origins`

Every request, read or write, must name scout in its `Host` header by an IP address, `localhost`, or a name in
`server.trusted_hosts`; anything else is a 400 before any route runs. This is what stops **DNS rebinding**: a page on
`evil.example` can re-point its own name at `127.0.0.1` and then read `http://evil.example:8400/wall` as if it were
same-origin, but its requests still say `Host: evil.example`. Only `/health` and `/metrics` answer any `Host`: they
hold no event or report content, and are reached by service names (the quickstart's Prometheus scrapes
`scout:8400`).

So the two settings answer different questions:

| Setting | Question | Needed when |
|---|---|---|
| `trusted_hosts` | Which **names** may this server be reached by? | You open scout by a DNS name (`http://scout.lan:8400`), directly or behind a proxy that passes `Host` through: list `scout.lan`. A page served from that name may then also write to its own server. |
| `allowed_origins` | Which **other** sites' pages may send it writes? | A UI on a different origin (`https://ui.example`) posts approvals to scout. It must still reach scout by a trusted `Host`. |

Reaching scout by `127.0.0.1`, another IP or `localhost` needs neither. A name in `trusted_hosts` is trusted for both
the `Host` check and as a same-host write `Origin`; listing a name's origin in `allowed_origins` alone is not enough
to reach scout by that name.

**Bodies are capped**: 256 KiB for an event push, 8 KiB for an ack, approve or reject (413 over it, 400 for a bad
`Content-Length`). Write routes read the body only after auth, so an unauthenticated caller can't make scout buffer
one. Action bodies refuse unknown keys; `by` is 1-100 characters and `reason` up to 2000, with no control
characters.

| Status | Meaning |
|---|---|
| 400 | `Host` isn't an IP, `localhost` or a `trusted_hosts` name; body isn't JSON, or the source can't parse it; a malformed `Content-Length` |
| 401 | Missing or wrong bearer token |
| 403 | `server.read_only`, or an untrusted browser origin |
| 404 | No such source (told only to the server token), event, recommendation or report |
| 405 | A push to a polled source (told only to the server token) |
| 409 | The event is already acked; the recommendation isn't `proposed`, or changed since you viewed it (reload and decide again) |
| 413 | Body over the cap |
| 422 | Body failed validation |
| 503 | No token configured: the route is closed |

A test walks every route the app serves and fails on a state-changing one without the guards and a token check, and
on any route (bar `/health` and `/metrics`) that answers a rebinding `Host`.

## `/wall`

A plain-text page for a terminal or a kiosk: the latest report, the open items (severity, source, summary, count,
whether an ack is needed) and up to 20 recommendations awaiting a decision, each with its id and revision
(`[<id> rev <N>]`, what an approve or reject names). Times are in
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
