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
  recorded as a failure. The other channels are unaffected, and the next delivery of that message tries the
  failed channel again.
- **Voice follows the heartbeat policy.** Voice channels are a separate plugin kind. Scout synthesises speech
  once per message and gives the same audio to every voice channel. During rest hours nothing is spoken. During
  quiet hours only critical and `quiet_exempt` messages are spoken. A report is kept off the speakers when
  `sources.scout.channel` in the heartbeat file is `text`. Text channels are never held back by these rules.

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
| `retries` | `2` | Retries after a connection error, a 429 or a 5xx. Other 4xx responses and redirects are final. |
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
