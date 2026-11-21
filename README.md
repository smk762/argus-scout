# argus-scout

Homelab intelligence for the Argus suite. Scout reads your telemetry, reasons about it, and reports back with
briefings and recommendations.

- **It never acts.** Scout proposes; a person decides and carries it out. A standing approval can move a
  recommendation to `approved` and no further, and nothing in scout deploys, dispatches or executes anything.
- **Prometheus is the source of truth.** Scout asks Prometheus over PromQL and reads the status events producers
  send it. It never scrapes or connects to the hosts it reports on.
- **A bad edit never silences it.** The policy files reload while it runs, and a broken one falls back to defaults.

Part of the [Argus suite](https://github.com/smk762/argus-cortex/blob/dev/SUITE.md). Where
[argus-herald](https://github.com/smk762/argus-herald) broadcasts out to the public, scout reports in to you.

> **Status: pre-alpha.** Everything below is built and tested; see the development epic in this repo's issues for
> what's left before v0.1.

## Quickstart

Scout, Prometheus, a node exporter and Ollama with a small model, with text delivery working out of the box:

```bash
git clone https://github.com/smk762/argus-scout && cd argus-scout/deploy/quickstart
docker compose up -d --build                                  # or: podman compose up -d --build
docker compose exec scout tail -f /config/scout-reports.log   # the first briefing, a minute or so in
curl -s localhost:8400/wall                                   # the latest report and open items
docker compose down -v                                        # stop and delete the volumes
```

GPU, model choice, turning on writes, adding an event source and cleanup: [deploy/quickstart/README.md](deploy/quickstart/README.md).

## How it fits together

```
Prometheus ── PromQL, read-only ──▶ signal rules ──▶ recommendation engine ──▶ proposed ──▶ approved
                                    threshold          dedup, expiry,         │      by a person, or a
                                    tariff_shift       rejection memory       │      standing rule; never
                                    tariff_plan                               │      further
status events ──▶ inbox ──▶ heartbeat policy                                  │
webhook, json_poll,         report, voice,                                    │
plugins                     quiet and rest hours                              │
                                   │                                          │
                                   ▼                                          ▼
           tariff, notes, persona ──▶ narrative: morning, evening, status reports
                                      (a model via argus-cortex, or a plain template)
                                                     │
                                                     ▼
                                      store ──▶ channels: text · webhook · Snapcast · argus-mask
                                                (speech from argus-echo, synthesised once per message)
```

## Features

- **Signal rules in YAML**: a PromQL query, a threshold, how long it must hold, escalation steps and a
  recommendation template, with safe placeholders. Tariff-aware kinds suggest shifting load to a cheaper period
  and compare plans against your real usage. [Signals](docs/signals.md)
- **A recommendation engine** that refreshes rather than duplicates, retires what went stale, remembers what you
  rejected (and why), and reads a Prometheus outage as "unknown", never "all clear".
- **Standing approvals** for recurring low-risk advice, which can never approve a high-impact or critical one.
- **Status events from anywhere**: producers push JSON to a webhook, or scout polls any JSON endpoint and maps its
  fields, with per-source tokens. Other kinds are plugins. [Sources](docs/sources.md)
- **A heartbeat policy** that decides what is worth interrupting for, and explains every decision.
- **Briefings and status updates** written by a local or hosted model from scout's facts only, with prompts sized
  for small models, under a GPU lease when the GPU is shared, and from a plain template whenever the model can't:
  a briefing is never skipped. [Narrative](docs/narrative.md)
- **Delivery channels**: a text file, signed webhooks, Snapcast speakers and argus-mask faces, each message at most
  once per channel, retried when a channel fails. Other kinds are plugins. [Channels](docs/channels.md)
- **An HTTP API** that is closed by default, a plain-text `/wall`, a policy explainer and `/metrics`.
  [API](docs/api.md)
- **Daily usage caps** for model calls and speech that a crash loop can't reset, and a store that survives
  restarts. [Operations](docs/operations.md)

## Install

```bash
pip install "argus-scout[server]"     # the CLI and the server: what most installs want
```

| Extra | Adds |
|---|---|
| (none) | the library and the CLI: `check`, `analyze`, `brief`, `deliver`, `poll`, `init` |
| `server` | `argus-scout serve`: the HTTP API, the scheduler and `/metrics` |
| `snapcast` | the `snapcast` voice channel |
| `mask` | the `mask` voice channel |
| `cli` | `rich`, for nicer `--help` |

The container image is `ghcr.io/smk762/argus-scout`; to try everything at once, use the [Quickstart](#quickstart)
above.

## Configure

One file, `scout.yaml`, names every other file scout reads and every service it talks to. Secrets never go in it:
it names an environment variable, and the value lives in a `.env` beside it or in the real environment.

```bash
argus-scout init config/                    # a commented config/scout.yaml and config/.env.example
cp config/.env.example config/.env
argus-scout check config config/scout.yaml  # validates it and every file it names
argus-scout serve --config config/scout.yaml
```

Each policy file has a commented example in [`examples/`](examples/): `heartbeat.yaml`, `approvals.yaml`,
`tariff.yaml`, `signals.yaml` and `persona.yaml`.

## Docs

| Guide | Covers |
|---|---|
| [Config reference](docs/config.md) | every `scout.yaml` key: type, default, constraints, example (generated from the schema) |
| [Sources](docs/sources.md) | the status event envelope, webhook pushes, JSON polling, writing a source plugin |
| [Signals](docs/signals.md) | threshold and tariff rules, templates, how the engine decides, standing approvals |
| [Narrative](docs/narrative.md) | report kinds, model backends, caps and fallback, GPU leases, persona, memory |
| [Channels](docs/channels.md) | text, webhook, argus-echo speech, Snapcast, argus-mask, redelivery, writing a channel plugin |
| [HTTP API](docs/api.md) | routes, the auth model, `/wall`, `/api/policy/explain`, `/metrics` |
| [Operations](docs/operations.md) | the store, backups and pruning, usage caps, the scheduler, upgrading, troubleshooting |
| [CLI reference](docs/cli.md) | every command and option (generated) |
| [Quickstart](deploy/quickstart/README.md) | Prometheus, Ollama and scout in one compose file |

## Develop

```bash
make install   # venv + editable install with the "dev" extras
make test
make lint
make docs      # regenerate docs/cli.md and docs/config.md after changing the CLI or the config schema
```

Versions come from git tags (`hatch-vcs`). CI runs the shared [`argus-ci`](https://github.com/smk762/argus-ci)
workflows, and a `v*` tag publishes to PyPI and GHCR. Scaffolded from
[`argus-pkg-template`](https://github.com/smk762/argus-pkg-template); `copier update` pulls template changes.

## License

MIT
