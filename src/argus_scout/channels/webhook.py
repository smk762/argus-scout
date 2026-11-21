"""The ``webhook`` channel: POST each message as JSON to one configured URL.

::

    - kind: webhook
      name: chat
      url: https://hooks.example/scout
      token_env: SCOUT_CHAT_TOKEN       # optional: sent as "Authorization: Bearer <token>"
      secret_env: SCOUT_CHAT_SECRET     # optional: HMAC-SHA256 key for X-Scout-Signature
      retries: 2                        # after a connection error, 429 or 5xx; a 4xx is final
      backoff: 1.0                      # seconds before the first retry, doubling
      min_severity: notice
      kinds: [morning, evening, event]

The body is :func:`build_payload` (schema ``argus-scout.delivery``, documented in ``docs/channels.md``).
Headers: ``Content-Type: application/json``, ``X-Scout-Delivery`` (the message key: the same message always
carries the same key, so a receiver can drop repeats), ``X-Scout-Timestamp`` (Unix seconds) and, with a
secret, ``X-Scout-Signature: sha256=<hex>``: HMAC-SHA256 over ``<timestamp>.<body>``.

**Fetching** goes through ``argus_cortex.net.afetch`` with only the configured host (and port) in
``allow_hosts``, the same as the ``json_poll`` source: a LAN receiver is normal for a homelab, any other
host still has to pass the public-address rule, and a POST is never redirected. The response body is
capped and ignored. A channel only sends: nothing a receiver answers is acted on.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime
from typing import Any

import structlog
from argus_cortex.net import Reply, UrlRefused, afetch

import argus_scout
from argus_scout.channels.base import ChannelError, Delivery, DeliveryResult
from argus_scout.config import WebhookChannel as WebhookChannelConfig
from argus_scout.config import resolve_secret
from argus_scout.sources.json_poll import allow_host

log = structlog.get_logger(__name__)

PAYLOAD_SCHEMA = "argus-scout.delivery"
PAYLOAD_VERSION = 1
MAX_REPLY_BYTES = 64 * 1024
ATTEMPT_TIMEOUT_S = 10.0
MAX_BACKOFF_S = 60.0

Fetcher = Callable[..., Awaitable[Reply]]
Sleep = Callable[[float], Awaitable[object]]


def build_payload(message: Delivery, *, channel: str, sent_at: datetime) -> dict[str, Any]:
    """The JSON body for ``message`` (schema ``argus-scout.delivery``, version :data:`PAYLOAD_VERSION`)."""
    event = message.event
    return {
        "schema": PAYLOAD_SCHEMA,
        "schema_version": PAYLOAD_VERSION,
        "delivery_id": message.key,
        "channel": channel,
        "type": message.type,
        "kind": message.kind,
        "severity": message.severity.value,
        "title": message.title,
        "text": message.text,
        "speakable": message.speakable,
        "generated_at": message.generated_at.astimezone(UTC).isoformat(),
        "sent_at": sent_at.astimezone(UTC).isoformat(),
        "report_id": message.report_id,
        "event": None
        if event is None
        else {
            "source": event.source,
            "event_id": event.event_id,
            "requires_ack": event.requires_ack,
            "link": event.link,
        },
        "meta": message.meta,
    }


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """The ``X-Scout-Signature`` value: ``sha256=`` + hex HMAC-SHA256 of ``<timestamp>.<body>``."""
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


async def post(
    cfg: WebhookChannelConfig,
    message: Delivery,
    *,
    environ: MutableMapping[str, str] | None = None,
    fetcher: Fetcher | None = None,
    sleep: Sleep = asyncio.sleep,
    now: datetime | None = None,
) -> DeliveryResult:
    """POST ``message`` with bounded retries. Raises :class:`ChannelError` once every attempt has failed."""
    sent_at = now or datetime.now(UTC)
    body = json.dumps(build_payload(message, channel=cfg.name, sent_at=sent_at), default=str).encode()
    timestamp = str(int(sent_at.timestamp()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"argus-scout/{argus_scout.__version__}",
        "X-Scout-Delivery": message.key,
        "X-Scout-Timestamp": timestamp,
    }
    if cfg.token_env:
        token = resolve_secret(cfg.token_env, environ)
        if token is None:
            raise ChannelError(f"webhook {cfg.name}: {cfg.token_env} is not set, so nothing was sent")
        headers["Authorization"] = f"Bearer {token.get_secret_value()}"
    if cfg.secret_env:
        secret = resolve_secret(cfg.secret_env, environ)
        if secret is None:
            raise ChannelError(f"webhook {cfg.name}: {cfg.secret_env} is not set, so nothing was sent")
        headers["X-Scout-Signature"] = sign(secret.get_secret_value(), timestamp, body)
    attempts = cfg.retries + 1
    error = ""
    for attempt in range(attempts):
        try:
            reply = await (fetcher or afetch)(
                cfg.url,
                method="POST",
                content=body,
                headers=headers,
                allow_hosts=[allow_host(cfg.url)],
                timeout=min(ATTEMPT_TIMEOUT_S, cfg.timeout),
                deadline=cfg.timeout,
                max_bytes=MAX_REPLY_BYTES,
                max_redirects=0,
            )
        except UrlRefused as exc:
            error, retry = str(exc), True
        else:
            if reply.ok:
                tries = f" after {attempt + 1} attempts" if attempt else ""
                return DeliveryResult(ok=True, detail=f"POST answered {reply.status}{tries}")
            error, retry = f"POST answered {reply.status}", _retryable(reply.status)
        if not retry or attempt + 1 == attempts:
            break
        delay = min(MAX_BACKOFF_S, cfg.backoff * 2**attempt)
        log.info("channel.webhook.retry", channel=cfg.name, attempt=attempt + 1, error=error, delay=delay)
        await sleep(delay)
    tries = f" ({attempt + 1} attempt{'s' * (attempt > 0)})"
    raise ChannelError(f"webhook {cfg.name}: {error}{tries}")


class WebhookChannel:
    """The ``webhook`` channel kind."""

    kind = "webhook"
    config_model = WebhookChannelConfig

    def __init__(
        self,
        config: WebhookChannelConfig,
        *,
        environ: MutableMapping[str, str] | None = None,
        fetcher: Fetcher | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.config = config
        self.environ = environ
        self.fetcher = fetcher
        self.sleep = sleep

    async def deliver(self, message: Delivery) -> DeliveryResult:
        return await post(self.config, message, environ=self.environ, fetcher=self.fetcher, sleep=self.sleep)


__all__ = ["PAYLOAD_SCHEMA", "PAYLOAD_VERSION", "WebhookChannel", "build_payload", "post", "sign"]
