# Delivery channels

Scout delivers every report it writes (scheduled briefings and status reports) to the channels in `scout.yaml`.
Channels only deliver: none of them acts on a monitored host, and the only outbound HTTP is to a channel's own
configured URL, through `argus_cortex.net`.

```yaml
channels:
  - kind: text
    name: log
    dir: .                         # relative to scout.yaml
    file: scout-reports.log
  - kind: webhook
    name: chat
    url: https://<HOOK_HOST>/scout
    token_env: SCOUT_CHAT_TOKEN
    secret_env: SCOUT_CHAT_SECRET
    min_severity: notice
    kinds: [morning, evening, event]
```

Check the file with `argus-scout check config scout.yaml`. Re-send the newest report with
`argus-scout deliver --config scout.yaml --latest [--kind morning] [--channel chat] [--force]`.

## Keys every channel has

| Key | Default | Meaning |
|---|---|---|
| `kind` | required | `text`, `webhook`, or a kind an installed plugin provides |
| `name` | required | Unique among channels. Delivery records are keyed by it, so renaming a channel makes it new. |
| `enabled` | `true` | `false` keeps the entry but sends nothing |
| `min_severity` | `info` | Send only messages at or above `info` / `notice` / `warning` / `critical` |
| `kinds` | all | Send only these kinds: report kinds (`morning`, `evening`, `status`), or `event` for a single inbox item |
| `timeout` | `30` | Seconds one delivery may take, retries included |

## How a message goes out

- **Once per channel.** Each attempt is recorded in the store's `deliveries` table, which holds at most one
  successful row per message and channel. Delivering the same report again (a retry, the CLI, a restart) skips
  the channels that already have it. `--force` sends again.
- **Concurrently and in isolation.** Every channel runs at the same time under its own timeout. A channel that
  fails, hangs or raises is logged, counted (`scout_deliveries_total{channel,result}` on `/metrics`) and
  recorded as a failure. The other channels are unaffected, and the redelivery sweep (below) tries the failed
  channel again later.
- **Voice follows the heartbeat policy.** Voice channels are a separate plugin kind. Scout synthesises speech
  once per message and gives the same audio to every voice channel. During rest hours nothing is spoken. During
  quiet hours only critical and `quiet_exempt` messages are spoken. A report is kept off the speakers when
  `sources.scout.channel` in the heartbeat file is `text`. Text channels are never held back by these rules.

## Speech: argus-echo

Voice channels play speech that scout gets from [argus-echo](https://github.com/smk762/argus-echo). Configure
it under `services`; without it, voice channels stay silent and text still goes out.

```yaml
services:
  echo:
    url: http://echo:8000          # an engine's root: Miso, XTTS-v2 or Qwen3-TTS (same contract)
    voice_clone_id: <CLONE_ID>     # a stored clone; or speaker: <SPEAKER> for a built-in one
    language: en
    format: pcm_s16le              # what voice channels receive: pcm_s16le or wav
```

| Key | Default | Meaning |
|---|---|---|
| `url` | required | The engine's root URL. Only that host and port are contacted. |
| `voice_clone_id` | unset | A clone stored on that engine (clones are engine-specific). Wins over `speaker`. |
| `speaker` | unset | A built-in speaker (`GET /speakers` on the engine). Neither: the engine's default voice. |
| `language` | `en` | ISO code. Miso speaks English only. |
| `instruction`, `voice_description`, `emotion` | unset | Qwen3-TTS only; other engines ignore them |
| `extra` | `{}` | Engine-specific request fields, passed through (Miso: `temperature`, `topk`) |
| `format` | `pcm_s16le` | The audio voice channels receive: raw 16-bit little-endian PCM, or the same in a WAV container |
| `timeout` | `60` | Seconds one synthesis may take with the model loaded |
| `load_timeout` | `180` | Seconds one synthesis may take when echo has to load the model first |
| `health_timeout` | `5` | Seconds the readiness check may take |
| `max_seconds` | `300` | The longest speech accepted; the response is capped to match |

**Breaking change:** `services.echo_url` was replaced by `services.echo.url`. A config that still has
`echo_url` is refused with a message saying so.

How scout uses echo's contracts:

- **Readiness (resident contract).** Before each synthesis scout reads `GET /health` (argus-cortex's
  `ResidentHealth`). An engine that doesn't answer, or reports `model_state: error`, fails in seconds. One that
  is `unloaded` or `loading` (or answers `503` while loading) gets `load_timeout`, because echo loads its model
  on first use. Scout never calls `/admin/*`: placing models on GPUs is argus-iris's job.
- **Synthesis (shared HTTP contract).** One `POST /synthesise/stream` per message, with the voice fields above.
  The PCM layout is read from the `X-Sample-Rate`, `X-Audio-Format` and `X-Channels` headers, not assumed, and
  float32 is converted to 16-bit. A stream that ends mid-frame (the engine failed while speaking) is a failure.
- **Once per message.** The same audio goes to every voice channel, so three speakers cost one synthesis.
- **No GPU lease.** Scout never holds a GPU lease around synthesis. Echo takes its own.
- **Failures.** If echo is down, busy (`503`) or refuses the request, the failure is recorded on every voice
  channel and text channels are unaffected. A `4xx` other than 408 or 429 (a bad voice, say) is permanent.
  Anything else is retried by the redelivery sweep.

Echo has no speaker of its own, so there is no `echo` channel. Voice channels such as Snapcast and argus-mask
play the audio.

## Redelivery

A failed delivery is retried later by the `redeliver` job:

```yaml
delivery:
  retry_interval: 300      # seconds between sweeps
  retry_max_age: 21600     # stop retrying a report this many seconds after it was written
  max_attempts: 5          # failed attempts per channel before giving up
  speak_max_age: 600       # older than this, a retry goes to text channels only
```

- Only **reports** are retried, and only on the channels that **failed**. A channel the report was never tried
  on (filtered, held back by quiet hours, or added to the config later) isn't backfilled.
- A webhook's own retries count as one attempt. After `max_attempts` failed attempts, or once the report is older
  than `retry_max_age`, the sweep gives up on that channel.
- A **permanent** failure is never retried: a `4xx` other than 408 or 429 from a webhook or from echo.
- **No late speech.** A report older than `speak_max_age` is retried on text channels only, because a briefing
  spoken an hour late is worse than none. `0` means a retry is never spoken. A voice retry still passes the
  heartbeat gates, so quiet hours hold it back like any other delivery.
- A retry goes through the same idempotency as every delivery. A success writes exactly one `ok` row, and a channel
  that already has the message isn't sent it again.

## `text`

Appends each message to a file under `dir`, so you can read it with `tail -f`.

| Key | Default | Meaning |
|---|---|---|
| `dir` | `.` (beside `scout.yaml`) | The root. Everything is written under it. |
| `file` | `scout-reports.log` | Path relative to `dir`. `{kind}` and `{date}` (YYYY-MM-DD, UTC) are filled in. |
| `rotate_bytes` | unset | Rotate (`x.log` → `x.log.1` …) before the file grows past this size |
| `keep` | `3` | Rotated files kept |

The file is fenced under `dir`. A `..` or an absolute path is refused when the config loads, and every write
resolves the path again with symlinks followed. A symlink that leads out of `dir` is refused, and the file is
opened with `O_NOFOLLOW`. A kind filled into `{kind}` is reduced to safe characters first. Control and format
characters, such as terminal escapes and bidi overrides, are stripped from the text before it is written.

Each entry looks like this:

```
=== 2026-11-21T07:30:00+00:00  morning  [info]  report:42 ===
<report text>

```

The `/wall` view reads the latest report from the store, not from this file, so it works with or without a
text channel.

## `webhook`

POSTs each message as JSON to `url`. Only that host and port are allowed. A receiver on the LAN is fine, but a
redirect is never followed.

| Key | Default | Meaning |
|---|---|---|
| `url` | required | `http(s)://` URL |
| `token_env` | unset | Env var holding a token, sent as `Authorization: Bearer <token>` |
| `secret_env` | unset | Env var holding an HMAC-SHA256 key, used for `X-Scout-Signature` |
| `retries` | `2` | Retries after a connection error, a 408, a 429 or a 5xx. Redirects are final. Any other 4xx is final and **permanent**: the redelivery sweep won't retry it either. |
| `backoff` | `1.0` | Seconds before the first retry. It doubles each time, up to 60. |

If `token_env` or `secret_env` names a variable that isn't set, nothing is sent and the delivery is recorded as a
failure. Scout never sends the message without the credentials you configured.

### Headers

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `User-Agent` | `argus-scout/<version>` |
| `X-Scout-Delivery` | The message's `delivery_id`. A message always carries the same id, so a receiver can drop repeats. |
| `X-Scout-Timestamp` | Unix seconds at send time |
| `X-Scout-Signature` | Sent only with `secret_env`: `sha256=` followed by the hex HMAC-SHA256 of `<timestamp>.<raw body>` |

To verify a signature, recompute it over the exact bytes you received and compare in constant time. Reject a
timestamp that is too old.

```python
import hashlib, hmac
expected = "sha256=" + hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
ok = hmac.compare_digest(expected, request.headers["X-Scout-Signature"])
```

### Payload (`argus-scout.delivery`, schema version 1)

```json
{
  "schema": "argus-scout.delivery",
  "schema_version": 1,
  "delivery_id": "report:42",
  "channel": "chat",
  "type": "report",
  "kind": "morning",
  "severity": "info",
  "title": "morning report",
  "text": "Good morning. Two things need a look ...",
  "speakable": "Good morning. Two things need a look ...",
  "generated_at": "2026-11-21T07:30:00+00:00",
  "sent_at": "2026-11-21T07:30:02+00:00",
  "report_id": 42,
  "event": null,
  "meta": {"path": "llm"}
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema`, `schema_version` | string, int | Always `argus-scout.delivery`. The version goes up only on an incompatible change. Fields may be added without a bump. |
| `delivery_id` | string | `report:<id>` for a report. For a single inbox item it is `event:<source>/<event_id>:<content hash>:<announce count>`, so a changed or re-announced item gets a new id. |
| `channel` | string | The channel's `name` |
| `type` | `report` \| `event` | A stored report, or one inbox status item |
| `kind` | string | Report kind (`morning`, `evening`, `status`), or `event` |
| `severity` | `info` \| `notice` \| `warning` \| `critical` | |
| `title` | string | Short label |
| `text` | string | The message. Report text may contain markdown. |
| `speakable` | string | The text to read aloud, with markdown stripped. Empty means the message is not to be spoken. |
| `generated_at`, `sent_at` | ISO 8601 UTC | |
| `report_id` | int \| null | Set when `type` is `report` |
| `event` | object \| null | Set when `type` is `event`: `{source, event_id, requires_ack, link}` |
| `meta` | object | The report's metadata as stored, for example `path`, which is `llm` or `template` |

The response body is read (64 KiB cap) and ignored. Any 2xx counts as delivered.

## Plugin channels

Another package adds a channel kind through the `argus_scout.channels` entry-point group:

```toml
[project.entry-points."argus_scout.channels"]
matrix = "my_package.scout:MatrixChannel"
```

The class has a `kind`, a `config_model` (a pydantic model, normally subclassing
`argus_scout.config.ChannelSettings`), and is built with one validated config entry. It implements
`async deliver(message: Delivery) -> DeliveryResult`. A voice channel also implements
`async play(audio: SpeechAudio, message: Delivery) -> DeliveryResult` and receives audio that scout has already
synthesised. A voice channel never synthesises speech itself. A plugin that fails to import, an entry its model
rejects, or a constructor that raises disables that one channel. The others still load, and
`argus-scout check config` reports the error.
