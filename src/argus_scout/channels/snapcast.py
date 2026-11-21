"""The ``snapcast`` voice channel: play speech on the house speakers through a Snapcast pipe stream.

::

    - kind: snapcast
      name: house
      fifo: /run/snapcast/scout.fifo   # the pipe stream's FIFO; snapserver creates it, scout never does
      sample_format: "48000:16:2"      # the stream's sampleformat, rate:bits:channels
      control: tcp://snapserver:1705   # optional JSON-RPC: tcp://host:1705 or http://host:1780/jsonrpc
      stream: scout                    # optional: switch the groups to this stream while speaking
      groups: []                       # group ids or names to switch; empty: every group
      volume: 60                       # optional: client volume (percent) while speaking
      unmute: true                     # unmute muted groups and clients while speaking
      restore_delay: 1.5               # seconds after the last byte before restoring (snapcast's buffer)

**Playback.** The speech is decoded (:mod:`argus_scout.channels.audio`), converted to the stream's
``sampleformat`` (channels, rate, sample width) and written into the FIFO, followed by ``tail_ms`` of
silence. Snapserver reads the pipe in real time, so the write is paced by the pipe itself.

**The FIFO is fenced.** The path comes from config and must already exist *as a FIFO*: a missing path or a
regular file is refused, and nothing is ever created there. It is opened ``O_WRONLY | O_NONBLOCK``, so a FIFO
with no reader fails at once with ``ENXIO`` instead of blocking; scout retries for ``open_timeout`` seconds (a
snapserver reopening its pipe) and then fails the delivery. Writes wait on ``select`` against a deadline, so a
reader that stops reading fails the delivery too. Nothing here can hang scout.

**Control** (optional) goes over Snapcast's JSON-RPC to the one ``control`` host only: a raw TCP connection
(the snapserver's control port, 1705) or an HTTP POST through ``argus_cortex.net`` (1780). Before playback,
the selected groups are switched to ``stream``, unmuted and set to ``volume`` as configured; after it (once the
audio has had time to play out), every change is undone in reverse order, whether playback worked or not.
Each call is bounded by ``control_timeout``. A failed setup fails the delivery (whatever was changed is put
back); a failed restore is logged and reported in the result's detail. Snapserver is a delivery target, never a
monitored host: scout sends it nothing but these calls and the audio.

Needs the ``snapcast`` extra (numpy, for format conversion): ``pip install argus-scout[snapcast]``.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import select
import stat
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import structlog
from argus_cortex.net import Reply, UrlRefused, afetch
from pydantic import Field, field_validator

from argus_scout.channels.audio import SampleFormat, decode, encode, require_numpy, silence
from argus_scout.channels.base import ChannelError, Delivery, DeliveryResult, SpeechAudio
from argus_scout.config import ChannelSettings, ConfigPath, NullAsEmptyList
from argus_scout.sources.json_poll import allow_host

log = structlog.get_logger(__name__)

EXTRA = "snapcast"
CHUNK_BYTES = 16 * 1024
WRITE_SLACK_S = 10.0
"""How much longer than the audio itself a FIFO write may take before it counts as stalled."""
POLL_S = 0.05
MAX_RPC_REPLY = 4 * 1024 * 1024

Fetcher = Callable[..., Awaitable[Reply]]


class SnapcastConfig(ChannelSettings):
    """A Snapcast pipe stream to speak into, and optionally the snapserver's JSON-RPC to set it up."""

    kind: Literal["snapcast"]
    fifo: ConfigPath
    """The pipe stream's FIFO. Must exist and be a FIFO: scout never creates it."""
    sample_format: str = "48000:16:2"
    """The stream's ``sampleformat``: ``rate:bits:channels``."""
    open_timeout: float = Field(default=2.0, ge=0, le=30)
    """Seconds to keep retrying a FIFO with no reader before failing."""
    tail_ms: int = Field(default=250, ge=0, le=5000)
    """Silence written after the speech, so the last word isn't cut by snapcast's chunking."""
    control: str | None = None
    """Snapserver JSON-RPC: ``tcp://host:port`` (control port, 1705) or ``http(s)://host:port/jsonrpc`` (1780)."""
    stream: str | None = None
    """Switch the groups to this stream (the pipe stream's ``name``) while speaking."""
    groups: Annotated[list[str], NullAsEmptyList] = Field(default_factory=list)
    """Group ids or names to set up. Empty: every group."""
    volume: int | None = Field(default=None, ge=0, le=100)
    """Client volume, in percent, while speaking. Unset: leave volumes alone."""
    unmute: bool = True
    """Unmute muted groups and clients while speaking."""
    restore_delay: float = Field(default=1.5, ge=0, le=30)
    """Seconds after the last byte is written before restoring: snapcast plays its buffer behind the pipe."""
    control_timeout: float = Field(default=5.0, gt=0, le=60)
    timeout: float = Field(default=120.0, gt=0, le=600)

    @field_validator("sample_format")
    @classmethod
    def _format(cls, value: str) -> str:
        return str(SampleFormat.parse(value))

    @field_validator("control")
    @classmethod
    def _control(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("tcp", "http", "https") or not parts.hostname:
            raise ValueError(f"{value!r} is not tcp://host:port or http(s)://host:port/jsonrpc")
        if parts.scheme == "tcp" and (parts.path not in ("", "/") or parts.query):
            raise ValueError("a tcp:// control address is just host:port")
        return value.rstrip("/") if parts.scheme == "tcp" else value

    @property
    def format(self) -> SampleFormat:
        return SampleFormat.parse(self.sample_format)


# --- JSON-RPC ------------------------------------------------------------------------------------------------


class SnapcastRpc:
    """JSON-RPC 2.0 to one snapserver, over raw TCP (newline-delimited) or HTTP. Every call is bounded."""

    def __init__(self, url: str, *, timeout: float = 5.0, fetcher: Fetcher | None = None) -> None:
        self.url = url
        self.timeout = timeout
        self.fetcher = fetcher
        parts = urlsplit(url)
        self.scheme = parts.scheme
        self.host = parts.hostname or ""
        self.port = parts.port or {"tcp": 1705, "http": 1780, "https": 443}[parts.scheme]
        self._ids = 0

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._ids += 1
        request: dict[str, Any] = {"id": self._ids, "jsonrpc": "2.0", "method": method}
        if params is not None:
            request["params"] = params
        try:
            reply = await asyncio.wait_for(self._send(request), timeout=self.timeout)
        except TimeoutError:
            raise ChannelError(f"snapserver {method} timed out after {self.timeout:g}s") from None
        except (OSError, UrlRefused, ValueError) as exc:
            raise ChannelError(f"snapserver {method} failed: {exc}") from exc
        if not isinstance(reply, dict):
            raise ChannelError(f"snapserver {method}: not a JSON-RPC reply")
        if reply.get("error"):
            raise ChannelError(f"snapserver {method}: {reply['error']}")
        if "result" not in reply:
            raise ChannelError(f"snapserver {method}: the reply has no result")
        return reply["result"]

    async def _send(self, request: dict[str, Any]) -> Any:
        body = json.dumps(request).encode()
        if self.scheme == "tcp":
            return await self._tcp(body, request["id"])
        reply = await (self.fetcher or afetch)(
            self.url,
            method="POST",
            content=body,
            headers={"Content-Type": "application/json"},
            allow_hosts=[allow_host(self.url)],
            timeout=self.timeout,
            deadline=self.timeout,
            max_bytes=MAX_RPC_REPLY,
            max_redirects=0,
        )
        if not reply.ok:
            raise ValueError(f"HTTP {reply.status}")
        return json.loads(reply.body)

    async def _tcp(self, body: bytes, request_id: int) -> Any:
        # Raw TCP to the configured snapserver only: a delivery target (speakers), not a monitored host.
        reader, writer = await asyncio.open_connection(self.host, self.port, limit=MAX_RPC_REPLY)
        try:
            writer.write(body + b"\r\n")
            await writer.drain()
            while True:  # the control port also pushes notifications (no id): skip them
                line = await reader.readline()
                if not line:
                    raise ValueError("the connection closed before the reply")
                message = json.loads(line)
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


Change = tuple[str, dict[str, Any]]


def _selected(status: dict[str, Any], wanted: list[str]) -> list[dict[str, Any]]:
    groups = [g for g in (status.get("server") or {}).get("groups") or [] if isinstance(g, dict)]
    if not wanted:
        return groups
    chosen = [g for g in groups if g.get("id") in wanted or (g.get("name") or "") in wanted]
    missing = set(wanted) - {g.get("id") for g in chosen} - {g.get("name") for g in chosen}
    if missing:
        raise ChannelError(f"snapserver has no group {', '.join(sorted(missing))}")
    return chosen


def plan(status: dict[str, Any], cfg: SnapcastConfig) -> list[tuple[Change, Change]]:
    """The calls that set the groups up for speech, each with the call that undoes it, from ``Server.GetStatus``."""
    server = status.get("server") or {}
    if cfg.stream is not None:
        streams = {s.get("id") for s in server.get("streams") or [] if isinstance(s, dict)}
        if cfg.stream not in streams:
            raise ChannelError(f"snapserver has no stream {cfg.stream!r} (has: {', '.join(sorted(map(str, streams)))})")
    steps: list[tuple[Change, Change]] = []
    for group in _selected(status, cfg.groups):
        gid = group.get("id")
        if cfg.stream is not None and group.get("stream_id") != cfg.stream:
            steps.append(
                (
                    ("Group.SetStream", {"id": gid, "stream_id": cfg.stream}),
                    ("Group.SetStream", {"id": gid, "stream_id": group.get("stream_id")}),
                )
            )
        if cfg.unmute and group.get("muted"):
            steps.append((("Group.SetMute", {"id": gid, "mute": False}), ("Group.SetMute", {"id": gid, "mute": True})))
        for client in group.get("clients") or []:
            volume = ((client.get("config") or {}).get("volume")) or {}
            old = {"muted": bool(volume.get("muted", False)), "percent": int(volume.get("percent", 100))}
            new = dict(old)
            if cfg.unmute:
                new["muted"] = False
            if cfg.volume is not None:
                new["percent"] = cfg.volume
            if new != old:
                cid = client.get("id")
                steps.append(
                    (("Client.SetVolume", {"id": cid, "volume": new}), ("Client.SetVolume", {"id": cid, "volume": old}))
                )
    return steps


# --- the FIFO ------------------------------------------------------------------------------------------------


def open_fifo(path: os.PathLike[str] | str, *, timeout: float, stop: threading.Event | None = None) -> int:
    """A non-blocking write descriptor on the FIFO at ``path``, or :class:`ChannelError`.

    Refuses a missing path or anything that isn't a FIFO (and never creates one). A FIFO with no reader is
    retried until ``timeout`` and then refused: an open for write never blocks.
    """
    try:
        info = os.stat(path)
    except FileNotFoundError:
        raise ChannelError(f"no FIFO at {path}: scout never creates it (snapserver's pipe stream does)") from None
    except OSError as exc:
        raise ChannelError(f"cannot stat {path}: {exc.strerror or exc}") from exc
    if not stat.S_ISFIFO(info.st_mode):
        raise ChannelError(f"{path} is not a FIFO: refusing to write to it")
    deadline = time.monotonic() + timeout
    flags = os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    while True:
        try:
            fd = os.open(path, flags)  # no O_CREAT: nothing is ever created here
            break
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                raise ChannelError(f"cannot open {path}: {exc.strerror or exc}") from exc
            if time.monotonic() >= deadline or (stop is not None and stop.is_set()):
                raise ChannelError(f"{path} has no reader (is snapserver running?)") from None
            time.sleep(POLL_S)
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):  # swapped between the stat and the open
        os.close(fd)
        raise ChannelError(f"{path} is not a FIFO: refusing to write to it")
    return fd


def write_fifo(fd: int, data: bytes, *, deadline: float, stop: threading.Event | None = None) -> float:
    """Write all of ``data`` to the non-blocking ``fd``, waiting on ``select``; the monotonic time the first
    byte went in. :class:`ChannelError` if the reader goes away, stalls past ``deadline`` or ``stop`` is set."""
    view = memoryview(data)
    started: float | None = None
    while view:
        if stop is not None and stop.is_set():
            raise ChannelError("playback was cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChannelError("the FIFO stopped draining (snapserver stalled)")
        _, ready, _ = select.select([], [fd], [], min(0.2, remaining))
        if not ready:
            continue
        try:
            written = os.write(fd, view[:CHUNK_BYTES])
        except BlockingIOError:
            continue
        except BrokenPipeError:
            raise ChannelError("the FIFO's reader went away mid-speech") from None
        if started is None:
            started = time.monotonic()
        view = view[written:]
    return started if started is not None else time.monotonic()


def play_into_fifo(cfg: SnapcastConfig, data: bytes, duration: float, stop: threading.Event | None = None) -> float:
    """Open the FIFO and write ``data`` (``duration`` seconds of audio) into it; when the first byte went in."""
    fd = open_fifo(cfg.fifo, timeout=cfg.open_timeout, stop=stop)
    try:
        return write_fifo(fd, data, deadline=time.monotonic() + duration + WRITE_SLACK_S, stop=stop)
    finally:
        os.close(fd)


# --- the channel ---------------------------------------------------------------------------------------------


class SnapcastChannel:
    """The ``snapcast`` voice channel kind."""

    kind = "snapcast"
    config_model = SnapcastConfig

    def __init__(self, config: SnapcastConfig, *, fetcher: Fetcher | None = None) -> None:
        require_numpy(EXTRA)
        self.config = config
        self.format = config.format
        self.rpc = (
            SnapcastRpc(config.control, timeout=config.control_timeout, fetcher=fetcher) if config.control else None
        )

    async def deliver(self, message: Delivery) -> DeliveryResult:
        return DeliveryResult(ok=False, error="snapcast is a voice channel: it plays speech, not text")

    async def _restore(self, undo: list[Change]) -> list[str]:
        assert self.rpc is not None
        errors = []
        for method, params in reversed(undo):
            try:
                await self.rpc.call(method, params)
            except ChannelError as exc:
                errors.append(str(exc))
                log.warning("channel.snapcast.restore_failed", channel=self.config.name, error=str(exc))
        return errors

    async def play(self, audio: SpeechAudio, message: Delivery) -> DeliveryResult:
        cfg = self.config
        data = encode(decode(audio), self.format) + silence(self.format, cfg.tail_ms)
        duration = len(data) / self.format.bytes_per_second
        undo: list[Change] = []
        restore_errors: list[str] = []
        try:
            if self.rpc is not None:
                status = await self.rpc.call("Server.GetStatus")
                for change, revert in plan(status, cfg):
                    await self.rpc.call(*change)
                    undo.append(revert)
            stop = threading.Event()
            try:
                started = await asyncio.to_thread(play_into_fifo, cfg, data, duration, stop)
            except asyncio.CancelledError:
                stop.set()  # the writer thread notices within a poll and closes the FIFO
                raise
            if undo:  # let the speakers play out what is still buffered before putting things back
                await asyncio.sleep(max(0.0, started + duration + cfg.restore_delay - time.monotonic()))
        finally:
            if undo:
                restore_errors = await self._restore(undo)
        detail = f"played {duration:.1f}s into {os.path.basename(cfg.fifo)} as {self.format}"
        if undo:
            detail += f"; {len(undo)} snapserver setting{'s' * (len(undo) != 1)} changed and restored"
        if restore_errors:
            detail += f" (restore failed: {restore_errors[0]})"
        return DeliveryResult(ok=True, detail=detail)


__all__ = ["SnapcastChannel", "SnapcastConfig", "SnapcastRpc", "open_fifo", "plan", "play_into_fifo", "write_fifo"]
