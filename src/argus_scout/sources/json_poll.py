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

**Stable ids.** A poller sees the same state on every poll, so an item without a mapped ``event_id``
gets one derived from what it says, never a random one: with no ``items`` the response *is* the event
(id ``<name>``); in a list, the id is ``<name>:<hash of the summary>``. Re-polling an unchanged item is
then a refresh, and a severity change under the same summary an escalation.

**Fetching.** Through ``argus_cortex.net.afetch``: resolved addresses are checked and pinned, the body
is capped and the whole fetch has a deadline. The configured host (and port) goes in ``allow_hosts``,
because a status endpoint on the LAN is normal for a homelab; a redirect to any *other* host still
has to pass the public-address rule, and drops the auth header when it changes origin.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import structlog
from argus_cortex.net import FetchFailed, Reply, UrlRefused, afetch

from argus_scout.config import EVENT_FIELDS, JsonPollSource, resolve_secret
from argus_scout.events import StatusEvent
from argus_scout.inbox import InvalidEvent, parse_envelope
from argus_scout.sources.base import SourceError

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


def allow_host(url: str) -> str:
    """The ``host:port`` entry for ``allow_hosts`` that lets a fetch reach the configured URL itself."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _stable_id(cfg: JsonPollSource, summary: str, single: bool) -> str:
    if single:
        return cfg.name
    digest = hashlib.sha256(" ".join(summary.lower().split()).encode()).hexdigest()[:16]
    return f"{cfg.name}:{digest}"


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
    for index, item in enumerate(items):
        where = "response" if single else f"item {index}"
        try:
            envelope = _envelope(cfg, item)
            summary = envelope.get("summary")
            default_id = _stable_id(cfg, str(summary), single) if summary not in (None, "") else None
            event = parse_envelope(envelope, default_source=cfg.name, default_event_id=default_id)
        except InvalidEvent as exc:
            mapped.skipped.append(f"{where}: {exc}")
            continue
        mapped.events.append(event)
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
) -> list[StatusEvent]:
    """Fetch once and return the events. Skipped items are logged; a failed fetch raises :class:`SourceError`."""
    mapped = map_response(cfg, await fetch_json(cfg, environ=environ, fetcher=fetcher))
    for reason in mapped.skipped:
        log.warning("source.json_poll.skipped", source=cfg.name, reason=reason)
    return mapped.events


class JsonPoll:
    """The ``json_poll`` source plugin (pull only)."""

    kind = "json_poll"
    config_model = JsonPollSource

    async def poll(self, cfg: JsonPollSource) -> list[StatusEvent]:
        return await poll_once(cfg)


plugin = JsonPoll()

__all__ = ["JsonPoll", "Mapped", "allow_host", "fetch_json", "lookup", "map_response", "plugin", "poll_once"]
