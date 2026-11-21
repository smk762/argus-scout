# argus-scout

Homelab intelligence for the Argus suite. Scout reads your telemetry, reasons about it, and reports back with
briefings and recommendations. **It never acts**: it reads Prometheus and the status events you send it, and never
touches the hosts it reports on. Anything it recommends is carried out by a person.

Part of the [Argus suite](https://github.com/smk762/argus-cortex/blob/dev/SUITE.md). Where
[argus-herald](https://github.com/smk762/argus-herald) broadcasts out to the public, scout reports in to you.

> **Status: pre-alpha.** The policy core, signals, the HTTP API, the narrative (briefings) and text and webhook
> delivery are here. Voice channels are being built in phases; see the development epic in this repo's issues.

## What's here

| Module | What it does |
|---|---|
| `argus_scout.heartbeat` | The policy that decides whether a status event is worth interrupting for: report and voice thresholds, quiet and rest hours, per-source routing (voice, text, briefing only), re-announce intervals. Every decision names the rule that made it. |
| `argus_scout.approvals` | Standing approvals: rules that auto-approve recurring low-risk recommendations. They only ever approve, never deploy, and proposals with high impact or a critical signal are never auto-approved, whatever the rules say. |
| `argus_scout.recommendations` | The recommendation lifecycle (draft, proposed, approved, rejected, deployed, retired) with an approval audit trail. |
| `argus_scout.budget` | Daily usage caps for LLM generations, speech syntheses and spoken deliveries, persisted so a crash loop can't reset them. Background work stops at the cap; requests a person makes still go through unless you block them too. |
| `argus_scout.tariff` | Time-of-use energy tariffs in any currency: the active period, the next change, block-rate tiers, and gaps in coverage. |
| `argus_scout.events` | The status-event envelope any source sends to scout. |
| `argus_scout.signals` | Declarative signal rules in YAML: a PromQL query, a threshold, how long it must hold, a severity and a recommendation template. They replace hard-coded analyzers. Try them once with `argus-scout analyze --config scout.yaml --dry-run`. |
| `argus_scout.narrative` | Morning, evening and status reports written from structured facts (open items, recommendations, rejections and their reasons, the tariff, your notes), with prompts sized for small local models. If the model is down, over its daily cap or not configured, the report comes from a plain template instead: a briefing is never skipped. Try one with `argus-scout brief --config scout.yaml --kind morning --dry-run`. |
| `argus_scout.llm` | The model call, through argus-cortex's remote backends: Ollama, or any OpenAI-compatible API (local or hosted). Every call is charged to a daily cap, and the model's output is only ever text: nothing parses it into an action. |
| `argus_scout.persona` / `argus_scout.memory` | How reports sound (a neutral default persona; bring your own YAML) and a directory of your own markdown notes folded into briefings. Scout only reads your notes and writes just its own rejections summary there. |
| `argus_scout.channels` | Where reports go. `text` appends to a file beside your config (on by default in the starter config), and `webhook` POSTs signed JSON to one URL. Other kinds, including voice, are plugins. Each message goes to a channel at most once, even across restarts. Channels run concurrently, and one failing never holds up the others. Speech is synthesised once per message for every speaker, and follows quiet hours. See [docs/channels.md](docs/channels.md). Re-send the newest report with `argus-scout deliver --config scout.yaml --latest`. |
| `argus_scout.engine` | Turns signal hits into recommendations: one per instance (refreshed, never duplicated), retired when the signal clears or goes stale, standing approvals applied when proposed, and a rejected proposal isn't raised again unless it gets worse. If Prometheus can't answer, the result is "unknown", never "all clear". |

## Install

```bash
uv pip install argus-scout            # library + CLI
uv pip install "argus-scout[server]"  # + the HTTP server
```

## Configure

One file, `scout.yaml`, names every other file scout reads and every service it talks to. Secrets never go in it:
it names an environment variable (`token_env: SCOUT_API_TOKEN`) and the value lives in a `.env` beside it, or in the
real environment, which wins. Write a commented starter and check it:

```bash
argus-scout init config/          # writes config/scout.yaml and config/.env.example
cp config/.env.example config/.env
argus-scout check config config/scout.yaml
argus-scout serve --config config/scout.yaml
```

Relative paths resolve against the config file's directory. Scout refuses to start on an invalid `scout.yaml` (unknown
keys included); the policy files it names are reloaded while it runs, and a bad edit to one falls back to defaults.

## HTTP API

`argus-scout serve` runs the scheduler and the API (the `[server]` extra). Reaching the port is host access, so the
defaults are closed: scout binds to `127.0.0.1`, `server.read_only: true` refuses every write, and with no token set
every write is refused too.

| Method | Path | Auth | What |
|---|---|---|---|
| POST | `/api/events/{source}` | the source's `token_env`, else the server token | Push one status event or a list (256 KiB max, 100 events); all or nothing. Returns each event's inbox outcome. 404 unknown source, 405 a polled source. |
| POST | `/api/events/{source}/{event_id}/ack` | server token | `{"by": "sam"}` |
| POST | `/api/recommendations/{id}/approve` | server token | `{"by": "sam", "reason": "..."}`: a manual approval, recorded in the audit trail. Proposed only. |
| POST | `/api/recommendations/{id}/reject` | server token | `{"by": "sam", "reason": "..."}` |
| GET | `/api/events` | read auth | Open events, grouped by fingerprint |
| GET | `/api/recommendations?status=proposed` | read auth | Recommendations; `/api/recommendations/{id}` adds the history |
| GET | `/api/reports/latest?kind=morning` | read auth | The newest report |
| GET | `/api/policy/explain` | read auth | The effective heartbeat policy, where each key came from, and the rule deciding each open item now |
| GET | `/wall` | read auth | The latest report and open items as plain text |
| GET | `/health`, `/metrics` | none | |

**Writes** need `Authorization: Bearer <token>`, the value of the variable `server.token_env` names
(`SCOUT_API_TOKEN`); unset is a 503, wrong a 401. A webhook source with its own `token_env` accepts only that
token, and only for pushes as that source. Every write is also refused (403) while `server.read_only` is on, and
when a browser sends it from an origin not in `server.allowed_origins`. Bodies are capped (413). Nothing moves a
recommendation past `approved`: there is no deploy route.

**Reads** are open by default (loopback only). Set `server.read_auth: true` to require the same token on `/api/*`
and `/wall`, and do that before binding anywhere but `127.0.0.1`.

```bash
curl -s -X POST localhost:8400/api/events/nas -H "Authorization: Bearer $SCOUT_API_TOKEN" \
  -d '{"event_id": "disk", "severity": "warning", "summary": "Disk 90% full"}'
curl -s localhost:8400/wall
```

## Try it

Each policy file has a commented example in [`examples/`](examples/). Copy one, edit it, and check it before scout loads
it:

```bash
argus-scout check tariff examples/tariff.yaml
argus-scout check heartbeat examples/heartbeat.yaml
argus-scout check approvals examples/approvals.yaml
argus-scout check signals examples/signals.yaml
argus-scout tariff now examples/tariff.yaml
```

The full command reference is in [docs/cli.md](docs/cli.md).

## Develop

```bash
make install   # venv + editable install with the "dev" extras
make test
make lint
```

Versions come from git tags (`hatch-vcs`). CI runs the shared [`argus-ci`](https://github.com/smk762/argus-ci)
workflows, and a `v*` tag publishes to PyPI and GHCR. Scaffolded from
[`argus-pkg-template`](https://github.com/smk762/argus-pkg-template); `copier update` pulls template changes.

## License

MIT
