"""The ``webhook`` source: producers POST status events to scout.

This module is the pure part: :func:`check_token` decides whether a request may push as a source,
and :func:`parse_webhook` turns a body into events. It mounts no route. The HTTP route (behind
argus-cortex's ``auth.bearer`` and ``WriteGuard``) calls these and ingests the result.

A body is one envelope or a list of them (see :func:`argus_scout.inbox.parse_envelope`). A body is
taken whole or not at all: one malformed envelope refuses the lot, with an error naming its index,
so a producer never has half a batch silently dropped.

An envelope's ``source`` defaults to the webhook source's ``name``. Naming a *different* source is
refused: a token authorises one source, so one producer can't speak (or ack-reset) for another.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from typing import Any

from argus_scout.config import WebhookSource, resolve_secret
from argus_scout.events import StatusEvent
from argus_scout.inbox import InvalidEvent, parse_envelope
from argus_scout.sources.base import SourceAuthError

MAX_EVENTS = 100
"""Most envelopes one body may carry. Byte limits are the route's (``argus_cortex.limits``)."""


def check_token(
    cfg: WebhookSource,
    authorization: str | None,
    *,
    fallback_env: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Raise :class:`SourceAuthError` unless ``authorization`` is ``Bearer <token>`` for this source.

    The token is read (on every call, so rotation needs no restart) from the source's ``token_env``,
    else ``fallback_env`` (the server-wide token). **No token configured means refused** (503), never
    open. The compare is argus-cortex's ``auth.check_bearer``: constant-time in the token, scheme
    case-insensitive. That module imports Starlette, which the HTTP route calling this already has.
    """
    from argus_cortex.auth import check_bearer

    env = cfg.token_env or fallback_env
    secret = resolve_secret(env, environ)
    if secret is None:
        raise SourceAuthError(
            f"source {cfg.name!r} has no token configured ({env or 'no token_env'}), so it is closed", 503
        )
    if not check_bearer(authorization, secret.get_secret_value()):
        raise SourceAuthError("bearer token required", 401)


def _decode(body: bytes | str | Mapping[str, Any] | list[Any]) -> Any:
    if not isinstance(body, bytes | str):
        return body
    try:
        return json.loads(body)
    except UnicodeDecodeError as exc:
        raise InvalidEvent("body is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise InvalidEvent(f"body is not JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc


def parse_webhook(cfg: WebhookSource, body: bytes | str | Mapping[str, Any] | list[Any]) -> list[StatusEvent]:
    """The events in a webhook body (raw JSON bytes, or already decoded), or :class:`InvalidEvent`."""
    data = _decode(body)
    single = isinstance(data, Mapping)
    if single:
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        raise InvalidEvent(f"body must be an event object or a list of them, got {type(data).__name__}")
    if len(items) > MAX_EVENTS:
        raise InvalidEvent(f"body has {len(items)} events; at most {MAX_EVENTS} per request")
    events = []
    for index, item in enumerate(items):
        where = "" if single else f"event {index}: "
        try:
            event = parse_envelope(item, default_source=cfg.name)
        except InvalidEvent as exc:
            raise InvalidEvent(f"{where}{exc}") from exc
        if event.source != cfg.name:
            raise InvalidEvent(f"{where}source: {event.source!r} does not match webhook source {cfg.name!r}")
        events.append(event)
    return events


class Webhook:
    """The ``webhook`` source plugin (push only)."""

    kind = "webhook"
    config_model = WebhookSource

    def parse(self, cfg: WebhookSource, body: bytes | str | Mapping[str, Any] | list[Any]) -> list[StatusEvent]:
        return parse_webhook(cfg, body)

    def authorize(
        self,
        cfg: WebhookSource,
        authorization: str | None,
        *,
        fallback_env: str | None = None,
        environ: MutableMapping[str, str] | None = None,
    ) -> None:
        check_token(cfg, authorization, fallback_env=fallback_env, environ=environ)


plugin = Webhook()

__all__ = ["MAX_EVENTS", "Webhook", "check_token", "parse_webhook", "plugin"]
