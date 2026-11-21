# Config reference

Every key `scout.yaml` takes. This page is generated from the schema (`argus_scout.config`) by `make docs`, and a
test fails if it drifts, so edit the model's descriptions, not this file. For a commented starter, run
`argus-scout init`; to check a file, `argus-scout check config scout.yaml`.

## How the file is read

- **Strict.** An unknown key, at any level, is an error, and so is a bad value. Scout refuses to start on an
  invalid `scout.yaml` and `argus-scout check config` names each problem by its dotted path.
- **Forgiving below it.** The policy files under `files` are reloaded while scout runs, and a bad edit to one
  falls back to defaults (or skips the bad rule) and is logged, so a typo can't stop status updates.
- **Paths** that are relative resolve against the directory `scout.yaml` is in, so a config directory can be
  moved or mounted anywhere (the container mounts it at `/config`).
- **Secrets never go in the file.** A key ending in `_env` names an environment variable; the value lives in a
  `.env` beside `scout.yaml` or in the real environment, which wins. `check config` reports each one as set or
  unset, never its value.
- **Empty sections.** A section whose keys are all commented out reads as its defaults, and an empty list as `[]`.
- **Types.** *path*: a file path, relative to the config's directory. *env var name*: the name of a variable
  (`SCOUT_API_TOKEN`), never the secret. *URL*: `http://` or `https://`; a trailing slash is dropped. Durations are
  seconds.

## Top level

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `files` | [files](#files) | section defaults |  | The policy files scout reads (reloaded while it runs). |
| `store` | [store](#store) | section defaults |  | Where scout keeps its state. |
| `sources` | list of [sources: webhook](#sources-webhook) or [sources: json_poll](#sources-json_poll) or [plugin kind](#plugin-kinds) | `[]` |  | Where status events come from. |
| `channels` | list of [channels: text](#channels-text) or [channels: webhook](#channels-webhook) or [plugin kind](#plugin-kinds) | `[]` |  | Where reports go. |
| `schedule` | [schedule](#schedule) | section defaults |  | Cycle intervals, the timezone and the briefings. |
| `services` | [services](#services) | section defaults |  | Prometheus, the model and argus-echo. |
| `budget` | [budget](#budget) | section defaults |  | Daily usage caps. |
| `delivery` | [delivery](#delivery) | section defaults |  | The redelivery sweep. |
| `server` | [server](#server) | section defaults |  | The HTTP API. |

## files

The policy files scout reads. Each is optional: a missing one means built-in defaults.

Check each one with `argus-scout check <kind> FILE`, or all of them with `argus-scout check config`. The examples are in [`examples/`](../examples/).

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `heartbeat` | path or null | `null` |  | The heartbeat policy (YAML): what is worth interrupting for, quiet and rest hours, per-source routing. Unset, or a bad file: the built-in defaults. See `examples/heartbeat.yaml`. |
| `approvals` | path or null | `null` |  | Standing approvals (YAML): rules that move matching proposals to `approved`, never further. Unset: none. See `examples/approvals.yaml`. |
| `tariff` | path or null | `null` |  | A time-of-use energy tariff (YAML), for the narrative, `/metrics` and the tariff-aware signal rules. Unset: no tariff. See `examples/tariff.yaml`. |
| `signals` | path or null | `null` |  | Signal rules (YAML): PromQL questions that raise recommendations. Unset: the analysis cycle does nothing. See `examples/signals.yaml`. |
| `persona` | path or null | `null` |  | The narrator's persona (YAML). Unset, or a bad file: the neutral built-in persona. |
| `memory` | path or null | `null` |  | A directory of operator-written markdown notes folded into briefings. Scout reads them and writes only its own recent-rejections file there (see `argus_scout.memory`). |

```yaml
files:
  heartbeat: heartbeat.yaml
  approvals: approvals.yaml
  tariff: tariff.yaml
  signals: signals.yaml
  persona: persona.yaml
  memory: memory/
```

## store

Where scout keeps events, recommendations, approvals and reports.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `path` | path | `scout.db` |  | The SQLite database file, created (with its directory) on first use and migrated on open. `:memory:` keeps nothing across restarts. |

```yaml
store:
  path: scout.db
```

## sources

The keys every source has. A source plugin's `config_model` subclasses this (see `argus_scout.sources`), narrowing `kind` to its own name and adding its own keys.

A list. Every entry has these keys; each kind adds its own below. A bad entry fails only itself (and `argus-scout check config`): the other sources still load. Guide: [sources.md](sources.md).

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `kind` | text | required |  | `webhook`, `json_poll`, or a kind an installed plugin provides. |
| `name` | text | required |  | Unique among sources. It is the `source` of every event this source produces, and the `{source}` in `POST /api/events/{source}`. |

### sources: webhook

Producers POST status events to scout.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `kind` | `webhook` | required |  | Selects this kind. |
| `token_env` | env var name or null | `null` |  | Env var holding the bearer token this source must send. Unset means the server-wide token applies. |

```yaml
sources:
  - kind: webhook
    name: ci
    token_env: SCOUT_CI_TOKEN
```

### sources: json_poll

Scout GETs a JSON endpoint on an interval and maps its fields onto status events.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `kind` | `json_poll` | required |  | Selects this kind. |
| `url` | URL | required |  | The status endpoint to GET. Only this host and port are allowed (a LAN address is fine); a redirect to another host must pass the public-address rule. |
| `interval` | number | `60.0` | > 0 | Seconds between polls. |
| `items` | text or null | `null` |  | Dotted path to the list of items in the response (`data.alerts`). Unset: the response is one item. |
| `fields` | map of text to text | `{}` |  | StatusEvent field -> dotted path within an item, e.g. `{summary: labels.alertname}`. `source` can't be mapped: a polled source's events are always its own. |
| `id` | text or list of text or null | `null` |  | Dotted path (or a list of them) to what identifies an item, e.g. `[labels.alertname, labels.instance]`. The event id is derived from those values, so set it whenever two items can share a summary. Unset: see `argus_scout.sources.json_poll` for the default rule. |
| `resolve_missing` | boolean | `true` |  | Close (`resolved`) this source's open events that a successful poll no longer lists. Turn it off for an endpoint that reports only changes rather than the full current state. |
| `auth_header` | text | `Authorization` |  | The header `auth_env`'s value is sent in. |
| `auth_env` | env var name or null | `null` |  | Env var holding the value sent in `auth_header` (for example `Bearer abc...`). |

```yaml
sources:
  - kind: json_poll
    name: alerts
    url: http://alertmanager:9093/api/v2/alerts
    interval: 60
    fields:
      summary: annotations.summary
      severity: labels.severity
    id: [labels.alertname, labels.instance]
```

## channels

The keys every channel has. A channel plugin's `config_model` subclasses this (see `argus_scout.channels`), narrowing `kind` to its own name and adding its own keys.

A list. Every entry has these keys; each kind adds its own below. A bad entry fails only itself: the other channels still load. Guide: [channels.md](channels.md).

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `kind` | text | required |  | `text`, `webhook`, `snapcast`, `mask`, or a kind an installed plugin provides. |
| `name` | text | required |  | Unique among channels. Delivery records are keyed by it, so renaming a channel makes it a new one. |
| `enabled` | boolean | `true` |  | `false` keeps the entry but sends nothing. |
| `min_severity` | `info` \| `notice` \| `warning` \| `critical` | `info` |  | Deliver only messages at or above this severity. A report's severity is that of the worst item in it (a briefing also counts its open recommendations); a report with nothing open is `info`. |
| `kinds` | list of text | `[]` |  | Deliver only these kinds (report kinds such as `morning`, or `event` for an inbox item). Empty: all. |
| `timeout` | number | `30.0` | > 0, <= 600 | Seconds one delivery may take, retries included. |

### channels: text

Append each message to a file under `dir`. The file is fenced under `dir`: no `..`, no absolute path, no symlink out.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `kind` | `text` | required |  | Selects this kind. |
| `dir` | path | `.` |  | The root everything is written under. Relative to the config's directory. |
| `file` | text | `scout-reports.log` |  | Relative to `dir`. `{kind}` and `{date}` (YYYY-MM-DD, UTC) are filled in per message. |
| `rotate_bytes` | integer or null | `null` | > 0 | Rotate the file (`x.log` -> `x.log.1` ...) before it would grow past this size. Unset: never. |
| `keep` | integer | `3` | >= 1, <= 100 | Rotated files to keep. |

```yaml
channels:
  - kind: text
    name: log
    file: scout-{date}.log
    rotate_bytes: 1048576
```

### channels: webhook

POST each message as JSON to `url` (only that host is allowed).

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `kind` | `webhook` | required |  | Selects this kind. |
| `url` | URL | required |  | Where to POST. Only this host and port are contacted (a LAN receiver is fine); redirects are never followed. |
| `token_env` | env var name or null | `null` |  | Env var holding a bearer token to send. Unset: no Authorization header. |
| `secret_env` | env var name or null | `null` |  | Env var holding an HMAC-SHA256 key; set, every POST carries an `X-Scout-Signature` header. |
| `retries` | integer | `2` | >= 0, <= 5 | Retries after a failed attempt (connection error, 408, 429 or 5xx). Any other 4xx is final and permanent: the redelivery sweep won't retry it either. |
| `backoff` | number | `1.0` | >= 0, <= 60 | Seconds before the first retry; doubles each time, up to 60. |

```yaml
channels:
  - kind: webhook
    name: chat
    url: https://hooks.example.com/scout
    secret_env: SCOUT_CHAT_SECRET
    kinds: [morning, evening]
```

### channels: snapcast

A Snapcast pipe stream to speak into, and optionally the snapserver's JSON-RPC to set it up.

Needs the `snapcast` extra: `pip install "argus-scout[snapcast]"`.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `kind` | `snapcast` | required |  | Selects this kind. |
| `timeout` | number | `120.0` | > 0, <= 600 | Seconds the whole delivery may take, playback included. |
| `fifo` | path | required |  | The pipe stream's FIFO. Must exist and be a FIFO: scout never creates it. |
| `sample_format` | text | `'48000:16:2'` |  | The stream's `sampleformat`: `rate:bits:channels`, with 16, 24 or 32 bits (24 kept in 4 bytes, as Snapcast does). |
| `open_timeout` | number | `2.0` | >= 0, <= 30 | Seconds to keep retrying a FIFO with no reader before failing. |
| `tail_ms` | integer | `250` | >= 0, <= 5000 | Silence written after the speech, so the last word isn't cut by snapcast's chunking. |
| `control` | text or null | `null` |  | Snapserver JSON-RPC: `tcp://host:port` (control port, 1705) or `http(s)://host:port/jsonrpc` (1780). |
| `stream` | text or null | `null` |  | Switch the groups to this stream (the pipe stream's `name`) while speaking. |
| `groups` | list of text | `[]` |  | Group ids or names to set up. Empty: every group. |
| `volume` | integer or null | `null` | >= 0, <= 100 | Client volume, in percent, while speaking. Unset: leave volumes alone. |
| `unmute` | boolean | `true` |  | Unmute muted groups and clients while speaking. |
| `restore_delay` | number | `1.5` | >= 0, <= 30 | Seconds after the last byte is written before restoring: snapcast plays its buffer behind the pipe. |
| `control_timeout` | number | `5.0` | > 0, <= 60 | Seconds per JSON-RPC call. |

```yaml
channels:
  - kind: snapcast
    name: house
    fifo: /run/snapcast/scout.fifo
    control: tcp://snapserver:1705
    stream: scout
    volume: 60
```

### channels: mask

An argus-mask server and the face to speak through.

Needs the `mask` extra: `pip install "argus-scout[mask]"`.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `kind` | `mask` | required |  | Selects this kind. |
| `timeout` | number | `120.0` | > 0, <= 600 | Seconds the whole delivery may take: mask answers a push when the speech ends. |
| `url` | URL | required |  | The argus-mask server. Only that host and port are contacted, with no redirects. |
| `face` | text | required | matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` | A face id or alias the server serves. |
| `wait_ready` | number | `0.0` | >= 0, <= 300 | Seconds to wait for a ready screen before skipping. 0: skip at once. |
| `poll_interval` | number | `1.0` | > 0, <= 30 | Seconds between readiness checks while waiting. |
| `seed` | integer | `0` | >= 0, <= 2147483647 | The idle layer's seed. |
| `token_env` | env var name or null | `null` |  | Env var holding a bearer token, for a proxy in front of mask. Unset: no Authorization header. |
| `transport` | `http` \| `websocket` | `http` |  | How speech reaches mask: `http` (`POST /push`) or `websocket` (its source socket, whose hello sets the disclosure label for a cloned voice). The same host and port either way: `ws://` or `wss://` from `url`. |
| `allow_cloned_voice` | boolean | `false` |  | http only: push speech in a cloned voice (`SpeechAudio.cloned`) anyway. Off by default: `/push` can't set mask's disclosure label, so a face would speak in a possibly real person's voice with no disclosure. Use `transport: websocket` instead, which discloses it. Ignored over websocket. |
| `cloned_voice_consented` | boolean | `false` |  | websocket only: setting this attests that the owner of the cloned voice (`services.echo.voice_clone_id`) consented to its use. Cloned speech is then pushed with mask's disclosure label, which every viewer sees as "An AI voice of a real person, used with their consent": mask displays your claim. Scout can't verify it. Unset, cloned speech is skipped over websocket. Synthetic voices are unaffected. |

```yaml
channels:
  - kind: mask
    name: face
    url: http://mask:8000
    face: lobby
```

### Plugin kinds

`sources` and `channels` entries of any other `kind` belong to an installed plugin (entry-point groups
`argus_scout.sources` and `argus_scout.channels`). Here only `kind` and `name` are checked and every other key is
kept; the plugin's own `config_model` then validates the whole entry, with paths resolved against the config's
directory like the built-in kinds. An unknown kind, or an entry its plugin rejects, fails only that entry and is
reported by `argus-scout check config`. `snapcast` and `mask` are built-in plugin kinds of this sort, validated by
the models above. Writing one: [sources.md](sources.md#writing-a-source-plugin) and
[channels.md](channels.md#writing-a-channel-plugin).

## schedule

When scout's jobs run. A restart neither repeats a briefing that already went out nor resets an interval.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `analysis_interval` | number | `900.0` | > 0 | Seconds between analysis cycles. |
| `status_interval` | number | `300.0` | > 0 | Seconds between status cycles. |
| `timezone` | text | `UTC` |  | An IANA zone name (`Europe/London`). Briefing times, quiet and rest hours and tariff periods are read in it. |
| `briefings` | list of [schedule.briefings](#schedulebriefings) | `[]` |  | Scheduled reports, one job each. A kind may appear at several times, never twice at the same time. A slot missed while scout was down runs late only if it passed less than 30 minutes ago. |

```yaml
schedule:
  analysis_interval: 900
  status_interval: 300
  timezone: Europe/London
  briefings:
    - {kind: morning, at: "07:30"}
    - {kind: evening, at: "19:00"}
```

### schedule.briefings

A scheduled report, e.g. `{kind: morning, at: "07:30"}` in the schedule's timezone.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `kind` | `morning` \| `evening` \| `status` | required |  | Selects this kind. |
| `at` | text | required |  | 24-hour `HH:MM` in `schedule.timezone` (quote it in YAML: `"07:30"`). |

## services

Services scout talks to. All optional; a missing one turns off what depends on it.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `prometheus_url` | URL or null | `null` |  | Prometheus's root (`http://prometheus:9090`), queried read-only over the PromQL HTTP API. Scout never scrapes or connects to the hosts it reports on. Unset: no signal rules run. |
| `llm` | [services.llm](#servicesllm) or null | `null` |  | The model reports are written with. Unset: every report comes from the plain template. |
| `echo` | [services.echo](#servicesecho) or null | `null` |  | argus-echo for speech. Unset: nothing is synthesised, so voice channels stay silent; text still goes out. |

```yaml
services:
  prometheus_url: http://prometheus:9090
```

### services.llm

The model the narrative is written with. See `argus_scout.llm`. `ollama` talks to Ollama's `/api/chat`. `openai` talks to any OpenAI-compatible `/chat/completions` (a local server, or a hosted provider with `api_key_env`); its `base_url` includes the API prefix, e.g. `http://llm:8000/v1`.

Guide: [narrative.md](narrative.md).

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `backend` | `ollama` \| `openai` | required |  | `ollama` (Ollama's `/api/chat`) or `openai` (any OpenAI-compatible `/chat/completions`). |
| `base_url` | URL | required |  | The model server. For `openai`, include the API prefix: `http://llm:8000/v1`. |
| `model` | text | required |  | The model name the server knows it by. |
| `api_key_env` | env var name or null | `null` |  | Env var holding an API key, sent as a bearer token (hosted backends). Unset: no key. |
| `timeout` | number | `120.0` | > 0 | Seconds before one model call gives up. |
| `max_tokens` | integer | `400` | > 0 | Most tokens one report may generate. |
| `context_tokens` | integer | `3072` | >= 1024 | The model's context window; prompts are cut to fit it with the output. |
| `temperature` | number or null | `null` | >= 0, <= 2 | Sampling temperature. Unset: the server's default. |
| `gpu` | [services.llm.gpu](#servicesllmgpu) or null | `null` |  | Hold a GPU lease around each model call. Leave unset for a hosted backend. |

```yaml
services:
  llm:
    backend: ollama
    base_url: http://ollama:11434
    model: qwen3:8b
```

### services.llm.gpu

A GPU lease around each model call (argus-iris broker, through `argus_cortex.gpu`). See `argus_scout.gpu`. Present (even empty, `gpu: {}`) means every narrative model call holds a lease for its HTTP round-trip; absent means no lease, which is right for a hosted backend. Which coordinator grants it (none, a host lock, the broker) is the host-wide `ARGUS_GPU_*` environment, never this file. The defaults are an 8B model in Ollama and its KV cache, the figures `gpu-tenant.yaml` declares.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `min_vram_mb` | integer | `6000` | > 0 | The whole footprint: weights plus working set, MiB. |
| `model_mb` | integer or null | `5200` | >= 0 | The part of min_vram_mb that stays loaded in the model server after a call. |
| `priority` | `realtime` \| `interactive` \| `batch` \| `background` | `background` |  | Briefings can wait: the lowest class. |
| `target` | text or null | `null` |  | The broker service that serves the model, `<service>` or `<service>:<model>`. Default for ollama: `ollama:<model>`. Required for openai. |

```yaml
services:
  llm:
    backend: openai
    base_url: http://vllm:8000/v1
    model: qwen3-8b
    gpu:
      target: vllm:qwen3-8b
```

### services.echo

argus-echo, the speech server voice channels play from. See `argus_scout.channels.echo`. The voice keys are argus-echo's shared request fields, sent as they are: `voice_clone_id` (a stored clone; wins over `speaker`), `speaker` (a built-in speaker), `language`, and the Qwen3-TTS-only `instruction`, `voice_description` and `emotion` (other engines ignore them). `extra` passes engine-specific fields through (Miso's `temperature`, `topk`). Scout never holds a GPU lease around a synthesis: echo takes its own.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `url` | URL | required |  | The engine's root, e.g. `http://echo:8000`: scout calls `/health` and `/synthesise/stream` under it. |
| `voice_clone_id` | text or null | `null` |  | A clone stored on that engine (clones are per engine). Wins over `speaker`. Speech in a clone is marked as cloned: the `mask` channel discloses it over `transport: websocket` when `cloned_voice_consented` attests consent, and over http refuses it unless its `allow_cloned_voice` is set. |
| `speaker` | text or null | `null` |  | A built-in speaker (`GET /speakers` on the engine). Neither this nor a clone: the engine's default voice. |
| `language` | text | `en` |  | ISO language code. Miso speaks English only. |
| `instruction` | text or null | `null` |  | Qwen3-TTS only: a speaking instruction. Other engines ignore it. |
| `voice_description` | text or null | `null` |  | Qwen3-TTS only: a designed voice, described in words. Other engines ignore it. |
| `emotion` | text or null | `null` |  | Qwen3-TTS only. Other engines ignore it. |
| `extra` | map of text to any | `{}` |  | Engine-specific request fields, passed through (Miso: `temperature`, `topk`). |
| `format` | `pcm_s16le` \| `wav` | `pcm_s16le` |  | The audio handed to voice channels: raw 16-bit little-endian PCM, or the same in a WAV container. |
| `timeout` | number | `60.0` | > 0, <= 600 | Seconds one synthesis may take, model warm. |
| `load_timeout` | number | `180.0` | > 0, <= 1800 | Seconds one synthesis may take when the model has to load first. |
| `health_timeout` | number | `5.0` | > 0, <= 60 | Seconds the readiness check may take. |
| `max_seconds` | number | `300.0` | > 0, <= 3600 | The longest speech accepted, in seconds. |

```yaml
services:
  echo:
    url: http://echo:8000
    speaker: <SPEAKER>
    format: pcm_s16le
```

## budget

Daily usage caps (see `argus_scout.budget`). `limits` maps a kind to calls per day; a missing kind or `0` is unlimited. The narrative charges each model call to `llm_calls`.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `limits` | map of text to integer | `{}` | each value >= 0 | Kind -> calls per day. Scout charges `llm_calls` (each report's model call), `tts` (each speech synthesis) and `speak` (each spoken message). Counts live in the store and reset at midnight in `schedule.timezone`. |
| `block_manual` | boolean | `false` |  | Refuse over-cap work done while serving an HTTP request too. Off: such work goes through and is logged. Scheduled jobs and the CLI are always held to the caps. |

```yaml
budget:
  limits:
    llm_calls: 48
    tts: 100
```

## delivery

The redelivery sweep (`argus_scout.channels.redeliver`): a report a channel failed to take is tried again on an interval, until it succeeds, the channel refuses it permanently, or the report is too old.

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `retry_interval` | number | `300.0` | > 0 | Seconds between redelivery sweeps. |
| `retry_max_age` | number | `21600.0` | > 0 | Seconds after a report was written that it is still retried. |
| `max_attempts` | integer | `5` | >= 1, <= 100 | Failed attempts per channel before giving up. |
| `speak_max_age` | number | `600.0` | >= 0 | Seconds after a report was written that a retry may still speak it. Older than this, a retry goes to text channels only: late speech is worse than none. `0`: never re-speak. |

```yaml
delivery:
  retry_interval: 300
  max_attempts: 5
  speak_max_age: 600
```

## server

The HTTP API that `argus-scout serve` runs. The defaults are closed: loopback only, read-only, and with no token set every write is refused.

Guide: [api.md](api.md).

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `host` | text | `127.0.0.1` |  | Bind address (`serve --host` overrides it). Reads are open unless `read_auth`: keep it on loopback. |
| `port` | integer | `8400` | >= 1, <= 65535 | TCP port (`serve --port` overrides it). |
| `read_only` | boolean | `true` |  | Refuse every state-changing route (webhook pushes included). Turn off only once a token is set. |
| `allowed_origins` | list of text | `[]` |  | Browser origins on *another* host trusted to make writes (`https://ui.example`). No `Origin` header (curl, scripts) and a same-host origin whose host is an IP, `localhost` or in `trusted_hosts` are always allowed; any other cross-origin write is refused. |
| `trusted_hosts` | list of text | `[]` |  | Host names (no scheme or port) scout may be reached by, as in `http://scout.lan:8400`. Every request whose `Host` is not an IP address, `localhost` or listed here is refused with a 400, so a DNS-rebinding page can't read the API. A name listed here is also trusted as a same-host `Origin` for writes. `/health` and `/metrics` answer any `Host`. |
| `token_env` | env var name or null | `SCOUT_API_TOKEN` |  | Env var holding the bearer token for write routes. Unset or empty means every write route is closed. |
| `read_auth` | boolean | `false` |  | Require the same bearer token on the read API (`/api/*` GETs and `/wall`). Off by default: scout binds to loopback, where reaching the port is already host access. Turn it on before binding anywhere else. `/health` and `/metrics` stay open either way. |

```yaml
server:
  host: 127.0.0.1
  port: 8400
  read_only: false
  token_env: SCOUT_API_TOKEN
```

## Environment

Not in `scout.yaml`, on purpose:

| Variable | Read by | Meaning |
|---|---|---|
| `SCOUT_CONFIG` | `argus-scout serve` | The config file when `--config` isn't given. The container image sets `/config/scout.yaml`. |
| the variables `*_env` keys name | scout | Secrets: tokens, API keys, HMAC keys. |
| `ARGUS_GPU_COORDINATOR`, `ARGUS_GPU_BROKER_URL`, `ARGUS_GPU_BROKER_TOKEN`, `ARGUS_GPU_TENANT_MANIFEST`, `ARGUS_GPU_LEASE_TIMEOUT_S`, ... | argus-cortex | The host-wide GPU coordinator every argus service on the host shares, used when `services.llm.gpu` is set. See [narrative.md](narrative.md#gpu-leases). |
