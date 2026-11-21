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
| POST | `/api/recommendations/{id}/approve` | server token | Body `{"by": "<NAME>", "reason": "...", "revision": <N>}`. A manual approval, recorded in the audit trail. Proposed only, and only at the `revision` you viewed (see [Decisions](#decisions)). An optional `standing` also creates a runtime standing rule from it, in the same transaction (see [Standing rules](#standing-rules)). |
| POST | `/api/recommendations/{id}/reject` | server token | The same body, without `standing`. The reason feeds rejection memory and the briefings. Proposed only. |
| DELETE | `/api/approvals/rules/{rule_id}` | server token | Body `{"by": "<NAME>"}`. Deletes a **runtime** standing rule. 409 for a file rule (edit the approvals file), 404 unknown. |
| POST | `/api/reports` | server token | Body `{"kind": "morning"\|"evening"\|"status", "deliver": true, "no_llm": false}`. Write a report now, store it and (unless `deliver: false`) send it to the channels. Returns the report plus `delivered` and `delivery`. 409 while a report of that kind is being written. See [On-request reports](#on-request-reports). |
| POST | `/api/reports/{id}/deliver` | server token | Body `{"force": false, "channels": null}`. Send a stored report, as `argus-scout deliver`: a channel that already has it says `duplicate` unless `force`; `channels` names a subset. 404 unknown report, 409 no channels configured, 422 an unknown channel name. |
| GET | `/api/events` | read auth | Open events, grouped by fingerprint |
| GET | `/api/recommendations` | read auth | Recommendations, oldest first, each with its `revision`. `?status=proposed` (repeatable), `?limit=` (1-500, default 100). |
| GET | `/api/recommendations/{id}` | read auth | One recommendation with its `revision` and transition history; `approved_by_rule` names the standing rule that approved it, if one did |
| GET | `/api/approvals/rules` | read auth | Every standing rule: `origin` `file` (the approvals file) or `api` (created at runtime), `expires_at`, `expired`, and for a runtime rule `created_by` |
| GET | `/api/reports` | read auth | Stored reports, newest first. `?kind=morning`, `?limit=` (1-200, default 20). |
| GET | `/api/reports/{id}` | read auth | One report (`id` is an integer). 404 when there is none. |
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

## On-request reports

`POST /api/reports` writes a report the way a scheduled briefing does, through the same code: the same narrator and
persona, the model's `llm_calls` cap, the GPU lease and the template fallback (see [narrative.md](narrative.md)).
The report is stored with `meta.trigger: "request"` and then delivered through the channels, unless the body says
`"deliver": false`, in which case nothing is sent and nothing is spoken (send it later with
`/api/reports/{id}/deliver`). `"no_llm": true` skips the model and uses the template.

```bash
curl -s -X POST localhost:8400/api/reports \
  -H "Authorization: Bearer $SCOUT_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind": "morning", "deliver": false}'
curl -s -X POST localhost:8400/api/reports/<REPORT_ID>/deliver \
  -H "Authorization: Bearer $SCOUT_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"channels": ["<CHANNEL>"]}'
```

- **One report of a kind at a time.** While a report of that kind is being written, by a scheduled briefing job or
  another request, the answer is 409 at once: a request is never held open for the length of someone else's model
  call. Retry when it's done. Other kinds aren't held up. A scheduled job that comes due during an on-request
  report waits for it and then writes its own, so a slot is never lost.
- **Held to the caps.** Other work done while serving an HTTP request may go over a daily cap (unless
  `budget.block_manual`); reports and their deliveries never do, whatever `block_manual` says, exactly as the CLI
  and the scheduler are held. Over `llm_calls` the report comes from the template (`fallback_reason: budget`);
  over `tts` or `speak` voice is held back and text still goes.
- **Waits for the model.** The request returns when the report is stored and delivered, so allow for the model's
  `timeout`. The model call runs off the event loop; the rest of the API keeps answering meanwhile.
- A `status` report written here covers every open item, as a status report at a briefing slot does, not only the
  items the status cycle would announce.

## Decisions

While a recommendation is `proposed`, every analysis cycle that still sees its signal rewrites it: the title,
action, severity, evidence and expiry can all change. So a decision names what it is a decision on. Every save bumps
the recommendation's `revision` (shown by both GETs and on `/wall` as `[<id> rev <N>]`), and an approve or reject
must carry the `revision` you viewed. If the recommendation has changed since, the answer is 409 "changed since you
viewed it; reload": read it again and decide on what it says now. The check and the write are one transaction, and
the decision is applied to the recommendation as stored, so it can never undo a change the engine made, nor approve
wording you didn't see. The engine's own writes carry the revision they read too, so a cycle can't overwrite a
decision either.

## Standing rules

"Approve, and keep approving things like this": an approve may carry `standing`, which creates a runtime standing
rule from the recommendation being approved. Its fields are what
[`StandingApprovals.add_from`](../src/argus_scout/approvals.py) takes:

| Field | Required | Meaning |
|---|---|---|
| `scope` | yes | `analyzer`: matching proposals from this analyzer until the rule is deleted (or until `until`, if given). `until`: the same, but `until` is required. A one-off is a plain approve: omit `standing`. |
| `until` | for `scope: until` | When the rule expires: an ISO 8601 time with a UTC offset, in the future. |
| `title_pattern` | no | A regex (case-insensitive, up to 200 characters) the title must contain. Without it the rule matches this recommendation's title exactly (after normalising case and punctuation). |
| `max_impact` | no | `low` or `medium`; defaults to this recommendation's impact. `high` is never allowed. |

The rule also takes the recommendation's analyzer and confidence (a proposal must be at least as confident), and
records `created_by` (the body's `by`) and a note naming the recommendation.

```bash
curl -s -X POST localhost:8400/api/recommendations/<REC_ID>/approve \
  -H "Authorization: Bearer $SCOUT_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"by": "<NAME>", "revision": <N>, "standing": {"scope": "until", "until": "2026-12-31T00:00:00Z"}}'
curl -s localhost:8400/api/approvals/rules
curl -s -X DELETE localhost:8400/api/approvals/rules/<RULE_ID> \
  -H "Authorization: Bearer $SCOUT_API_TOKEN" -H 'Content-Type: application/json' -d '{"by": "<NAME>"}'
```

**All or nothing.** The manual approval and the rule are written in one store transaction: both happen or neither
does. You asked for one decision ("approve, and keep approving these"), and half of it would be a surprise either
way: approved, while you expect the next one to be too and it won't be; or a rule with no approval behind it.

**The guardrails still apply.** A standing rule for a proposal with high impact, or raised from a critical signal,
is refused with **422** and the reason (`no standing approval for this proposal: high impact; nothing was
approved`), and **the manual approval is not applied either**: the recommendation stays `proposed` at the same
revision. To approve it by hand, send the approve again without `standing`. A stale `revision`, or a
recommendation that isn't `proposed`, is a 409 as usual and creates no rule.

The response is the approved recommendation, as for any approve, plus `standing_rule`: the new rule. From the next
analysis cycle, a matching proposal is moved to `approved` (never further) with `approval_source: "standing"` and
`approved_by_rule` set to the rule's id; `/wall` lists what a rule approved in the last 24 hours ("approved by
standing rule `<RULE_ID>`"). An expired rule matches nothing; `GET /api/approvals/rules` still lists it (with
`expired: true`) until it is deleted.

**Audit.** Who approved the recommendation is in its transition history, as for any approve. The rule records who
created it (`created_by`, stored with the rule, so it survives restarts). Who deleted a runtime rule is logged
(`standing_approval.rule_removed`, with `rule_id`, `by` and `created_by`): the store keeps no history of deleted
rules. File rules can't be created or deleted over the API; they come from `files.approvals`.

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

**Bodies are capped**: 256 KiB for an event push, 8 KiB for an ack, approve, reject, rule delete, report or deliver (413 over it, 400 for a bad
`Content-Length`). Write routes read the body only after auth, so an unauthenticated caller can't make scout buffer
one. Action bodies refuse unknown keys; `by` is 1-100 characters and `reason` up to 2000, with no control
characters.

| Status | Meaning |
|---|---|
| 400 | `Host` isn't an IP, `localhost` or a `trusted_hosts` name; body isn't JSON, or the source can't parse it; a malformed `Content-Length` |
| 401 | Missing or wrong bearer token |
| 403 | `server.read_only`, or an untrusted browser origin |
| 404 | No such source (told only to the server token), event, recommendation, report or standing rule |
| 405 | A push to a polled source (told only to the server token) |
| 409 | The event is already acked; the recommendation isn't `proposed`, or changed since you viewed it (reload and decide again); a delete names a file rule (edit the approvals file); a report of that kind is being written; no channels to deliver to |
| 413 | Body over the cap |
| 422 | Body failed validation; a standing rule the guardrails refuse (nothing is approved) |
| 503 | No token configured: the route is closed |

A test walks every route the app serves and fails on a state-changing one without the guards and a token check, and
on any route (bar `/health` and `/metrics`) that answers a rebinding `Host`.

## `/wall`

A plain-text page for a terminal or a kiosk: the latest report, the open items (severity, source, summary, count,
whether an ack is needed), up to 20 recommendations awaiting a decision, each with its id and revision
(`[<id> rev <N>]`, what an approve or reject names), and what a standing rule approved in the last 24 hours
(`[<id>] approved by standing rule <RULE_ID>: <title>`). Times are in
`schedule.timezone`. Everything on it came from producers or the model, so control and format characters
(terminal escapes, bidi overrides) are stripped first. It reads the store, so it works with or without a text
channel.

```bash
watch -n 60 curl -s localhost:8400/wall
```

**In a browser or kiosk.** With `server.read_auth: true`, `/wall` needs `Authorization: Bearer <TOKEN>` like the
rest of the read API, and a plain browser can't send that header. Scout deliberately takes no token in the query
string (it would end up in browser history, proxy logs and `Referer` headers) and has no login page or session
cookie; argus-cortex's `auth` module, which every suite service uses, offers only the bearer header check. So pick
one of these:

- **`read_auth: false` on a network you trust.** Bind scout to loopback (`server.host: 127.0.0.1`, the default) for
  a kiosk on the same host, or to a LAN address for one elsewhere on the LAN. The `Host` check still applies: open
  it by IP, `localhost`, or a name listed in `server.trusted_hosts`, so a DNS-rebinding page can't read it. Writes
  stay behind the token whatever `read_auth` says. Remember this opens every read route (`/api/*` GETs too) to
  whoever can reach the port, so don't do it on a network you don't control.
- **A reverse proxy that adds the header.** Keep `read_auth: true`, keep scout on loopback, and put a proxy in front
  that authenticates the person or device its own way (its login, client certificates, an allowlist) and adds
  `Authorization: Bearer <TOKEN>` to requests it forwards to `/wall`. List the name the proxy forwards in
  `server.trusted_hosts` if it passes `Host` through. Forward only `GET /wall` (and whatever else the kiosk needs),
  not the write routes, so the proxy's users can't approve or ack with the token it holds. For example, with
  nginx:

  ```nginx
  location = /wall {
      limit_except GET { deny all; }
      proxy_pass http://127.0.0.1:8400;
      proxy_set_header Authorization "Bearer <TOKEN>";
  }
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
