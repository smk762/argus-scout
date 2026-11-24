"""The ``json_poll`` source: scout GETs a JSON status endpoint and maps it onto status events.

Any producer is config, not code::

    - kind: json_poll
      name: backups
      url: http://backups:8080/status
      items: data.jobs            # dotted path to the list; leave out if the response is one item
      fields:                     # status event field -> dotted path within an item
        event_id: id
        summary: message
        severity: level

A field that isn't mapped is read from the item's key of the same name, so an endpoint that already
speaks the envelope needs no ``fields`` at all. Path segments that are integers index lists
(``alerts.0.labels.name``).

**Stable ids.** A poller sees the same state on every poll, so an item's event id is derived from the item,
never random. In order:

1. ``id:`` in the config (a dotted path, or a list of them): ``<name>:<the values>`` joined by ``/`` (hashed when long,
   not plain text, padded with spaces, or when several values and one holds a ``/``, so ids never collide). Use it whenever the endpoint has a real identity, e.g. ``[labels.alertname, labels.instance]``.
   An item missing one of the paths is skipped.
2. An ``event_id`` the item carries (or ``fields`` maps), as is.
3. With no ``items`` the response *is* the event: id ``<name>``.
4. Otherwise ``<name>:<hash of the identity>``, where the identity is the mapped envelope **minus** what a
   condition changes while staying the same condition: ``severity`` (so a change is an escalation, not a new
   event), ``generated_at`` and ``expires_at`` (timestamps), ``body_md`` (prose, which usually carries the current
   reading) and the delivery flags. That leaves ``summary``, ``payload`` and ``link``. The summary is hashed as
   written (case and spacing folded), numbers included: stripping them would merge ``host1`` with ``host2``.
   If two items in one response still share an identity (the same alertname on two instances, with nothing
   else mapped), each of those gets the hash of its whole raw item instead and a warning says to set ``id:``:
   two conditions are never collapsed into one, at the cost of ids that change if those items' raw contents do.

Re-polling an unchanged item is then a refresh, and a severity change under the same identity an escalation.

**Its own events only.** Every event is this source's (``source = name``); an item that names another source
is skipped and logged, as the webhook source refuses it, so an endpoint can't speak (or reset ack state) for a
different source.

**Cleared conditions.** A successful poll lists the *whole* current state, so with ``resolve_missing`` (the
default) the poll job closes this source's open events the response no longer lists (see
:class:`~argus_scout.cycles.PollJob`). The result is a :class:`~argus_scout.sources.base.PollResult` whose
``complete`` is false when any item was skipped (or cut at :data:`MAX_ITEMS`): the job then resolves nothing,
since a skipped item's event can't be told apart from a cleared one. A failed poll raises, and resolves nothing.

**Fetching.** Through ``argus_cortex.net.afetch``: resolved addresses are checked and pinned, the body
is capped and the whole fetch has a deadline. The configured host (and port) goes in ``allow_hosts``,
because a status endpoint on the LAN is normal for a homelab; a redirect to any *other* host still
has to pass the public-address rule, and drops the auth header when it changes origin.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

import structlog
from argus_cortex.net import FetchFailed, Reply, UrlRefused, afetch

from argus_scout.config import EVENT_FIELDS, JsonPollSource, resolve_secret
from argus_scout.events import StatusEvent
from argus_scout.hosts import allow_host
from argus_scout.inbox import InvalidEvent, parse_envelope
from argus_scout.sources.base import PollResult, SourceError

log = structlog.get_logger(__name__)

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT_S = 10.0
DEADLINE_S = 30.0
MAX_ITEMS = 500

Fetcher = Callable[..., Awaitable[Reply]]
_MISSING = object()


@dataclass
class Mapped:
    """What a response mapped to: the events, and why any items were skipped."""

    events: list[StatusEvent] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    """Things that mapped but deserve a config change (colliding derived ids)."""


def lookup(data: Any, path: str) -> Any:
    """The value at a dotted ``path`` in ``data``, or a sentinel when any segment is missing."""
    value = data
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part, _MISSING)
        elif isinstance(value, list) and part.lstrip("-").isdigit() and -len(value) <= int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
        if value is _MISSING:
            return _MISSING
    return value


IDENTITY_FIELDS = ("summary", "payload", "link")
"""The envelope fields a derived id hashes (rule 4 in the module docs). Everything else is volatile."""
ID_TEXT_MAX = 120


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _configured_id(cfg: JsonPollSource, item: Any) -> str:
    paths = [cfg.id] if isinstance(cfg.id, str) else list(cfg.id or [])
    values = []
    for path in paths:
        value = lookup(item, path)
        if value is _MISSING or value is None or (isinstance(value, str) and not value.strip()):
            raise InvalidEvent(f"id: {path!r} is not in the item")
        values.append(value)
    plain = all(isinstance(v, str | int | float) and not isinstance(v, bool) for v in values)
    texts = [str(v) for v in values]
    # The readable form must be injective: a value with surrounding whitespace, or a "/" when several values
    # are joined ("a/b" + "c" vs "a" + "b/c"), would collide, so those are hashed (the hash keeps them apart).
    readable = all(t == t.strip() for t in texts) and (len(texts) == 1 or not any("/" in t for t in texts))
    text = "/".join(texts)
    if plain and readable and len(text) <= ID_TEXT_MAX:
        return f"{cfg.name}:{text}"
    return f"{cfg.name}:{_digest(values)}"


def _identity(envelope: dict[str, Any]) -> dict[str, Any]:
    identity = {k: envelope[k] for k in IDENTITY_FIELDS if envelope.get(k) not in (None, "", {})}
    if isinstance(identity.get("summary"), str):
        identity["summary"] = " ".join(identity["summary"].lower().split())
    return identity


def _envelope(cfg: JsonPollSource, item: Any) -> dict[str, Any]:
    envelope: dict[str, Any] = {}
    if isinstance(item, dict):
        envelope = {k: item[k] for k in EVENT_FIELDS if k in item}
    elif not cfg.fields:
        raise InvalidEvent(f"expected an object, got {type(item).__name__}")
    for name, path in cfg.fields.items():
        value = lookup(item, path)
        if value is _MISSING:
            envelope.pop(name, None)
        else:
            envelope[name] = value
    # Every event is this source's own. Naming another one is refused, as the webhook source does: a
    # polled endpoint must not speak (or reset ack and announce state) for a different source.
    named = envelope.get("source")
    if named is not None and str(named).strip() != cfg.name:
        raise InvalidEvent(f"source: {named!r} does not match polled source {cfg.name!r}")
    envelope["source"] = cfg.name
    if cfg.id is not None:
        envelope["event_id"] = _configured_id(cfg, item)
    return envelope


def map_response(cfg: JsonPollSource, data: Any) -> Mapped:
    """Map a decoded response onto events. Raises :class:`SourceError` if the item list is missing;
    a malformed item is skipped (and reported in ``skipped``) so one bad item can't hide the rest."""
    single = cfg.items is None
    if single:
        items = [data]
    else:
        items = lookup(data, cfg.items)
        if items is _MISSING:
            raise SourceError(f"{cfg.name}: items path {cfg.items!r} is not in the response")
        if not isinstance(items, list):
            raise SourceError(f"{cfg.name}: items path {cfg.items!r} is {type(items).__name__}, not a list")
    mapped = Mapped()
    if len(items) > MAX_ITEMS:
        mapped.skipped.append(f"{len(items) - MAX_ITEMS} items past the first {MAX_ITEMS} were ignored")
        items = items[:MAX_ITEMS]
    parsed: list[tuple[Any, bool, StatusEvent]] = []  # (raw item, id was derived, event)
    for index, item in enumerate(items):
        where = "response" if single else f"item {index}"
        try:
            envelope = _envelope(cfg, item)
            default_id = cfg.name if single else f"{cfg.name}:{_digest(_identity(envelope))}"
            event = parse_envelope(envelope, default_source=cfg.name, default_event_id=default_id)
        except InvalidEvent as exc:
            mapped.skipped.append(f"{where}: {exc}")
            continue
        parsed.append((item, not single and event.event_id == default_id, event))
    # A derived id two items share would merge two conditions into one event (and flip between them on every
    # poll): give each of those the hash of its whole raw item instead, and say how to fix it properly.
    derived = Counter(e.event_id for _, was_derived, e in parsed if was_derived)
    for item, was_derived, event in parsed:
        if was_derived and derived[event.event_id] > 1:
            event.event_id = f"{cfg.name}:{_digest(item)}"
        mapped.events.append(event)
    clashes = sum(n for n in derived.values() if n > 1)
    if clashes:
        mapped.warnings.append(
            f"{clashes} items share a summary and payload, so their ids come from their whole contents; "
            "set id: to what identifies an item (e.g. [labels.alertname, labels.instance])"
        )
    return mapped


async def fetch_json(
    cfg: JsonPollSource, *, environ: MutableMapping[str, str] | None = None, fetcher: Fetcher | None = None
) -> Any:
    """GET the configured URL (SSRF-guarded, bounded) and decode it. :class:`SourceError` on failure."""
    headers = {"Accept": "application/json"}
    if cfg.auth_env:
        secret = resolve_secret(cfg.auth_env, environ)
        if secret is None:
            raise SourceError(f"{cfg.name}: {cfg.auth_env} is not set, so the poll has no credentials")
        headers[cfg.auth_header] = secret.get_secret_value()
    try:
        reply = await (fetcher or afetch)(
            cfg.url,
            headers=headers,
            allow_hosts=[allow_host(cfg.url)],
            timeout=TIMEOUT_S,
            deadline=DEADLINE_S,
            max_bytes=MAX_BYTES,
        )
        reply.raise_for_status()
    except (UrlRefused, FetchFailed) as exc:
        raise SourceError(f"{cfg.name}: {exc}") from exc
    try:
        return json.loads(reply.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"{cfg.name}: the response is not JSON") from exc


async def poll_once(
    cfg: JsonPollSource, *, environ: MutableMapping[str, str] | None = None, fetcher: Fetcher | None = None
) -> PollResult:
    """Fetch once and return the events, ``complete`` unless an item was skipped. Skipped items are logged; a
    failed fetch raises :class:`SourceError`."""
    mapped = map_response(cfg, await fetch_json(cfg, environ=environ, fetcher=fetcher))
    for reason in mapped.skipped:
        log.warning("source.json_poll.skipped", source=cfg.name, reason=reason)
    for warning in mapped.warnings:
        log.warning("source.json_poll.ids", source=cfg.name, warning=warning)
    return PollResult(mapped.events, complete=not mapped.skipped)


class JsonPoll:
    """The ``json_poll`` source plugin (pull only)."""

    kind = "json_poll"
    config_model = JsonPollSource

    async def poll(self, cfg: JsonPollSource) -> PollResult:
        return await poll_once(cfg)


plugin = JsonPoll()

__all__ = ["JsonPoll", "Mapped", "fetch_json", "lookup", "map_response", "plugin", "poll_once"]
