# Operations

Running scout day to day: where its state lives, what runs when, how to upgrade, and what to try when something
looks wrong. To get a first instance up, start with the [quickstart](../deploy/quickstart/README.md).

## Running it

```bash
argus-scout serve --config scout.yaml            # the scheduler and the HTTP API, on 127.0.0.1:8400
docker run -v ./config:/config -p 127.0.0.1:8400:8400 ghcr.io/smk762/argus-scout
```

The image runs `argus-scout serve --host 0.0.0.0 --port 8400` with `SCOUT_CONFIG=/config/scout.yaml`: mount your
config directory at `/config`, and the default store lands beside `scout.yaml`. Off loopback inside the container,
so publish the port on loopback or set `server.read_auth: true` (see [api.md](api.md#auth-model)).

Scout refuses to start on an invalid `scout.yaml`. The policy files it names are reloaded while it runs, and a bad
edit to one falls back to defaults (or skips the bad rule) and is logged, so status updates never stop for a typo.

## The store

Scout keeps everything in one SQLite file, `store.path` (default `scout.db` beside `scout.yaml`): status events,
recommendations and their audit trail, standing-rule bookkeeping, reports, delivery records, the signal rules'
pending `for` clocks, scheduler bookkeeping and the daily usage counts. It runs in WAL mode, so `scout.db-wal` and
`scout.db-shm` sit beside it; keep the three together. The server and a CLI run (`brief`, `deliver`, `poll --ingest`)
can share the file safely.

**Migrations** are applied automatically when the store opens, each in its own transaction, and recorded in the
database's `user_version`. They only go forward: an older scout refuses a database a newer one has migrated
("database schema vN is newer than this argus-scout"), so take a backup before upgrading.

**Backups.** Copy the database with SQLite's online backup, which is safe while scout runs:

```bash
python3 -c "import sqlite3; sqlite3.connect('scout.db').backup(sqlite3.connect('scout-backup.db'))"
docker compose exec scout python -c "import sqlite3; sqlite3.connect('/config/scout.db').backup(sqlite3.connect('/config/scout-backup.db'))"
```

Or, with the `sqlite3` tool installed: `sqlite3 scout.db ".backup scout-backup.db"`. Copying `scout.db` alone
while scout is running can miss what's still in the WAL file. To restore, stop scout and put the backup in place
of `scout.db` (removing the old `-wal` and `-shm` files).

**Pruning.** Nothing is pruned automatically in this version: the store grows with every report and delivery
record, slowly. To trim it, stop scout or run this beside it:

```bash
python3 -c "
from datetime import UTC, datetime, timedelta
from argus_scout.store import SqliteStore
with SqliteStore('scout.db') as store:
    print(store.prune(datetime.now(UTC) - timedelta(days=90)))
"
```

It deletes events acked, resolved or expired before the cutoff, reports and delivery records older than it, and
usage counts of earlier days. Open events, recommendations and their audit trail are never pruned. A pruned report
can no longer be redelivered, which doesn't matter past `delivery.retry_max_age`.

## Usage caps

`budget.limits` caps calls per day, per kind; a missing kind or `0` is unlimited.

| Kind | Charged for |
|---|---|
| `llm_calls` | each report's model call, before the model is asked (a failure still counts) |
| `tts` | each speech synthesis (once per message, however many voice channels) |
| `speak` | each message spoken on the voice channels |

Counts live in the store, so a crash loop can't reset them and the server and the CLI share them: each charge is
one atomic check-and-increment. They reset at midnight in `schedule.timezone`, whatever the host's clock says. At the cap, a report comes from
its template (`fallback_reason: budget`) and speech is held back; text still goes out. Work done while serving an
HTTP request goes over the cap and is logged, unless `budget.block_manual: true`; scheduled jobs and the CLI are
always held to it. Watch `scout_usage_used` and `scout_usage_limit` on `/metrics`. An older scout's `usage.json`
beside the store is imported once and renamed.

## The scheduler and its jobs

`serve` runs these jobs:

| Job | When | What |
|---|---|---|
| `status` | every `schedule.status_interval` (300 s) | Reloads the heartbeat policy, reads the open inbox items, and writes and delivers a `status` report when the reportable ones changed or a re-announce interval passed. Skipped during rest hours. |
| `analysis` | every `schedule.analysis_interval` (900 s) | Runs the signal rules against Prometheus and feeds the recommendation engine. Does nothing without `services.prometheus_url` and `files.signals`. |
| `briefing:<kind>@<HH:MM>` | daily, at each `schedule.briefings` entry, in `schedule.timezone` | Writes and delivers that report. |
| `poll:<source>` | every source's `interval` | Polls one `json_poll` (or plugin) source and ingests what it returns. |
| `redeliver` | every `delivery.retry_interval` (300 s) | Retries reports a channel failed to take; see [channels.md](channels.md#redelivery). |

- **Restarts.** Each job's last successful run is kept in the store, so a restart doesn't repeat a briefing that
  already went out or reset an interval.
- **No catch-up storm.** After downtime an interval job runs once, and a briefing runs late only if its slot passed
  less than 30 minutes ago; older slots are skipped.
- **DST-safe.** A repeated hour fires once, and a skipped hour fires at the first real instant after it.
- **Isolated.** A job that raises is logged and counted (`scout_job_errors_total`) and retried after a minute (sooner for a poll with a shorter interval); it
  never stops the scheduler or the other jobs, and never overlaps a running copy of itself.

## Upgrading

1. Back up the store (above).
2. `pip install -U "argus-scout[server]"`, or pull the new image.
3. `argus-scout check config scout.yaml`: a renamed or removed key is refused with a message saying what replaced
   it (`services.echo_url` became `services.echo.url`, for one). Fix, check again.
4. Restart. The store migrates on open.

Read the release notes for anything that changes behaviour.

## Troubleshooting

Start with the config, then reproduce the step that looks wrong by hand. None of these commands acts on anything.

```bash
argus-scout check config scout.yaml                 # the config, every policy file, sources, channels, secrets set or unset
argus-scout analyze --config scout.yaml --dry-run   # one analysis cycle against Prometheus, on an in-memory copy of the store
argus-scout brief --config scout.yaml --no-llm      # a report from the template: is the problem the model?
argus-scout brief --config scout.yaml --dry-run     # ask the model, store nothing
argus-scout deliver --config scout.yaml --latest    # re-send the newest report; prints each channel's outcome
argus-scout poll --config scout.yaml                # poll every polling source once, store nothing
```

| Symptom | Look at |
|---|---|
| No briefings at all | `schedule.briefings` and `schedule.timezone`; `scout_job_last_success_timestamp_seconds{job=~"briefing.*"}`; the log's `briefing.due`. |
| Briefings say "Written from a template" | The reason on the last line (and `meta.fallback_reason` in `GET /api/reports/latest`): no model configured, the daily cap, no GPU lease, or the model failed. `brief --dry-run` shows the model's error. |
| No status updates | `GET /api/policy/explain`: each open item's `rule` says why it isn't reported (below `report.min_severity`, routed to `briefing`, acked, quiet or rest hours). A status update goes out only when an item is news (new, escalated or otherwise changed, or cleared and back); items already announced are repeated only when their `reannounce` interval has passed. |
| A channel gets nothing | `deliver --latest --channel <NAME>`; its `kinds`, `enabled`, and `min_severity` (a report's severity is its worst item's; one with nothing open is `info`); `scout_deliveries_total{result="failed"}`. A channel that already has a report shows `duplicate`; `--force` sends again. |
| Nothing is spoken | `services.echo` set? Rest or quiet hours, `sources.scout.channel` in the heartbeat policy, the status update's items' own voice rules (`voice.min_severity`, `speakable`, source routing, `on_change_only`), the `tts` and `speak` caps, and the voice channel's own outcome in `deliver --latest`. |
| Signals never fire | `analyze --dry-run`: "pending" means a rule is waiting out its `for`, "unknown" means Prometheus couldn't answer the query (try it in Prometheus's UI). A rejected proposal isn't raised again until it escalates or its `rejection_ttl` passes. |
| Pushes are refused | 403: `server.read_only` or an untrusted `Origin`; 503: no token set; 401: wrong token. See [api.md](api.md#auth-model). |
| A policy edit had no effect | `check heartbeat` (or `approvals`, `signals`, `tariff`, `persona`) on the file: a bad file falls back to defaults and the log says why. |
