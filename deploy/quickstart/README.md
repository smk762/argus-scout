# argus-scout quickstart

Scout, Prometheus with a node exporter, and Ollama with a small model, in one compose file. Text delivery works
out of the box: the first report lands in `config/scout-reports.log` and at `/wall` within a minute or two of
`up`. Scout stays advisory here as everywhere: it reads Prometheus and writes reports, and never acts.

```bash
cd deploy/quickstart
docker compose up -d --build
docker compose exec scout tail -f /config/scout-reports.log    # Ctrl-C to stop following
curl -s localhost:8400/wall
```

`podman compose` works the same way. Needs Docker Compose v2 (or podman-compose), about 11 GB of disk (the Ollama
image is most of it, the model 1.3 GB), and network access to pull them the first time.

## What runs

| Service | Image | What it does |
|---|---|---|
| `scout` | built from this checkout | The server and scheduler. Port 8400 on loopback. Mounts `./config` at `/config`. |
| `first-brief` | the scout image | One-shot: writes a morning briefing once scout is healthy and delivers it, then exits. |
| `prometheus` | `prom/prometheus` | Scrapes node exporter (`job="node"`) and scout's own `/metrics`. Port 9090 on loopback. |
| `blackbox` | `prom/blackbox-exporter` | Only with `compose.iris.yaml`: probes each suite service's `/health` for Prometheus. |
| `node-exporter` | `prom/node-exporter` | Real data for the example signal rules. |
| `ollama` | `ollama/ollama` | The model server. Models live in the `ollama-models` volume. |
| `ollama-pull` | `ollama/ollama` | One-shot: pulls the model scout.yaml names, then exits. |

Every image is pinned to an exact version in `compose.yaml`. Scout depends on Prometheus being healthy and Ollama
being started, not on the model being pulled: until the pull finishes, reports come from scout's plain template
(they say so at the bottom). The first briefing usually beats the download and is a template one; ask for another
once `ollama-pull` has exited:

```bash
docker compose exec scout argus-scout brief --config /config/scout.yaml --kind morning
docker compose exec scout argus-scout deliver --config /config/scout.yaml --latest
```

`brief` writes and stores a report; `deliver` sends the newest one to the channels. The `first-brief` service runs
the same two commands, and writes a fresh briefing on every `up`; delete the service if you don't want that.

Ports taken? Every published port can be moved:
`SCOUT_PUBLISH=127.0.0.1:18400 PROMETHEUS_PUBLISH=127.0.0.1:19090 docker compose up -d`.

## Where reports land

- `config/scout-reports.log`: the text channel. Every briefing and status update is appended (rotated at 1 MiB).
- `GET /wall` (`curl -s localhost:8400/wall`): the latest report and the open items as plain text.
- `GET /api/reports/latest?kind=morning`: the newest report as JSON.
- `config/scout.db`: scout's store (events, recommendations, reports, usage counts).

Scout runs as root in its container by default, which suits rootless podman (container root is you). With
rootful Docker the files it writes in `config/` are then root's. To own them, set `SCOUT_USER` before the first
`up`:

```bash
SCOUT_USER=$(id -u):$(id -g) docker compose up -d --build
```

## The schedule

`config/scout.yaml` runs the signal rules and checks for status events every 60 seconds (the defaults are 15 and 5
minutes), and writes morning and evening briefings at 07:30 and 19:00 UTC. Set `schedule.timezone` to yours.

A status update is written when there are open status events worth reporting (the heartbeat policy decides).
The quickstart has no event sources until you add one; see below. The signal rules raise recommendations, which
the next briefing covers. A fresh host usually trips none of them, which is the right answer.

Try the rules against Prometheus without writing anything:

```bash
docker compose exec scout argus-scout analyze --config /config/scout.yaml --dry-run
```

`cheaper-plan` reports as unknown: it needs an energy meter metric the quickstart doesn't have.

## Model choice and GPU

The default model is `llama3.2:1b` (about 1.3 GB), small enough for a CPU. Its briefings are short and sometimes
odd; a 3B-8B instruct model is noticeably better and wants a GPU. To change it, edit both places together (a test
checks they agree):

- `compose.yaml`: the `ollama-pull` command, e.g. `["pull", "llama3.2:3b"]`
- `config/scout.yaml`: `services.llm.model`

then `docker compose up -d` again. Pull more by hand with `docker compose exec ollama ollama pull <MODEL>`.

With an NVIDIA GPU and the NVIDIA Container Toolkit installed, add the override file:

```bash
docker compose -f compose.yaml -f compose.gpu.yaml up -d
```

Any model call over `services.llm.timeout` (180 s here) or the daily cap (`budget.limits.llm_calls`) falls back
to the template, so a slow model never costs you a briefing.

## Shared settings: the suite file

If other Argus services run on this host, the values they share (the GPU broker's `ARGUS_GPU_BROKER_URL` and
`ARGUS_GPU_BROKER_TOKEN`, the suite-wide service URLs such as `ARGUS_XTTS_URL` and `ARGUS_MASK_URL`) can live once in
the suite settings file, `~/.config/argus/suite.env` (mode 600; `python -m argus_cortex.settings --init` creates
it), instead of in `config/.env`. The override file `compose.suite.yaml` mounts it read-only at
`/etc/argus/suite.env`:

```bash
COMPOSE_FILE=compose.yaml:compose.suite.yaml docker compose up -d
```

It is a separate file because compose cannot make a bind mount optional: a host without a suite file could not
start with the mount in `compose.yaml`. `ARGUS_SUITE_ENV` names another path. Anything set in `config/.env` or the
container's environment still wins.

Known limit ([argus-cortex#218](https://github.com/smk762/argus-cortex/issues/218)): argus-cortex reads the file
only when root or the user the process runs as owns it, and a mode-600 file can be opened only by its owner. So
today the mounted file must be owned by root. Otherwise it is ignored with a warning, and scout runs as if there
were none.

The `scout` container carries the labels argus-iris's agents build the suite's service directory from:
`argus.peer: scout`, `argus.service: argus-scout`, `argus.health: /health` and `argus.port: "8400"`.

## The whole suite, through argus-iris

`config/scout.yaml` has a `suite_status` source: scout asks Prometheus about every target with an `argus_peer`
label and reports each service's health and version. Out of the box that is one service, scout itself, which
`prometheus.yml` labels:

```bash
docker compose exec scout argus-scout brief --config /config/scout.yaml --kind suite
```

To watch every Argus service on your hosts, let Prometheus find them through
[argus-iris](https://github.com/smk762/argus-iris), whose `/directory/sd` lists each running container labelled
`argus.peer`. Put your broker's address in `prometheus.iris.yml` (replace `iris:8120`, twice), then add the
override file:

```bash
docker compose -f compose.yaml -f compose.iris.yaml up -d
```

It swaps in `prometheus.iris.yml` (one job scraping each service's `/metrics`, one probing each `/health`) and
adds a blackbox exporter with the `argus_health` module from `blackbox.yml`. Prometheus does the probing; scout
only reads the result. When a service goes down, stays degraded or changes version, scout raises a status event,
and the suite report lists them all. The details are in [docs/suite.md](../../docs/suite.md).

## Turning on writes

The HTTP API refuses every write by default (`server.read_only: true`, and no token). To accept status events,
acks and approvals:

1. `cp config/.env.example config/.env` and set `SCOUT_API_TOKEN` (generate one with
   `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`).
2. In `config/scout.yaml`, set `server.read_only: false`.
3. `docker compose restart scout`.

The port is published on loopback only. Before publishing it anywhere else, set `server.read_auth: true` so
`/api/*` reads and `/wall` need the token too.

## Adding a webhook source

Status events come from sources. Uncomment the `ci` webhook source in `config/scout.yaml`, turn on writes (above),
restart scout, and push an event:

```bash
curl -s -X POST localhost:8400/api/events/ci \
  -H "Authorization: Bearer $SCOUT_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"event_id": "build-42", "severity": "warning", "summary": "Nightly build failed"}'
```

Within a minute (`schedule.status_interval`) a status update appears in `config/scout-reports.log`. Give a
source its own token with `token_env: SCOUT_CI_TOKEN` and set that in `config/.env`. To poll a JSON endpoint
instead, see the `json_poll` source in the reference config (`argus-scout init`).

## Changing the policy

`config/` holds copies of the repo's `examples/` (heartbeat, approvals, tariff, signals). Edit them in place:
scout reloads them while it runs, and a bad edit falls back to defaults. Check everything with:

```bash
docker compose exec scout argus-scout check config /config/scout.yaml
```

Voice (argus-echo, Snapcast) is not part of the quickstart; see `docs/channels.md` for other channels.

## The demo: Grafana, a simulated house and a talking face

`compose.demo.yaml` adds Grafana with a provisioned dashboard and a simulated house (power loads, a GPU, an
energy meter, and a tariff whose peak is now). `compose.voice.yaml` adds argus-echo and an argus-mask face that
speaks each report. `./demo/demo.sh up [--voice]` then `./demo/demo.sh run` plays three scenarios: tariff-aware
shifting, a standing approval meeting a guardrail, and a Prometheus outage that scout reads as unknown, never as
clear. See [demo/README.md](demo/README.md).

## Cleanup

```bash
docker compose down -v                                  # containers, network, model and metrics volumes
rm -f config/scout.db* config/scout-reports.log* config/.env
```

If scout ran as root under rootful Docker, its files are root's: remove them from a container instead, then
`down -v` again for the network that creates:
`docker compose run --rm --no-deps scout sh -c 'rm -f /config/scout.db* /config/scout-reports.log*'`.
