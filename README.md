# argus-scout

Homelab intelligence for the Argus suite. Scout reads your telemetry, reasons about it, and reports back with
briefings and recommendations. **It never acts**: it reads Prometheus and the status events you send it, and never
touches the hosts it reports on. Anything it recommends is carried out by a person.

Part of the [Argus suite](https://github.com/smk762/argus-cortex/blob/dev/SUITE.md). Where
[argus-herald](https://github.com/smk762/argus-herald) broadcasts out to the public, scout reports in to you.

> **Status: pre-alpha.** The policy core is here (below). Prometheus signals, LLM briefings, delivery channels and the
> HTTP API are being built in phases; see the development epic in this repo's issues.

## What's here

| Module | What it does |
|---|---|
| `argus_scout.heartbeat` | The policy that decides whether a status event is worth interrupting for: report and voice thresholds, quiet and rest hours, per-source routing (voice, text, briefing only), re-announce intervals. Every decision names the rule that made it. |
| `argus_scout.approvals` | Standing approvals: rules that auto-approve recurring low-risk recommendations. They only ever approve, never deploy, and proposals with high impact or a critical signal are never auto-approved, whatever the rules say. |
| `argus_scout.recommendations` | The recommendation lifecycle (draft, proposed, approved, rejected, deployed, retired) with an approval audit trail. |
| `argus_scout.budget` | Daily usage caps for LLM generations, speech syntheses and spoken deliveries, persisted so a crash loop can't reset them. Background work stops at the cap; requests a person makes still go through unless you block them too. |
| `argus_scout.tariff` | Time-of-use energy tariffs in any currency: the active period, the next change, block-rate tiers, and gaps in coverage. |
| `argus_scout.events` | The status-event envelope any source sends to scout. |

## Install

```bash
uv pip install argus-scout            # library + CLI
uv pip install "argus-scout[server]"  # + the HTTP server
```

## Try it

Each config file has a commented example in [`examples/`](examples/). Copy one, edit it, and check it before scout loads
it:

```bash
argus-scout check tariff examples/tariff.yaml
argus-scout check heartbeat examples/heartbeat.yaml
argus-scout check approvals examples/approvals.yaml
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
