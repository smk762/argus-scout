"""Speech from argus-echo: the :class:`~argus_scout.channels.base.Synthesiser` voice channels play from.

::

    services:
      echo:
        url: http://echo:8000          # an engine's root (Miso, XTTS-v2 or Qwen3-TTS: the same contract)
        voice_clone_id: <CLONE_ID>     # or speaker: <SPEAKER>; neither means the engine's default
        language: en
        format: pcm_s16le              # what voice channels get: pcm_s16le, or wav

**The contract.** argus-echo's engines share one HTTP contract (``argus_echo.contract`` and echo's
``docs/api.md``), which this module follows exactly. It is re-implemented here rather than imported: it is a
handful of field and header names, argus-echo isn't published to PyPI, and echo's own README says clients share
the contract, not code. The parts scout uses:

- ``POST /synthesise/stream`` with a ``SynthesiseRequest`` body (``text``, ``voice_clone_id``, ``speaker``,
  ``language``, the Qwen-only ``voice_description``, ``instruction``, ``emotion``, and ``speed``, reserved and
  applied by no engine yet; an engine ignores the
  fields it has no use for, and engine-specific extras pass through). It answers ``200`` with raw PCM whose
  layout is in the ``X-Sample-Rate``, ``X-Audio-Format`` (``pcm_f32le`` or ``pcm_s16le``) and ``X-Channels``
  headers, which are read rather than assumed. Scout streams instead of calling ``/synthesise`` because that
  one answers with an ``audio_url`` in the engine's storage, which the caller may not be able to reach.
  ``503`` (busy, loading, out of memory) is temporary; another 4xx refuses this request for good.
- ``GET /health``, the **resident contract**: argus-cortex's ``ResidentHealth`` (``model_state`` is
  ``unloaded``, ``loading``, ``cpu``, ``gpu`` or ``error``), or ``503 {"status": "loading"}`` while a model
  loads. Scout asks first, so an engine that is down fails in seconds, not after the synthesis timeout, and
  a cold one (``unloaded``/``loading``) gets ``load_timeout`` instead of ``timeout``: an engine loads its model
  on first use, which can take minutes. ``error`` fails at once. ``/admin/*`` is never called: waking and
  evicting models is argus-iris's job, not scout's.

**No GPU lease.** Scout never holds an ``argus_cortex.gpu`` lease around a synthesis: echo takes its own for
the model, and a lease held here as well would block it (see :mod:`argus_scout.llm`).

**Failures** raise :class:`~argus_scout.channels.base.SynthesisError`, which the dispatcher records against
every voice channel in play while text goes out regardless; ``permanent`` is set only for a refusal that
retrying can't fix.

**Cloned voices.** Echo's contract carries no clone flag (not in ``/health``, the stream headers or the
``/synthesise`` reply), so scout decides from its own request: speech made with a ``voice_clone_id`` is
``SpeechAudio(cloned=True)``. A built-in ``speaker`` or a designed ``voice_description`` is synthetic. Channels
that can't disclose a clone to their audience (``mask``) refuse cloned audio unless configured to allow it.

Speech comes back as 16-bit PCM (converted from float32 when the engine sends that), raw
or in a WAV container, and is capped at ``max_seconds``. Echo has no speaker of its own, so there is no
``echo`` voice channel: Snapcast and argus-mask channels play what this returns.
"""

from __future__ import annotations

import array
import asyncio
import io
import json
import math
import sys
import wave
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from argus_cortex.gpu_wire import ResidentHealth
from argus_cortex.net import Reply, UrlRefused, afetch
from pydantic import ValidationError

import argus_scout
from argus_scout.channels.base import Delivery, SpeechAudio, SynthesisError
from argus_scout.config import EchoConfig
from argus_scout.sources.json_poll import allow_host

log = structlog.get_logger(__name__)

# --- argus-echo's contract (argus_echo.contract) ----------------------------------------------------------------
STREAM_PATH = "/synthesise/stream"
HEALTH_PATH = "/health"
SAMPLE_RATE_HEADER = "X-Sample-Rate"
AUDIO_FORMAT_HEADER = "X-Audio-Format"
CHANNELS_HEADER = "X-Channels"
SAMPLE_WIDTH = {"pcm_f32le": 4, "pcm_s16le": 2}
"""Bytes per sample for each ``X-Audio-Format`` an engine may send."""
STREAM_RATE = 24_000
STREAM_WIDTH = 4
"""Every engine streams 24 kHz mono float32 today; the response cap is sized from it."""

MAX_HEALTH_BYTES = 64 * 1024
WARM_STATES = frozenset({"cpu", "gpu"})
COLD_STATES = frozenset({"unloaded", "loading"})

ECHO_VOICE_KEYS = ("voice_clone_id", "speaker", "voice_description", "instruction", "emotion", "speed")
"""The optional shared request fields, sent when configured. With ``text`` and ``language`` they are every field of
:data:`~argus_scout.config.ECHO_REQUEST_FIELDS`: a test fails if one is reserved there but has no key here."""

Fetcher = Callable[..., Awaitable[Reply]]
Readiness = Literal["warm", "cold"]


@dataclass(frozen=True)
class StreamFormat:
    """The PCM layout of a ``/synthesise/stream`` reply, from its headers."""

    sample_rate: int
    audio_format: str
    channels: int

    @property
    def sample_width(self) -> int:
        return SAMPLE_WIDTH[self.audio_format]

    @property
    def frame_bytes(self) -> int:
        return self.sample_width * self.channels

    def seconds(self, n_bytes: int) -> float:
        return n_bytes / (self.frame_bytes * self.sample_rate)

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> StreamFormat:
        """Parse the stream headers. Raises ``ValueError`` if one is missing, malformed or unknown."""
        lower = {k.lower(): v for k, v in headers.items()}
        try:
            fmt = cls(
                sample_rate=int(lower[SAMPLE_RATE_HEADER.lower()]),
                audio_format=lower[AUDIO_FORMAT_HEADER.lower()].strip(),
                channels=int(lower[CHANNELS_HEADER.lower()]),
            )
        except KeyError as exc:
            raise ValueError(f"missing stream header {exc.args[0]!r}") from None
        if fmt.sample_rate <= 0 or fmt.channels <= 0:
            raise ValueError(f"bad stream format: {fmt.sample_rate} Hz x{fmt.channels}")
        if fmt.audio_format not in SAMPLE_WIDTH:
            raise ValueError(f"unknown audio format {fmt.audio_format!r}")
        return fmt


def build_request(cfg: EchoConfig, text: str) -> dict[str, Any]:
    """The ``SynthesiseRequest`` body for ``text``: the configured voice fields that are set, plus ``extra``."""
    body: dict[str, Any] = {**cfg.extra, "text": text, "language": cfg.language}
    for key in ECHO_VOICE_KEYS:
        value = getattr(cfg, key)
        if value is not None:
            body[key] = value
    return body


# --- audio --------------------------------------------------------------------------------------------------


def _clip(sample: float) -> int:
    if math.isnan(sample):
        return 0
    # Clamp before scaling: an infinite (or huge) sample saturates instead of overflowing round().
    return max(-32768, min(32767, round(max(-1.0, min(1.0, sample)) * 32767.0)))


def to_s16le(data: bytes, audio_format: str) -> bytes:
    """Little-endian float32 or int16 PCM as little-endian int16 (float samples clipped to [-1, 1])."""
    if audio_format == "pcm_s16le":
        return data
    floats = array.array("f")
    floats.frombytes(data)
    if sys.byteorder == "big":
        floats.byteswap()
    ints = array.array("h", map(_clip, floats))
    if sys.byteorder == "big":
        ints.byteswap()
    return ints.tobytes()


def wav_bytes(pcm_s16le: bytes, sample_rate: int, channels: int) -> bytes:
    """16-bit PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm_s16le)
    return buf.getvalue()


def _error_detail(reply: Reply) -> str:
    try:
        parsed = json.loads(reply.body[:2000] or b"null")
    except ValueError:
        return reply.body[:200].decode("utf-8", "replace").strip()
    if isinstance(parsed, dict):
        return str(parsed.get("detail") or parsed.get("error") or parsed.get("status") or "")[:200]
    return ""


# --- the synthesiser ----------------------------------------------------------------------------------------


class EchoSynthesiser:
    """Text to speech through one argus-echo engine. See the module docs for the contract and the failures."""

    def __init__(self, config: EchoConfig, *, fetcher: Fetcher | None = None) -> None:
        self.config = config
        self.fetcher = fetcher
        self._allow = [allow_host(config.url)]
        self._headers = {"User-Agent": f"argus-scout/{argus_scout.__version__}"}

    @property
    def max_duration(self) -> float:
        """The longest one :meth:`synthesise` can take: the readiness check plus a cold synthesis."""
        return self.config.health_timeout + self.config.load_timeout + 5.0

    @property
    def max_bytes(self) -> int:
        return int(self.config.max_seconds * STREAM_RATE * STREAM_WIDTH)

    def _host(self) -> str:
        return self._allow[0]

    async def _fetch(self, path: str, **kw: Any) -> Reply:
        return await (self.fetcher or afetch)(self.config.url + path, allow_hosts=self._allow, max_redirects=0, **kw)

    async def readiness(self) -> Readiness:
        """``warm`` or ``cold`` from echo's ``GET /health``; raises :class:`SynthesisError` when it is down or in
        ``error``. A reply that isn't ``ResidentHealth`` counts as cold (the long timeout), not as a failure."""
        timeout = self.config.health_timeout
        try:
            reply = await self._fetch(
                HEALTH_PATH, headers=self._headers, timeout=timeout, deadline=timeout, max_bytes=MAX_HEALTH_BYTES
            )
        except UrlRefused as exc:
            raise SynthesisError(f"argus-echo at {self._host()} does not answer: {exc}") from exc
        if reply.status == 503:
            return "cold"  # a model is loading
        if reply.status != 200:
            raise SynthesisError(f"argus-echo /health answered {reply.status}")
        try:
            health = ResidentHealth.model_validate_json(reply.body)
        except ValidationError as exc:
            log.warning("echo.health_unreadable", host=self._host(), error=exc.errors()[0]["msg"])
            return "cold"
        if health.model_state == "error":
            raise SynthesisError("argus-echo reports model_state=error")
        return "warm" if health.model_state in WARM_STATES else "cold"

    async def synthesise(self, text: str, message: Delivery) -> SpeechAudio | None:
        """Speech for ``text`` (one ``/synthesise/stream`` call), or :class:`SynthesisError`."""
        if not text.strip():
            return None
        cfg = self.config
        state = await self.readiness()
        timeout = cfg.timeout if state == "warm" else cfg.load_timeout
        body = json.dumps(build_request(cfg, text)).encode()
        try:
            reply = await self._fetch(
                STREAM_PATH,
                method="POST",
                content=body,
                headers={**self._headers, "Content-Type": "application/json"},
                timeout=timeout,
                deadline=timeout,
                max_bytes=self.max_bytes,
            )
        except UrlRefused as exc:
            raise SynthesisError(f"argus-echo synthesis failed ({state} model): {exc}") from exc
        if reply.status != 200:
            detail = _error_detail(reply)
            retry_after = reply.headers.get("retry-after") or reply.headers.get("Retry-After")
            permanent = 400 <= reply.status < 500 and reply.status not in (408, 429)
            raise SynthesisError(
                f"argus-echo answered {reply.status}"
                + (f": {detail}" if detail else "")
                + (f" (retry after {retry_after}s)" if retry_after else ""),
                permanent=permanent,
            )
        try:
            fmt = StreamFormat.from_headers(reply.headers)
        except ValueError as exc:
            raise SynthesisError(f"argus-echo stream: {exc}") from exc
        data = reply.body
        if not data:
            raise SynthesisError("argus-echo sent no audio")
        if len(data) % fmt.frame_bytes:
            # Once the 200 is sent, a failure can only end the stream early: a partial frame means it did.
            raise SynthesisError("argus-echo's audio stream ended mid-frame (the engine failed while speaking)")
        if fmt.seconds(len(data)) > cfg.max_seconds:
            raise SynthesisError(f"argus-echo sent more than max_seconds={cfg.max_seconds:g} of audio")
        pcm = await asyncio.to_thread(to_s16le, data, fmt.audio_format)
        audio = pcm if cfg.format == "pcm_s16le" else wav_bytes(pcm, fmt.sample_rate, fmt.channels)
        log.info(
            "echo.synthesised",
            key=message.key,
            seconds=round(fmt.seconds(len(data)), 2),
            model=state,
            format=cfg.format,
        )
        return SpeechAudio(
            data=audio,
            format=cfg.format,
            sample_rate=fmt.sample_rate,
            channels=fmt.channels,
            text=text,
            voice=cfg.voice_clone_id or cfg.speaker,
            cloned=cfg.voice_clone_id is not None,
        )


__all__ = [
    "EchoSynthesiser",
    "StreamFormat",
    "build_request",
    "to_s16le",
    "wav_bytes",
]
