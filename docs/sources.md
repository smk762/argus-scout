# Status sources

A source is where status events come from: a CI job, a backup script, Alertmanager, anything that can say
"this is wrong" in JSON. Scout records each event in its inbox, and the heartbeat policy decides whether it's
worth a status update. Sources only produce events. Nothing a source sends can make scout act, and a poller only
fetches the one status endpoint its config names: it never connects to the hosts it reports on.

Three kinds are built in, and others come from plugins:

| Kind | Who moves the data | Use it when |
|---|---|---|
| `webhook` | the producer POSTs to scout | you control the producer (a script, a CI step, a cron job) |
| `json_poll` | scout GETs a JSON endpoint | the producer already serves its status over HTTP |
| `suite_status` | scout asks Prometheus | you want the Argus suite's own health, versions and deploys ([suite.md](suite.md)) |

Every source needs a unique `name`, which becomes the `source` of every event it produces. The full list of keys is
in the [config reference](config.md#sources). After editing, run `argus-scout check config scout.yaml`.

## The status event

Every source produces the same envelope. Only `summary` is required; the source fills in `source`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `event_id` | text, up to 200 | random | Identifies the condition. Send the same id again to update it rather than add a new one. |
| `source` | text | the source's `name` | Must match the source it arrives through, or the event is refused. |
| `severity` | `info` \| `notice` \| `warning` \| `critical` | `info` | An unknown word reads as `info`. |
| `summary` | text | required | One line; collapsed and clipped to 200 characters. `title` is accepted instead. |
| `body_md` | text | empty | Detail, in markdown. |
| `generated_at` | ISO 8601 or Unix seconds | now | Naive times are read as UTC. |
| `expires_at` | ISO 8601 or Unix seconds | none | After this the event no longer counts as open. |
| `speakable` | boolean | `true` | `false` keeps it off the speakers. |
| `speech` | text | none | What to say aloud instead of `summary` when the event is spoken (a sentence written for the ear). Control characters stripped, one line, cut to 500 characters at a word. `speakable` still decides whether it is spoken at all. Not part of the event's identity: rewording it is not news. |
| `requires_ack` | boolean | `false` | Always included in status updates until a person acks it. |
| `quiet_exempt` | boolean | `false` | Gets through quiet hours. |
| `link` | text | none | A URL for more detail. |
| `payload` | object | `{}` | Anything else, kept with the event. |
| `schema_version` | positive integer | `1` | |

Booleans also accept `"yes"`, `"on"`, `1` and the like. A field of the wrong type is refused with a message naming
it, and unknown keys are ignored.

**What happens to an event.** The inbox keys events by `(source, event_id)`. Sending one is reported back as:

| Outcome | Meaning |
|---|---|
| `new` | First sighting. |
| `duplicate` | A new id, but an open event already says the same thing (same source, severity and summary; `speech` doesn't count). Both are kept and shown as one group. |
| `escalated` | A known id whose severity went up. Reopened if it had been acked. |
| `updated` | A known id whose content changed otherwise. Reopened if it had been acked. |
| `refreshed` | A known id, unchanged. Ack and announce state are kept. |
| `recurred` | A known id that had been resolved is back. |

An event stays open until a person acks it (`POST /api/events/{source}/{event_id}/ack`), it expires, or a poller
stops seeing it (`resolve_missing`, below).

## `webhook`

```yaml
sources:
  - kind: webhook
    name: ci
    token_env: SCOUT_CI_TOKEN   # optional: this source's own token
```

The producer POSTs one envelope, or a list of up to 100, to `/api/events/<name>`:

```bash
curl -s -X POST localhost:8400/api/events/ci \
  -H "Authorization: Bearer $SCOUT_CI_TOKEN" -H 'Content-Type: application/json' \
  -d '{"event_id": "nightly", "severity": "warning", "summary": "Nightly build failed",
       "link": "https://ci.example.com/runs/42", "requires_ack": true}'
```

```json
{"source": "ci", "accepted": 1, "events": [{"event_id": "nightly", "outcome": "new", "news": true,
  "reopened": false, "previous_severity": null, "duplicate_of": []}]}
```

When the build is fixed, send the same `event_id` again at `info` (or ack it). A batch is taken whole or not at
all: one bad envelope refuses the request (422) with a message naming its index.

**Tokens.** With `token_env`, the source accepts only the token in that variable, and that token can push only as
this source: it can't ack, approve or push as another source. Without it, the server token (`server.token_env`)
applies. No token configured means the route is closed (503), never open. The server must also have writes turned
on (`server.read_only: false`); see [api.md](api.md#auth-model) for the whole model.

| Status | Why |
|---|---|
| 200 | Accepted; the body lists each event's outcome. |
| 400 | The `Host` isn't an IP, `localhost` or a `server.trusted_hosts` name, or a plugin source couldn't parse the body. |
| 401 | Missing or wrong token. Also the answer for an unknown or polled source unless the caller holds the server token, so source names can't be probed. |
| 403 | `server.read_only` is on, or a browser sent it from another origin not in `server.allowed_origins`. |
| 404 | No source by that name (with the server token). |
| 405 | The source is a poller; it takes no pushes (with the server token). |
| 413 | Body over 256 KiB. |
| 422 | Not JSON, more than 100 events, or an envelope is invalid. |
| 503 | No token is configured for this source (or, for a name that isn't a push source, no server token). |

## `json_poll`

Scout GETs a URL every `interval` seconds and maps each item in the response onto an envelope. Any producer that
serves JSON is config, not code.

```yaml
sources:
  - kind: json_poll
    name: backups
    url: http://backups:8080/status
    interval: 300
    items: jobs                      # dotted path to the list; leave out if the response is one item
    fields:                          # envelope field -> dotted path within an item
      summary: message
      severity: level
    id: name                         # what identifies an item
    auth_env: SCOUT_BACKUPS_AUTH     # optional: the value sent in auth_header
```

For a response like `{"jobs": [{"name": "photos", "level": "warning", "message": "Photos backup is 3 days old"}]}`
that gives one event, `backups:photos`.

A feed that writes a separate sentence for speech maps it onto `speech`, not `speakable` (which is a yes/no flag, so
mapping text onto it refuses the item):

```yaml
    fields:
      summary: title                 # what the text channels and /wall show
      speech: spoken                 # what a voice channel says instead, when the item is spoken
```

**Mapping.**

- `items` is a dotted path to the list (`data.alerts`). Integer segments index lists (`alerts.0.labels`). Without
  it, the whole response is one event, with the id `<name>`.
- `fields` maps envelope fields to dotted paths in an item. A field you don't map is read from the item's key of
  the same name, so an endpoint that already speaks the envelope needs no `fields`. `source` can't be mapped: a
  polled source's events are always its own, and an item naming another source is skipped.
- `id` is a dotted path, or a list of them (`[labels.alertname, labels.instance]`). The event id becomes
  `<name>:<values>`. Set it whenever the endpoint has a real identity. Without it, an item's own `event_id` is
  used, else a hash of its summary, payload and link. Severity and timestamps stay out of that hash, so a severity
  change is an escalation, not a new event. Two items that would share a derived id get ids from their whole
  contents instead, with a warning in the log telling you to set `id`.

**Cleared conditions.** With `resolve_missing: true` (the default), each successful poll is taken as the source's
whole current state: its open events that the response no longer lists are resolved. A failed poll, or one where
any item was skipped as malformed, resolves nothing. Turn it off for an endpoint that reports only changes.

**Auth and limits.** `auth_env` names the variable whose value goes in `auth_header` (default `Authorization`),
so `SCOUT_BACKUPS_AUTH=Bearer <TOKEN>` in `.env`. If the variable is unset the poll fails rather than going out
without credentials. Only the configured host and port are reachable (a LAN address is fine). A redirect to
another host must pass the public-address rule and drops the auth header. Responses are capped at 4 MiB and 500
items, and a poll gives up after 30 seconds.

**Try it** without writing anything, then for real:

```bash
argus-scout poll --config scout.yaml --source backups            # prints the events
argus-scout poll --config scout.yaml --source backups --ingest   # stores them too
```

On the server, each polling source is its own scheduler job (`poll:<name>`), so one failing endpoint never
holds up another.

## Writing a source plugin

Another package adds a source kind through the `argus_scout.sources` entry-point group. The entry point's name is
the kind, and its value is the plugin object (a class is instantiated with no arguments):

```toml
[project.entry-points."argus_scout.sources"]
mqtt = "my_package.scout:MqttSource"
```

```python
from typing import Literal

from argus_scout.config import SourceSettings
from argus_scout.events import StatusEvent
from argus_scout.sources import PollResult, SourceError


class MqttSourceConfig(SourceSettings):
    kind: Literal["mqtt"]
    broker: str
    interval: float = 60.0          # read by the scheduler for a polling source
    resolve_missing: bool = False   # true: each poll is the whole current state


class MqttSource:
    kind = "mqtt"
    config_model = MqttSourceConfig

    async def poll(self, cfg: MqttSourceConfig) -> list[StatusEvent]:
        ...  # return a list, or PollResult(events, complete=False) when part of the response was unreadable
```

- **`config_model`** is a pydantic model, normally a subclass of `argus_scout.config.SourceSettings`, that keeps
  `name` and narrows `kind` to your kind. It validates the whole `sources:` entry, with relative paths resolved
  against the config's directory if you use `argus_scout.config.ConfigPath`.
- **Pull** (`PollingSource`): `async poll(cfg)` returns events. The scheduler calls it every `interval` seconds
  (default 60). Raise `SourceError` when the source can't answer; the job is counted as failed and retried.
- **Push** (`PushSource`): `parse(cfg, body)` turns a request body into events (raise
  `argus_scout.inbox.InvalidEvent` for a bad one), and `authorize(cfg, authorization, *, fallback_env=None,
  environ=None)` raises `SourceAuthError(message, status)` unless the `Authorization` header may push as this
  source. Fail closed: no token configured is a refusal. The source is then reachable at `POST /api/events/<name>`.
- Build events with `argus_scout.inbox.parse_envelope(raw, default_source=cfg.name)` so they get the same
  validation as the built-in kinds.
- A source **only produces events**: it never ingests, announces or acts, and it never contacts the hosts it reports
  on. Fetch over HTTP through `argus_cortex.net` as the built-in poller does.

A plugin that fails to import, an entry its model rejects, or an object without `kind`, `config_model` and either
`poll` or `parse` and `authorize` disables that one source; the others still load, and `argus-scout check config`
says why. A plugin can't take over a built-in kind.
