# Delivery channels

Scout delivers every report it writes (scheduled briefings and status reports) to the channels in `scout.yaml`.
Channels only deliver: none of them acts on a monitored host, and the only outbound HTTP is to a channel's own
configured URL, through `argus_cortex.net`. Every key is also listed in the [config reference](config.md#channels).

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
    min_severity: warning          # only reports about something at warning or worse
```

Check the file with `argus-scout check config scout.yaml`. Re-send the newest report with
`argus-scout deliver --config scout.yaml --latest [--kind morning] [--channel chat] [--force]`.

## Keys every channel has

| Key | Default | Meaning |
|---|---|---|
| `kind` | required | `text`, `webhook`, `snapcast`, `mask`, or a kind an installed plugin provides |
| `name` | required | Unique among channels. Delivery records are keyed by it, so renaming a channel makes it new. |
| `enabled` | `true` | `false` keeps the entry but sends nothing. The entry is still validated, but the channel is never built, so a disabled `snapcast` or `mask` needs no extra installed. Config is read at startup: enabling one means editing the file and restarting. |
| `min_severity` | `info` | Send only messages at or above `info` / `notice` / `warning` / `critical` |
| `kinds` | all | Send only these kinds: report kinds (`morning`, `evening`, `status`), or `event` for a single inbox item |
| `timeout` | `30` | Seconds one delivery may take, retries included |

**What gets sent in this version.** Scout delivers reports: the scheduled briefings and the status updates the
status cycle writes when open items are worth reporting. Inbox items reach channels inside those status updates;
the `event` message type exists in the interface and the webhook payload, but nothing sends single items yet.
A report is delivered at the severity of the worst item in it: for a status update, its items; for a briefing,
its open items and the recommendations awaiting a decision or approved. A briefing with nothing open is `info`.
So `min_severity: warning` gets status updates and briefings that mention something at warning or worse, and
`kinds` chooses which kinds of report a channel gets at all. The report's `title` names it (a status update's
names its worst item), and it counts as `quiet_exempt` when any item in it is.

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
  quiet hours only critical and `quiet_exempt` messages are spoken (a report counts as critical or exempt
  when an item in it is). A report is kept off the speakers when `sources.scout.channel` in the heartbeat file is
  `text`. A status update speaks only its items the heartbeat voice rules allow (source routed to `voice`,
  `speakable`, at or above `voice.min_severity`, and with `voice.on_change_only` only those that are news),
  and is not spoken at all when none is. Each item says its `speech` when the event has one, else its summary
  (see [sources.md](sources.md#the-status-event)). Text channels are never held back by these rules.

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
| `url` | required, unless `peer` is set | The engine's root URL. Only that host and port are contacted. |
| `peer` | unset | Used only when `url` is left out: `xtts` (XTTS-v2) reads the suite-wide `ARGUS_XTTS_URL`, `tts` (Qwen3-TTS) reads `ARGUS_TTS_URL`. See below. |
| `voice_clone_id` | unset | A clone stored on that engine (clones are engine-specific). Wins over `speaker`. |
| `speaker` | unset | A built-in speaker (`GET /speakers` on the engine). Neither: the engine's default voice. |
| `language` | `en` | ISO code. Miso speaks English only. |
| `instruction`, `voice_description`, `emotion` | unset | Qwen3-TTS only; other engines ignore them |
| `speed` | unset | Speaking rate (`1.0` is normal, up to `4`). Reserved by echo's contract: no engine applies it yet. |
| `extra` | `{}` | Engine-specific request fields, passed through (Miso: `temperature`, `topk`). A shared field (any key above that goes in the request) is refused here: set it as its own key. |
| `format` | `pcm_s16le` | The audio voice channels receive: raw 16-bit little-endian PCM, or the same in a WAV container |
| `timeout` | `60` | Seconds one synthesis may take with the model loaded |
| `load_timeout` | `180` | Seconds one synthesis may take when echo has to load the model first |
| `health_timeout` | `5` | Seconds the readiness check may take |
| `max_seconds` | `300` | The longest speech accepted; the response is capped to match |

**The suite-wide URL.** Every argus service on a host can share one name per service: `ARGUS_XTTS_URL` for the
XTTS-v2 engine and `ARGUS_TTS_URL` for Qwen3-TTS, set in the environment, the `.env` beside `scout.yaml` or the
suite settings file (`~/.config/argus/suite.env`). Scout reads one only when `url` is left out and `peer` says which
engine you mean, because clones and speakers are per engine:

```yaml
services:
  echo:
    peer: xtts                     # no url: read ARGUS_XTTS_URL
    voice_clone_id: <CLONE_ID>
```

A `url` in `scout.yaml` always wins. With neither a `url` nor the variable, or with a value that isn't an absolute
http(s) URL, `check config` fails and names the variable. Miso has no suite-wide name: give its `url`.

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

Echo's contract has no flag saying a voice is a clone, so scout decides from its own request: speech made with
a `voice_clone_id` is marked as cloned. The `mask` channel shows it with mask's disclosure label over
`transport: websocket` when you attest consent (`cloned_voice_consented: true`), and over http refuses it unless `allow_cloned_voice` is set (see `mask` below).

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
| `delivery_id` | string | `report:<id>` for a report. For a single inbox item (not sent in this version) it is `event:<source>/<event_id>:<content hash>:<announce count>`, so a changed or re-announced item gets a new id. |
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

## Voice channels and speech audio

`snapcast` and `mask` play speech that scout has already synthesised (once per message, shared by every voice
channel). Each is an optional extra, because both need numpy to convert the audio:

```bash
pip install "argus-scout[snapcast]"   # or [mask], or both: "argus-scout[snapcast,mask]"
```

Without the extra, that channel fails to start with `install argus-scout[snapcast]` (or `[mask]`) in
`argus-scout check config`, and every other channel still works.

The synthesiser's audio may be `wav` (8, 16, 24 or 32-bit integer PCM, or 32/64-bit float, including
`WAVE_FORMAT_EXTENSIBLE`), `pcm_s16le` or `pcm_f32le` (raw, described by the audio's `sample_rate` and
`channels`). Other formats, such as `mp3` or `ogg`, are refused with a clear error. Configure argus-echo to return
WAV or raw PCM. Channel counts are converted (mono is copied to every channel, anything else is averaged), and the
sample rate is converted by linear interpolation, which is plenty for speech.

A voice channel may answer **skipped** when its device isn't ready, such as no screen watching an argus-mask face.
A skipped delivery is not a failure and isn't recorded, so a later delivery of the same message (the CLI, say)
tries again. The redelivery sweep never retries a skip on its own: a skip doesn't count toward `max_attempts`, and a
channel that only ever skipped has no failure to repair. If a channel failed first and then skipped on a retry,
the sweep keeps trying it under the usual limits, and never speaks it once the report is older than
`speak_max_age`.

## `snapcast`

Plays speech on Snapcast speakers by writing PCM into a snapserver **pipe stream**'s FIFO, optionally setting up
the groups over Snapcast's JSON-RPC first and restoring them afterwards.

```yaml
- kind: snapcast
  name: house
  fifo: /run/snapcast/scout.fifo
  sample_format: "48000:16:2"
  control: tcp://snapserver:1705
  stream: scout
  volume: 60
```

The matching snapserver stream:

```ini
[stream]
source = pipe:///run/snapcast/scout.fifo?name=scout&sampleformat=48000:16:2
```

| Key | Default | Meaning |
|---|---|---|
| `fifo` | required | The pipe stream's FIFO. A relative path resolves beside `scout.yaml`. |
| `sample_format` | `48000:16:2` | The stream's `sampleformat`: `rate:bits:channels`. Bits are 16, 24 or 32. Snapcast keeps 24-bit samples in 4 bytes, and so does scout. |
| `open_timeout` | `2` | Seconds to keep retrying a FIFO that has no reader, then fail |
| `tail_ms` | `250` | Silence written after the speech, so the last word isn't cut off |
| `control` | unset | Snapserver JSON-RPC: `tcp://host:1705` (the control port) or `http://host:1780/jsonrpc`. Unset: no control, audio only. |
| `stream` | unset | Switch the groups to this stream (its `name`) while speaking, then switch them back |
| `groups` | all | Group ids or names to set up |
| `volume` | unset | Client volume (percent) while speaking, then restored |
| `unmute` | `true` | Unmute muted groups and clients while speaking, then mute them again |
| `restore_delay` | `1.5` | Seconds after the last byte before restoring, for the audio still in Snapcast's buffer |
| `control_timeout` | `5` | Seconds per JSON-RPC call |
| `timeout` | `120` | Seconds the whole delivery may take, playback included |

**The FIFO.** The path must already exist and be a FIFO. A missing path or a regular file is refused, and scout
never creates anything there. It opens the FIFO with `O_NONBLOCK`, so a FIFO with no reader fails at once
(`ENXIO`) instead of blocking. Scout retries for `open_timeout` seconds and then fails the delivery. Writes wait on
`select` against a deadline (the speech's length plus 10 s), so a snapserver that stops reading fails the delivery
too. A dead snapserver can't hang scout.

**Control.** With `control` set, scout reads `Server.GetStatus`, then for each selected group calls
`Group.SetStream`, `Group.SetMute` and `Client.SetVolume` only where something needs to change. Once the speech has
played out, it undoes each change in reverse order, whether playback worked or not. A failed setup fails the
delivery after putting back whatever had changed. A failed restore is logged and noted in the result. JSON-RPC goes
to the `control` host only. Snapserver is a delivery target, not a monitored host.

## `mask`

Speaks through an [argus-mask](https://github.com/smk762/argus-mask) face: lip-synced on whatever screen is
watching it. It uses mask's published agent contracts: HTTP `POST /push` by default, or mask's source socket with
`transport: websocket`, which is the only way to show a cloned voice with mask's disclosure label.

```yaml
- kind: mask
  name: face
  url: http://mask:8000
  face: <FACE_ID>
  wait_ready: 0
  transport: websocket   # recommended if scout ever speaks in a cloned voice
  cloned_voice_consented: false   # true attests the clone's owner consented; mask shows that claim
```

| Key | Default | Meaning |
|---|---|---|
| `url` | `ARGUS_MASK_URL` | The argus-mask server. Only that host and port are contacted, with no redirects. Left out, it is the suite-wide `ARGUS_MASK_URL` (the environment, the `.env` beside `scout.yaml`, or the suite settings file); with neither, or a value that isn't an absolute http(s) URL, the channel is refused and `check config` names the variable. |
| `face` | required | A face id or alias the server serves |
| `wait_ready` | `0` | Seconds to wait for a ready screen. `0` skips at once. |
| `poll_interval` | `1` | Seconds between readiness checks while waiting |
| `seed` | `0` | The idle layer's seed |
| `token_env` | unset | Env var holding a bearer token, for a reverse proxy in front of mask. If it is set in the config but missing from the environment, nothing is sent. |
| `timeout` | `120` | Seconds the whole delivery may take. Mask answers a push when the speech ends. |
| `transport` | `http` | `http` (`POST /push`) or `websocket` (the source socket, `ws://` or `wss://` from `url`). See below. |
| `cloned_voice_consented` | `false` | websocket only: your attestation that a cloned voice's owner consented. Mask displays that claim. Unset, cloned speech is skipped. See below. |
| `allow_cloned_voice` | `false` | http only: push speech in a cloned voice with no disclosure. Not recommended: see below. |

**Readiness** comes from `GET /health`, whichever transport is used. The face is ready when the driver is
available, the server isn't read-only, no other source is speaking through the face (`live`), and at least one
screen watching it reports `audio_ready`. Screens are keyed by the name the viewer used, so an alias and its face
both match. A screen with house audio (`sound: external`, the sound on Snapcast) counts as ready. If the face isn't
ready, the delivery is **skipped**, unless `wait_ready` gives it time. A read-only server or a driver that is down
is a failure.

**Speech over http** (`transport: http`) goes as `POST /push?face=<face>&seed=<seed>&require_screen=true` with raw
16-bit PCM and the headers `X-Sample-Rate`, `X-Audio-Format: pcm_s16le` and `X-Channels`. Mask never sees the text.
`require_screen` makes mask refuse with a 409 if the screen went away after the readiness check, and scout reports
that as skipped too, so speech never goes to a face no one can hear. Mask caps a push at 25 MB, about 9 minutes of
24 kHz mono speech.

**Speech over the socket** (`transport: websocket`) goes to `WS /session/<face>/source`:

1. Scout sends a JSON hello: `{"sample_rate": …, "channels": …, "format": "s16le", "seed": …, "disclose": …}`.
   `disclose` is `true` exactly when the speech is in a cloned voice, and `false` otherwise. A cloned voice gets
   this far only with `cloned_voice_consented: true`.
2. Mask answers `{"type": "ready", …}` once its driver is open. If the face already has a source, mask answers
   `{"type": "error", …}` and closes, and the delivery is **skipped**. Any other refusal (a driver that is down, a
   face it doesn't serve, a read-only server) is a failure.
3. Scout sends the PCM as binary messages of at most 64 KB each, paced at real time plus a one-second lead, as
   mask's own client does. Then it sends `{"type": "end"}`.
4. Mask answers `{"type": "done", "frames": …, "duration_ms": …}` and closes. A socket that closes before `done`,
   or an `error` mid-speech, is a failure.

The socket has no `require_screen`, so the `/health` check just before connecting is its only readiness check. A
screen that goes away in the second between the check and the speech isn't caught as it is over http.

The socket can't go through `argus_cortex.net`, so scout applies the same rules itself. The configured host is
resolved once, and the addresses are checked with `argus_cortex.net.check_url`, with only that host allowed. Scout
then connects to those addresses and nowhere else. `Host` and TLS SNI stay the configured name, and a redirect is
refused, not followed. Environment proxies are ignored and compression is off. A message from mask is capped at
64 KB, since mask only sends small JSON. The connect and handshake have a 10 s limit, `ready` has 30 s, and the
speech has its length plus 15 s, capped at `timeout`. Scout sends no `Origin` header, which mask's origin guard
admits as a non-browser client.

argus-mask has no authentication of its own. Reaching its port is access, so keep it on a trusted network or behind
a proxy (`token_env`, sent on both transports). Mask takes its own GPU lease (argus-iris) for the stream, and scout
takes none.

**Cloned voices are disclosed on your attestation, or refused.** argus-echo can speak in a cloned voice
(`services.echo.voice_clone_id`), which may be a real person's. Speech synthesised with a `voice_clone_id` is marked
as cloned. Built-in speakers and Qwen's designed voices aren't clones, and none of this applies to them.

- Over **websocket**, a cloned voice is pushed only when you set `cloned_voice_consented: true`. That setting is
  your attestation that the voice's owner consented. The speech then goes with `disclose: true`, and every screen
  watching the face, and every recording, shows mask's fixed label: *An AI voice of a real person, used with their
  consent.* Mask displays your claim, and scout can't verify it. Without the attestation, the channel **skips**
  cloned speech (permanently, so it's never retried) with a reason naming the option, before contacting mask at
  all. `allow_cloned_voice` plays no part here.
- Over **http**, `/push` has no way to carry that label. The channel **skips** cloned speech (permanently, so it's
  never retried) with a reason saying why, before contacting mask at all. `cloned_voice_consented` doesn't change
  that.
- `allow_cloned_voice: true` makes http push the clone anyway, with **no disclosure**. Scout logs a warning
  (`channel.mask.cloned_voice_undisclosed`) on every such push. It exists for a clone whose owner consented and
  whose viewers are told some other way. Prefer `transport: websocket`.

**Why it takes an attestation.** The hello's only consent field is `disclose`. It carries no consent record or
owner id. Mask checks its consent records (signed releases, revocation) only for voices it synthesises itself,
through `/speak` and `say`, from the voice's record in its Voices directory. A source on the socket is trusted to
say whether to disclose. Scout can't see whether the echo clone it asked for is consented, so it never asserts
consent on its own and doesn't invent a consent reference. Set `cloned_voice_consented: true` only for a clone of
someone who consented (mask's *A consenting speaker*, covered by a release). A *Personal use* clone shown on a face
would claim a consent that doesn't exist, so use a synthetic `speaker` instead.

The `snapcast` channel plays cloned voices: it is audio-only house playback that you configured for your own
speakers, and there's no face presenting the voice as a person. If that matters in your house, use a synthetic
`speaker` for scout.

To play the same speech in the house and on the face, configure both channels and open the face's viewer with
`audio=external`. Scout synthesises once and both channels get the same audio.

## Writing a channel plugin

Another package adds a channel kind through the `argus_scout.channels` entry-point group. The entry point's name
is the kind, and its value is the channel class:

```toml
[project.entry-points."argus_scout.channels"]
matrix = "my_package.scout:MatrixChannel"
```

```python
from typing import Literal

from argus_scout.channels import ChannelError, Delivery, DeliveryResult
from argus_scout.config import ChannelSettings, EnvName, HttpUrl


class MatrixConfig(ChannelSettings):
    kind: Literal["matrix"]
    url: HttpUrl
    room: str
    token_env: EnvName


class MatrixChannel:
    kind = "matrix"
    config_model = MatrixConfig

    def __init__(self, config: MatrixConfig) -> None:
        self.config = config

    async def deliver(self, message: Delivery) -> DeliveryResult:
        ...  # send message.text (markdown) or message.speakable (plain)
        return DeliveryResult(ok=True, detail="posted to the room")
```

- **`config_model`** is a pydantic model, normally a subclass of `argus_scout.config.ChannelSettings`, which brings
  `name`, `enabled`, `min_severity`, `kinds` and `timeout`. It must keep `name` and narrow `kind` to your kind.
  Scout validates the `channels:` entry with it (relative `ConfigPath`s resolve beside `scout.yaml`) and constructs
  the class once with the result.
- **`async deliver(message)`** gets a `Delivery`: `type` (`report` or `event`), `kind`, `key` (stable per message,
  for idempotency on the receiving end), `text`, `speakable`, `severity`, `title`, `generated_at`, `report_id` and
  `meta`. The dispatcher has already applied `enabled`, `min_severity`, `kinds` and the once-per-channel rule, and
  runs the call under the channel's `timeout`.
- **Failing.** Raise `ChannelError("why")`, or return `DeliveryResult(ok=False, error="why")`; any other exception
  counts as a failure too. The failure is recorded and the redelivery sweep tries again later. When retrying can't
  help (the receiver refused the message), use `ChannelError("why", permanent=True)` and it won't be retried.
- **Declining.** A device that isn't ready returns `DeliveryResult(ok=True, status="skipped", detail="why")`: not a
  failure and not recorded, so a later delivery of the same message tries again.
- **A voice channel** also implements `async play(audio: SpeechAudio, message: Delivery) -> DeliveryResult`. It gets
  audio scout has already synthesised (once, shared by every voice channel), plays it, and never synthesises speech
  itself. Its `deliver` is never called for text; return a failure from it. Respect `audio.cloned`: a channel that
  presents a voice as a person, as a face does, should refuse cloned speech unless its config allows it.
- **Delivery only.** A channel sends to the place its config names and nothing else: no action on a monitored
  host, and outbound HTTP only to its own configured URL, through `argus_cortex.net`.

A plugin that fails to import, an entry its model rejects, or a constructor that raises disables that one channel.
The others still load, and `argus-scout check config` reports the error. A plugin can't take over a built-in kind.
