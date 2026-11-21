"""The ``mask`` voice channel: speak through an argus-mask face, lip-synced, on whatever screen is watching it.

::

    - kind: mask
      name: face
      url: http://mask:8000           # the argus-mask server
      face: <FACE_ID>                 # a face id or alias it serves
      wait_ready: 0                   # seconds to wait for a screen that can be heard; 0: skip at once
      token_env: SCOUT_MASK_TOKEN     # optional: a bearer token for a proxy in front of mask
      transport: http                 # http (POST /push) or websocket (the source socket, which can disclose)
      allow_cloned_voice: false       # http only: push a cloned voice with no disclosure (not recommended)

argus-mask's agent contract (its README, "Agent clients: HTTP push and speak") is used as published:

- **Readiness**: ``GET /health``. The face is ready when the driver is available, the server isn't read-only,
  the face has no source already (``live``) and at least one screen watching it reports ``audio_ready``
  (``screens``, keyed by the name the viewer used, so aliases are matched both ways). A screen with house audio
  (``sound: external``, e.g. Snapcast carrying the sound) counts as ready: it moves in sync.
- **Speech**: ``POST /push?face=<id>&require_screen=true`` with raw PCM: ``X-Sample-Rate``, ``X-Audio-Format:
  pcm_s16le`` and ``X-Channels`` headers. The server never sees text. The request lasts as long as the speech
  and answers ``{"session_id", "frames", "duration_ms"}``. ``require_screen`` makes mask itself refuse (409) if
  the screen went away between the check and the push, so speech is never pushed to a face no one can hear.

- **Speech over the source socket** (``transport: websocket``): ``WS /session/<face>/source``. The first message
  is a JSON hello, ``{"sample_rate", "channels", "format": "s16le", "seed", "disclose"}`` (mask's ``SourceHello``);
  mask answers ``{"type": "ready", ...}`` once it has opened its driver and announced the stream. Then binary
  messages of raw PCM, paced at real time plus a one-second lead (as mask's own client does: the socket has no
  server-side pacing), then ``{"type": "end"}``; mask answers ``{"type": "done", "frames", "duration_ms"}`` and
  closes. A refusal is ``{"type": "error", "detail"}`` and a close (1013 for a face that already has a source,
  which is skipped; anything else fails). ``disclose`` is true exactly when the audio is ``SpeechAudio.cloned``:
  viewers then show mask's fixed label, "An AI voice of a real person, used with their consent." The socket has
  no ``require_screen``, so the ``/health`` check just before connecting is the only readiness check.

**Not ready is skipped, not failed**: no screen (or a busy face) returns a ``skipped`` result unless
``wait_ready`` gives it time to become ready. A read-only server or a driver that is down is a failure.

argus-mask has no authentication of its own (reaching its port is access, and its origin guard lets a client
that sends no ``Origin`` through). ``token_env`` is for a reverse proxy that adds one. Mask takes its own GPU
lease (argus-iris) for the stream; scout holds none.

**Cloned voices are disclosed or refused.** Over the socket, speech marked ``SpeechAudio.cloned`` (argus-echo with
a ``voice_clone_id``) goes with ``disclose: true``. ``/push`` carries no disclosure flag, so over http a clone is
skipped, permanently and with a reason, before mask is contacted, unless the channel sets
``allow_cloned_voice: true`` (logged as a warning on every push: the face speaks a possibly real person's voice
with nothing telling the viewer). The hello carries no consent reference: mask checks consent records only for
voices it synthesises itself, and trusts a source's ``disclose``. Scout can't see whether a clone is consented,
so the operator stays responsible for using only a consented clone; the label says it is.

Fetching (``/health``, ``/push``) goes through ``argus_cortex.net.afetch`` with only the configured host allowed and
no redirects. The socket can't use ``afetch``, so it is pinned the same way here: the configured host is resolved
once, the addresses pass ``argus_cortex.net.check_url`` (with only that host allowed), and the connection goes to
those addresses only (``Host`` and TLS SNI stay the configured name), with redirects refused, environment proxies
ignored, no compression, capped messages and timeouts on the connect, the ``ready`` and the whole push.
Needs the ``mask`` extra (numpy, to normalise the audio; websockets, for the socket):
``pip install argus-scout[mask]``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import structlog
from argus_cortex.net import CheckedUrl, Reply, UrlRefused, afetch, check_url
from pydantic import Field

import argus_scout
from argus_scout.channels.audio import MissingExtra, Pcm, decode, require_numpy, to_s16le
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

WS_CONNECT_TIMEOUT_S = 10.0
"""The TCP connect and the WebSocket handshake, together."""
WS_READY_TIMEOUT_S = 30.0
"""From the hello to mask's ``ready`` (it opens its driver first)."""
WS_CLOSE_TIMEOUT_S = 5.0
WS_MAX_MESSAGE_BYTES = 64 * 1024
"""The largest message accepted from mask. It only sends small JSON (``ready``, ``done``, ``error``)."""
WS_FRAME_BYTES = 64 * 1024
"""The most PCM sent in one message (mask refuses one over 4 MB). websockets sends a message as one frame."""
WS_LEAD_S = 1.0
"""How far ahead of real time the socket is fed: mask paces ``/push`` itself, but not its socket."""

Fetcher = Callable[..., Awaitable[Reply]]
Sleep = Callable[[float], Awaitable[object]]
Resolver = Callable[[str, int], list[str]]


class MaskConfig(ChannelSettings):
    """An argus-mask server and the face to speak through."""

    kind: Literal["mask"]
    url: HttpUrl
    """The argus-mask server. Only that host and port are contacted, with no redirects."""
    face: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    """A face id or alias the server serves."""
    wait_ready: float = Field(default=0.0, ge=0, le=300)
    """Seconds to wait for a ready screen before skipping. 0: skip at once."""
    poll_interval: float = Field(default=1.0, gt=0, le=30)
    """Seconds between readiness checks while waiting."""
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    """The idle layer's seed."""
    token_env: EnvName | None = None
    """Env var holding a bearer token, for a proxy in front of mask. Unset: no Authorization header."""
    timeout: float = Field(default=120.0, gt=0, le=600)
    """Seconds the whole delivery may take: mask answers a push when the speech ends."""
    transport: Literal["http", "websocket"] = "http"
    """How speech reaches mask: ``http`` (``POST /push``) or ``websocket`` (its source socket, whose hello sets the
    disclosure label for a cloned voice). The same host and port either way: ``ws://`` or ``wss://`` from ``url``."""
    allow_cloned_voice: bool = False
    """http only: push speech in a cloned voice (``SpeechAudio.cloned``) anyway. Off by default: ``/push`` can't
    set mask's disclosure label, so a face would speak in a possibly real person's voice with no disclosure. Use
    ``transport: websocket`` instead, which discloses it. Ignored over websocket."""


CLONED_REFUSAL = (
    "refused: the speech is in a cloned voice and argus-mask's /push cannot carry a disclosure; "
    "use transport: websocket (which discloses it), or set allow_cloned_voice: true on this channel only if the "
    "clone is consented and disclosed some other way"
)
BUSY_MARK = "already has a source"
"""How mask's socket says the face is taken (close code 1013, which a driver that is down shares)."""


def source_url(url: str, face: str) -> str:
    """The source socket for ``face`` on the mask server at ``url`` (``http`` -> ``ws``, ``https`` -> ``wss``)."""
    scheme, sep, rest = url.rstrip("/").partition("://")
    secure = {"http": False, "https": True}.get(scheme.lower()) if sep else None
    if secure is None:
        raise ChannelError(f"{url!r} is not an http(s):// URL")
    return f"{'wss' if secure else 'ws'}://{rest}/session/{quote(face, safe='')}/source"


def hello(pcm: Pcm, *, seed: int, disclose: bool) -> str:
    """Mask's ``SourceHello``: the audio format, the idle layer's seed, and whether to show the disclosure label."""
    fmt = {"sample_rate": pcm.sample_rate, "channels": pcm.channels, "format": "s16le"}
    return json.dumps(fmt | {"seed": seed, "disclose": disclose})


def require_websockets() -> Any:
    """The ``websockets`` package (the ``mask`` extra), imported only for ``transport: websocket``."""
    try:
        import websockets.asyncio.client
    except ImportError as exc:
        raise MissingExtra(f"transport: websocket needs websockets: install argus-scout[{EXTRA}]") from exc
    return websockets


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
        resolve: Resolver | None = None,
    ) -> None:
        require_numpy(EXTRA)
        if config.transport == "websocket":
            require_websockets()
        self.config = config
        self.environ = environ
        self.fetcher = fetcher
        self.sleep = sleep
        self.resolve = resolve
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

    async def pin(self) -> CheckedUrl:
        """Resolve the configured host once and check its addresses as ``afetch`` would, with only that host
        allowed. The socket then connects to these addresses and nothing else."""
        parts = urlsplit(self.config.url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            if self.resolve is not None:
                addresses = list(self.resolve(host, port))
            else:
                infos = await asyncio.get_running_loop().getaddrinfo(host, port)
                addresses = list(dict.fromkeys(str(i[4][0]) for i in infos))
        except (OSError, UnicodeError) as exc:
            raise ChannelError(f"mask {self.config.name}: cannot resolve {host}") from exc
        try:
            return check_url(self.config.url, allow_hosts=self.allow, resolve=lambda _host, _port: addresses)
        except UrlRefused as exc:
            raise ChannelError(f"mask {self.config.name}: {exc}") from exc

    async def connect(self, headers: dict[str, str]) -> Any:
        """A source socket to the configured host's checked addresses, tried in turn. No redirect is followed and
        no environment proxy used; ``Host`` (and TLS SNI) stay the configured name."""
        websockets = require_websockets()

        class Pinned(websockets.asyncio.client.connect):
            def process_redirect(self, exc: Exception) -> Exception:
                return exc  # a redirect is final: the socket goes nowhere but the configured host

        name = self.config.name
        checked = await self.pin()
        uri = source_url(self.config.url, self.config.face)
        tls = {"server_hostname": checked.host} if checked.scheme == "https" else {}
        extra = {k: v for k, v in headers.items() if k != "User-Agent"}
        last: Exception | None = None
        for ip in checked.addresses:
            try:
                return await Pinned(
                    uri,
                    host=ip,
                    port=checked.port,
                    proxy=None,
                    compression=None,
                    additional_headers=extra,
                    user_agent_header=headers.get("User-Agent"),
                    open_timeout=WS_CONNECT_TIMEOUT_S,
                    close_timeout=WS_CLOSE_TIMEOUT_S,
                    ping_interval=None,
                    max_size=WS_MAX_MESSAGE_BYTES,
                    **tls,
                )
            except TimeoutError as exc:
                raise ChannelError(
                    f"mask {name}: the source socket took over {WS_CONNECT_TIMEOUT_S:g}s to open"
                ) from exc
            except OSError as exc:  # this address is down: try the next
                last = exc
            except websockets.InvalidStatus as exc:
                status = exc.response.status_code
                raise ChannelError(
                    f"mask {name}: the source socket answered {status} (no redirect is followed)"
                ) from exc
            except (websockets.InvalidHandshake, websockets.InvalidURI) as exc:
                raise ChannelError(f"mask {name}: the source socket handshake failed: {exc}") from exc
        raise ChannelError(f"mask {name}: could not connect to {checked.host}") from last

    async def stream(
        self, pcm: Pcm, body: bytes, headers: dict[str, str], *, disclose: bool, why: str
    ) -> DeliveryResult:
        """Speak ``body`` (``pcm`` as s16le) over the source socket, with mask's disclosure label when ``disclose``."""
        cfg = self.config
        websockets = require_websockets()
        frame = 2 * pcm.channels
        step = WS_FRAME_BYTES - WS_FRAME_BYTES % frame
        per_second = frame * pcm.sample_rate
        stage = "ready"
        ws = await self.connect(headers)
        try:
            async with ws:
                # Mask refuses (a face already taken, a driver down) before it reads the hello, so sending it
                # can fail: the refusal is still read below.
                with contextlib.suppress(websockets.ConnectionClosed):
                    await ws.send(hello(pcm, seed=cfg.seed, disclose=disclose))
                ready = await asyncio.wait_for(self._reply(ws, "ready"), WS_READY_TIMEOUT_S)
                if ready is None:
                    log.info("channel.mask.not_ready", channel=cfg.name, face=cfg.face, why=BUSY_MARK)
                    return DeliveryResult(ok=True, status="skipped", detail=f"not ready: face {cfg.face!r} {BUSY_MARK}")
                stage = "push"
                async with asyncio.timeout(min(cfg.timeout, pcm.duration + PUSH_SLACK_S)):
                    started = time.monotonic()
                    for at in range(0, len(body), step):
                        chunk = body[at : at + step]
                        await ws.send(chunk)
                        ahead = (at + len(chunk)) / per_second - (time.monotonic() - started) - WS_LEAD_S
                        if ahead > 0:
                            await self.sleep(ahead)
                    await ws.send(json.dumps({"type": "end"}))
                    done = await self._reply(ws, "done")
        except TimeoutError as exc:
            took = f"no ready in {WS_READY_TIMEOUT_S:g}s" if stage == "ready" else "the push outlasted the speech"
            raise ChannelError(f"mask {cfg.name}: the source socket timed out ({took})") from exc
        except websockets.ConnectionClosed as exc:
            raise ChannelError(f"mask {cfg.name}: mask closed the source socket: {exc.reason or exc.code}") from exc
        if done is None:
            raise ChannelError(f"mask {cfg.name}: the face was taken mid-speech")
        if disclose:
            log.info("channel.mask.cloned_voice_disclosed", channel=cfg.name, face=cfg.face)
        try:
            spoken = f"{int(done.get('duration_ms', 0)) / 1000:.1f}s"
        except (TypeError, ValueError):
            spoken = f"{pcm.duration:.1f}s"
        label = ", disclosed as a cloned voice" if disclose else ""
        return DeliveryResult(ok=True, detail=f"face {cfg.face} spoke {spoken} ({why}{label})")

    async def _reply(self, ws: Any, expect: str) -> dict[str, Any] | None:
        """The next ``expect`` message from mask. ``None`` if it says the face already has a source;
        :class:`ChannelError` for any other refusal, or a socket closed before ``expect``."""
        websockets = require_websockets()
        while True:
            try:
                raw = await ws.recv()
            except websockets.ConnectionClosed as exc:
                why = exc.reason or exc.code
                if BUSY_MARK in str(exc.reason):  # the close's reason repeats the refusal
                    return None
                raise ChannelError(
                    f"mask {self.config.name}: the source socket closed before {expect!r}: {why}"
                ) from exc
            try:
                message = json.loads(raw) if isinstance(raw, str) else None
            except ValueError:
                message = None
            if not isinstance(message, dict):
                continue
            if message.get("type") == "error":
                detail = str(message.get("detail") or "refused")[:200]
                if BUSY_MARK in detail:
                    return None
                raise ChannelError(f"mask {self.config.name}: the source socket refused: {detail}")
            if message.get("type") == expect:
                return message

    async def play(self, audio: SpeechAudio, message: Delivery) -> DeliveryResult:
        cfg = self.config
        if audio.cloned and cfg.transport == "http":
            if not cfg.allow_cloned_voice:
                log.warning("channel.mask.cloned_voice_refused", channel=cfg.name, face=cfg.face, voice=audio.voice)
                return DeliveryResult(ok=True, status="skipped", permanent=True, detail=CLONED_REFUSAL)
            log.warning(
                "channel.mask.cloned_voice_undisclosed",
                channel=cfg.name,
                face=cfg.face,
                voice=audio.voice,
                why="allow_cloned_voice over http: the face speaks a cloned voice with no disclosure label",
            )
        pcm = decode(audio)
        body = to_s16le(pcm)
        if len(body) > MAX_PUSH_BYTES:
            raise ChannelError(f"mask {cfg.name}: {pcm.duration:.0f}s of speech is over argus-mask's 25 MB push limit")
        headers = self._headers()
        ready, why = await self.wait_until_ready(headers)
        if not ready:
            log.info("channel.mask.not_ready", channel=cfg.name, face=cfg.face, why=why)
            return DeliveryResult(ok=True, status="skipped", detail=f"not ready: {why}")
        if cfg.transport == "websocket":
            return await self.stream(pcm, body, headers, disclose=audio.cloned, why=why)
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


__all__ = ["CLONED_REFUSAL", "MaskChannel", "MaskConfig", "face_keys", "hello", "readiness", "source_url"]
