"""The ``mask`` voice channel: speak through an argus-mask face, lip-synced, on whatever screen is watching it.

::

    - kind: mask
      name: face
      url: http://mask:8000           # the argus-mask server
      face: <FACE_ID>                 # a face id or alias it serves
      wait_ready: 0                   # seconds to wait for a screen that can be heard; 0: skip at once
      token_env: SCOUT_MASK_TOKEN     # optional: a bearer token for a proxy in front of mask
      allow_cloned_voice: false       # refuse speech in a cloned voice: /push cannot disclose it

argus-mask's agent contract (its README, "Agent clients: HTTP push and speak") is used as published:

- **Readiness**: ``GET /health``. The face is ready when the driver is available, the server isn't read-only,
  the face has no source already (``live``) and at least one screen watching it reports ``audio_ready``
  (``screens``, keyed by the name the viewer used, so aliases are matched both ways). A screen with house audio
  (``sound: external``, e.g. Snapcast carrying the sound) counts as ready: it moves in sync.
- **Speech**: ``POST /push?face=<id>&require_screen=true`` with raw PCM: ``X-Sample-Rate``, ``X-Audio-Format:
  pcm_s16le`` and ``X-Channels`` headers. The server never sees text. The request lasts as long as the speech
  and answers ``{"session_id", "frames", "duration_ms"}``. ``require_screen`` makes mask itself refuse (409) if
  the screen went away between the check and the push, so speech is never pushed to a face no one can hear.

**Not ready is skipped, not failed**: no screen (or a busy face) returns a ``skipped`` result unless
``wait_ready`` gives it time to become ready. A read-only server or a driver that is down is a failure.

argus-mask has no authentication of its own (reaching its port is access, and its origin guard lets a client
that sends no ``Origin`` through). ``token_env`` is for a reverse proxy that adds one. Mask takes its own GPU
lease (argus-iris) for the stream; scout holds none.

**Cloned voices are refused by default.** ``/push`` carries no consent or disclosure flag, so a face fed a
clone would speak in a possibly real person's voice with nothing telling the viewer. Speech marked
``SpeechAudio.cloned`` (argus-echo with a ``voice_clone_id``) is skipped, permanently and with a reason, unless
the channel sets ``allow_cloned_voice: true``. Nothing is fetched or pushed for it.

Fetching goes through ``argus_cortex.net.afetch`` with only the configured host allowed and no redirects.
Needs the ``mask`` extra (numpy, to normalise the audio): ``pip install argus-scout[mask]``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Literal
from urllib.parse import quote

import structlog
from argus_cortex.net import Reply, UrlRefused, afetch
from pydantic import Field

import argus_scout
from argus_scout.channels.audio import decode, require_numpy, to_s16le
from argus_scout.channels.base import ChannelError, Delivery, DeliveryResult, SpeechAudio
from argus_scout.config import ChannelSettings, EnvName, HttpUrl, resolve_secret
from argus_scout.sources.json_poll import allow_host

log = structlog.get_logger(__name__)

EXTRA = "mask"
MAX_PUSH_BYTES = 25 * 1024 * 1024
"""argus-mask refuses a body larger than this (413)."""
HEALTH_TIMEOUT_S = 5.0
MAX_HEALTH_BYTES = 256 * 1024
PUSH_SLACK_S = 15.0
"""How much longer than the speech a push may take to answer (mask paces it at real time, plus its driver)."""

Fetcher = Callable[..., Awaitable[Reply]]
Sleep = Callable[[float], Awaitable[object]]


class MaskConfig(ChannelSettings):
    """An argus-mask server and the face to speak through."""

    kind: Literal["mask"]
    url: HttpUrl
    face: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    """A face id or alias the server serves."""
    wait_ready: float = Field(default=0.0, ge=0, le=300)
    """Seconds to wait for a ready screen before skipping. 0: skip at once."""
    poll_interval: float = Field(default=1.0, gt=0, le=30)
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    """The idle layer's seed."""
    token_env: EnvName | None = None
    """Env var holding a bearer token, for a proxy in front of mask. Unset: no Authorization header."""
    timeout: float = Field(default=120.0, gt=0, le=600)
    allow_cloned_voice: bool = False
    """Push speech in a cloned voice (``SpeechAudio.cloned``) to this face. Off by default: ``/push`` can't set
    mask's disclosure label, so a face would speak in a possibly real person's voice with no disclosure."""


CLONED_REFUSAL = (
    "refused: the speech is in a cloned voice and argus-mask's /push cannot carry a disclosure; "
    "set allow_cloned_voice: true on this channel only if the clone is consented and disclosed some other way"
)


def face_keys(face: str, aliases: dict[str, str]) -> set[str]:
    """Every name ``face`` goes by: itself, what it is an alias for, and the aliases that point at it."""
    keys = {face, aliases.get(face, "")} | {alias for alias, target in aliases.items() if target == face}
    keys.discard("")
    return keys


def readiness(health: dict[str, Any], face: str) -> tuple[bool, str]:
    """``(ready, why)`` for ``face`` from a ``/health`` body. Raises :class:`ChannelError` for a server that
    can't take speech at all (read-only, driver down)."""
    if health.get("read_only"):
        raise ChannelError("argus-mask is read-only: it takes no speech")
    driver = health.get("driver") or {}
    if not driver.get("available", False):
        raise ChannelError(f"argus-mask's driver is down: {driver.get('reason') or 'unavailable'}")
    aliases = {str(k): str(v) for k, v in (health.get("aliases") or {}).items()}
    keys = face_keys(face, aliases)
    if keys & {str(f) for f in health.get("live") or []}:
        return False, f"face {face!r} is already speaking"
    screens_by_face = health.get("screens") or {}
    screens = [s for key in keys for s in (screens_by_face.get(key) or []) if isinstance(s, dict)]
    if not screens:
        return False, f"no screen is watching face {face!r}"
    if not any(s.get("audio_ready") for s in screens):
        return False, f"no screen watching face {face!r} can be heard yet (it needs a tap, or a kiosk flag)"
    return True, f"{sum(bool(s.get('audio_ready')) for s in screens)} screen(s) ready"


def _detail(reply: Reply) -> str:
    try:
        body = json.loads(reply.body)
    except ValueError:
        return reply.body[:200].decode("utf-8", "replace")
    return str(body.get("detail", body)) if isinstance(body, dict) else str(body)


class MaskChannel:
    """The ``mask`` voice channel kind."""

    kind = "mask"
    config_model = MaskConfig

    def __init__(
        self,
        config: MaskConfig,
        *,
        environ: MutableMapping[str, str] | None = None,
        fetcher: Fetcher | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        require_numpy(EXTRA)
        self.config = config
        self.environ = environ
        self.fetcher = fetcher
        self.sleep = sleep
        self.allow = [allow_host(config.url)]

    async def deliver(self, message: Delivery) -> DeliveryResult:
        return DeliveryResult(ok=False, error="mask is a voice channel: it plays speech, not text")

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": f"argus-scout/{argus_scout.__version__}"}
        if self.config.token_env:
            token = resolve_secret(self.config.token_env, self.environ)
            if token is None:
                raise ChannelError(f"mask {self.config.name}: {self.config.token_env} is not set, so nothing was sent")
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        return headers

    async def _fetch(self, url: str, **kw: Any) -> Reply:
        try:
            return await (self.fetcher or afetch)(url, allow_hosts=self.allow, max_redirects=0, **kw)
        except UrlRefused as exc:
            raise ChannelError(f"mask {self.config.name}: {exc}") from exc

    async def health(self, headers: dict[str, str]) -> dict[str, Any]:
        reply = await self._fetch(
            f"{self.config.url}/health",
            headers=headers,
            timeout=HEALTH_TIMEOUT_S,
            deadline=HEALTH_TIMEOUT_S,
            max_bytes=MAX_HEALTH_BYTES,
        )
        if not reply.ok:
            raise ChannelError(f"mask {self.config.name}: /health answered {reply.status}")
        try:
            body = json.loads(reply.body)
        except ValueError as exc:
            raise ChannelError(f"mask {self.config.name}: /health is not JSON") from exc
        if not isinstance(body, dict):
            raise ChannelError(f"mask {self.config.name}: /health is not a JSON object")
        return body

    async def wait_until_ready(self, headers: dict[str, str]) -> tuple[bool, str]:
        """Poll ``/health`` until the face is ready or ``wait_ready`` runs out."""
        deadline = time.monotonic() + self.config.wait_ready
        while True:
            ready, why = readiness(await self.health(headers), self.config.face)
            if ready or time.monotonic() + self.config.poll_interval > deadline:
                return ready, why
            await self.sleep(self.config.poll_interval)

    async def play(self, audio: SpeechAudio, message: Delivery) -> DeliveryResult:
        cfg = self.config
        if audio.cloned and not cfg.allow_cloned_voice:
            log.warning("channel.mask.cloned_voice_refused", channel=cfg.name, face=cfg.face, voice=audio.voice)
            return DeliveryResult(ok=True, status="skipped", permanent=True, detail=CLONED_REFUSAL)
        pcm = decode(audio)
        body = to_s16le(pcm)
        if len(body) > MAX_PUSH_BYTES:
            raise ChannelError(f"mask {cfg.name}: {pcm.duration:.0f}s of speech is over argus-mask's 25 MB push limit")
        headers = self._headers()
        ready, why = await self.wait_until_ready(headers)
        if not ready:
            log.info("channel.mask.not_ready", channel=cfg.name, face=cfg.face, why=why)
            return DeliveryResult(ok=True, status="skipped", detail=f"not ready: {why}")
        push = f"{cfg.url}/push?face={quote(cfg.face)}&seed={cfg.seed}&require_screen=true"
        headers |= {
            "Content-Type": "application/octet-stream",
            "X-Sample-Rate": str(pcm.sample_rate),
            "X-Audio-Format": "pcm_s16le",
            "X-Channels": str(pcm.channels),
        }
        wait = min(cfg.timeout, pcm.duration + PUSH_SLACK_S)
        reply = await self._fetch(
            push, method="POST", content=body, headers=headers, timeout=wait, deadline=wait, max_bytes=MAX_HEALTH_BYTES
        )
        if reply.status == 409:  # the screen went away or another source took the face since the check
            return DeliveryResult(ok=True, status="skipped", detail=f"not ready: {_detail(reply)}")
        if not reply.ok:
            raise ChannelError(f"mask {cfg.name}: /push answered {reply.status}: {_detail(reply)}")
        try:
            done = json.loads(reply.body)
            spoken = f"{int(done.get('duration_ms', 0)) / 1000:.1f}s"
        except (ValueError, TypeError, AttributeError):
            spoken = f"{pcm.duration:.1f}s"
        return DeliveryResult(ok=True, detail=f"face {cfg.face} spoke {spoken} ({why})")


__all__ = ["CLONED_REFUSAL", "MaskChannel", "MaskConfig", "face_keys", "readiness"]
