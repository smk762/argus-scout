"""The Snapcast and argus-mask voice channels, and the audio conversion they share."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import io
import json
import math
import os
import struct
import sys
import threading
import time
import wave
from datetime import UTC, datetime

import httpx
import pytest
from argus_cortex.net import afetch

np = pytest.importorskip("numpy")

from argus_scout.channels import Delivery, Dispatcher, OpenGates, SpeechAudio, build_channels  # noqa: E402
from argus_scout.channels import snapcast as snapcast_module  # noqa: E402
from argus_scout.channels.audio import (  # noqa: E402
    AudioFormatError,
    Pcm,
    SampleFormat,
    decode,
    encode,
    remix,
    resample,
    silence,
    to_s16le,
    to_wav,
)
from argus_scout.channels.base import ChannelError  # noqa: E402
from argus_scout.channels.mask import MaskChannel, MaskConfig, readiness  # noqa: E402
from argus_scout.channels.registry import BoundChannel  # noqa: E402
from argus_scout.channels.snapcast import (  # noqa: E402
    SnapcastChannel,
    SnapcastConfig,
    SnapcastRpc,
    open_fifo,
    plan,
)
from argus_scout.config import parse_config  # noqa: E402
from argus_scout.store import Report, SqliteStore  # noqa: E402

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")


def message() -> Delivery:
    return Delivery.from_report(Report(id=1, kind="morning", generated_at=T0, text="Good morning.", meta={}))


def sine(rate: int = 22050, seconds: float = 0.2, freq: float = 440.0, channels: int = 1) -> Pcm:
    t = np.arange(int(rate * seconds)) / rate
    mono = (0.5 * np.sin(2 * math.pi * freq * t)).astype(np.float32)[:, None]
    return Pcm(np.repeat(mono, channels, axis=1), rate)


def wav_bytes(pcm: Pcm) -> bytes:
    return to_wav(pcm)


def speech(pcm: Pcm | None = None) -> SpeechAudio:
    pcm = pcm or sine()
    return SpeechAudio(data=wav_bytes(pcm), format="wav", sample_rate=pcm.sample_rate, channels=pcm.channels)


# --- audio conversion ----------------------------------------------------------------------------------------


def test_wav_and_raw_pcm_decode_to_the_same_samples():
    pcm = sine(channels=2)
    from_wav = decode(SpeechAudio(data=to_wav(pcm), format="wav", sample_rate=1, channels=1))  # header wins
    assert (from_wav.sample_rate, from_wav.channels, from_wav.frames) == (22050, 2, pcm.frames)
    raw = SpeechAudio(data=to_s16le(pcm), format="pcm_s16le", sample_rate=22050, channels=2)
    from_raw = decode(raw)
    assert np.array_equal(from_raw.samples, from_wav.samples)
    assert np.allclose(from_raw.samples, pcm.samples, atol=1 / 32767)
    # and back: PCM -> WAV -> PCM is lossless at 16 bits
    assert to_s16le(decode(SpeechAudio(data=to_wav(from_raw), format="wav"))) == raw.data


def test_wav_encodings_float_24_bit_and_8_bit():
    pcm = sine()
    ints = np.round(pcm.samples[:, 0] * (2**23 - 1)).astype(np.int32)
    packed = b"".join(struct.pack("<i", int(v))[:3] for v in ints)
    wav24 = _riff(1, 1, 22050, 24, packed)
    assert np.allclose(decode(SpeechAudio(data=wav24)).samples, pcm.samples, atol=1e-6)
    wavf = _riff(3, 1, 22050, 32, pcm.samples.astype("<f4").tobytes())
    assert np.allclose(decode(SpeechAudio(data=wavf)).samples, pcm.samples)
    ext = _riff(0xFFFE, 1, 22050, 32, pcm.samples.astype("<f4").tobytes(), sub=3)
    assert np.allclose(decode(SpeechAudio(data=ext)).samples, pcm.samples)
    wav8 = _riff(1, 1, 8000, 8, bytes([128, 255, 0]))
    assert np.allclose(decode(SpeechAudio(data=wav8)).samples[:, 0], [0, 127 / 128, -1])


def _riff(tag: int, channels: int, rate: int, bits: int, data: bytes, sub: int | None = None) -> bytes:
    align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * align, align, bits)
    if sub is not None:
        fmt += struct.pack("<HHI", 22, bits, 0) + struct.pack("<H", sub) + bytes(14)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_unsupported_formats_are_refused_clearly():
    with pytest.raises(AudioFormatError, match="'mp3' is not supported.*WAV or raw PCM"):
        decode(SpeechAudio(data=b"ID3...", format="mp3"))
    with pytest.raises(AudioFormatError, match="not a WAV file"):
        decode(SpeechAudio(data=b"OggS....", format="wav"))
    with pytest.raises(AudioFormatError, match="not supported"):
        decode(SpeechAudio(data=_riff(0x0055, 1, 8000, 16, b"\0\0")))  # an MP3-in-WAV tag


def test_resampling_and_remixing():
    pcm = sine(rate=22050, seconds=0.5, freq=440)
    up = resample(pcm, 48000)
    assert up.sample_rate == 48000 and up.frames == round(pcm.frames * 48000 / 22050)
    spectrum = np.abs(np.fft.rfft(up.samples[:, 0]))
    peak = np.argmax(spectrum) * 48000 / up.frames
    assert abs(peak - 440) < 5  # the pitch survives
    assert resample(pcm, 22050) is pcm
    stereo = remix(pcm, 2)
    assert stereo.channels == 2 and np.array_equal(stereo.samples[:, 0], stereo.samples[:, 1])
    back = remix(Pcm(np.stack([np.ones(4), -np.ones(4)], axis=1).astype(np.float32), 8000), 1)
    assert np.array_equal(back.samples, np.zeros((4, 1)))


def test_encode_to_snapcast_sample_formats():
    pcm = Pcm(np.array([[1.0], [-1.0], [0.5]], dtype=np.float32), 48000)
    assert encode(pcm, SampleFormat.parse("48000:16:2")) == struct.pack(
        "<6h", 32767, 32767, -32767, -32767, 16384, 16384
    )
    # snapcast keeps 24-bit samples in 4-byte containers
    assert encode(pcm, SampleFormat.parse("48000:24:1")) == struct.pack("<3i", 2**23 - 1, -(2**23 - 1), 2**22)
    assert SampleFormat.parse("48000:24:2").bytes_per_second == 48000 * 2 * 4
    assert silence(SampleFormat.parse("48000:16:2"), 10) == bytes(480 * 4)
    for bad in ("48000:16", "48000:12:2", "1000:16:2", "x"):
        with pytest.raises(ValueError):
            SampleFormat.parse(bad)


# --- snapcast: the FIFO --------------------------------------------------------------------------------------


def snap_cfg(fifo, **kw) -> SnapcastConfig:
    return SnapcastConfig(kind="snapcast", name="house", fifo=fifo, **kw)


class FifoReader(threading.Thread):
    """Reads a FIFO until EOF, like snapserver's pipe stream."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        self.data = bytearray()
        self.opened = threading.Event()

    def run(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        self.opened.set()
        os.set_blocking(fd, True)
        try:
            # the writer may not have opened yet: EOF before it has means keep waiting
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                chunk = os.read(fd, 65536)
                if chunk:
                    self.data += chunk
                elif self.data:
                    break
                else:
                    time.sleep(0.01)
        finally:
            os.close(fd)


@needs_fifo
async def test_snapcast_writes_converted_pcm_into_the_fifo(tmp_path):
    fifo = tmp_path / "scout.fifo"
    os.mkfifo(fifo)
    reader = FifoReader(fifo)
    reader.start()
    reader.opened.wait(5)
    pcm = sine(rate=22050, channels=1)
    channel = SnapcastChannel(snap_cfg(fifo, tail_ms=20))
    result = await channel.play(speech(pcm), message())
    reader.join(10)
    fmt = SampleFormat.parse("48000:16:2")
    expected = encode(decode(speech(pcm)), fmt) + silence(fmt, 20)
    assert result.ok and "48000:16:2" in result.detail
    assert bytes(reader.data) == expected
    assert len(expected) == (round(pcm.frames * 48000 / 22050) + 960) * 4  # resampled, stereo, plus the tail


@needs_fifo
async def test_snapcast_raw_pcm_input_at_the_stream_rate_passes_through(tmp_path):
    fifo = tmp_path / "scout.fifo"
    os.mkfifo(fifo)
    reader = FifoReader(fifo)
    reader.start()
    reader.opened.wait(5)
    pcm = sine(rate=48000, channels=2)
    raw = to_s16le(pcm)
    await SnapcastChannel(snap_cfg(fifo, tail_ms=0)).play(
        SpeechAudio(data=raw, format="pcm_s16le", sample_rate=48000, channels=2), message()
    )
    reader.join(10)
    assert bytes(reader.data) == raw


@needs_fifo
async def test_snapcast_fifo_without_a_reader_fails_in_time_not_a_hang(tmp_path):
    fifo = tmp_path / "scout.fifo"
    os.mkfifo(fifo)
    channel = SnapcastChannel(snap_cfg(fifo, open_timeout=0.3))
    started = time.monotonic()
    with pytest.raises(ChannelError, match="has no reader"):
        await asyncio.wait_for(channel.play(speech(), message()), timeout=5)
    assert time.monotonic() - started < 2


@needs_fifo
async def test_snapcast_reader_that_stops_reading_fails_in_time(tmp_path, monkeypatch):
    monkeypatch.setattr(snapcast_module, "WRITE_SLACK_S", 0.3)
    fifo = tmp_path / "scout.fifo"
    os.mkfifo(fifo)
    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # a reader that never reads: the pipe fills up
    try:
        channel = SnapcastChannel(snap_cfg(fifo))
        started = time.monotonic()
        with pytest.raises(ChannelError, match="stopped draining"):
            await asyncio.wait_for(channel.play(speech(sine(seconds=2)), message()), timeout=10)
        assert time.monotonic() - started < 4
    finally:
        os.close(fd)


def test_snapcast_refuses_a_path_that_is_not_a_fifo(tmp_path):
    regular = tmp_path / "scout.fifo"
    regular.write_bytes(b"keep me")
    with pytest.raises(ChannelError, match="is not a FIFO"):
        open_fifo(regular, timeout=0)
    assert regular.read_bytes() == b"keep me"
    with pytest.raises(ChannelError, match="is not a FIFO"):
        open_fifo(tmp_path, timeout=0)
    missing = tmp_path / "missing.fifo"
    with pytest.raises(ChannelError, match="never creates it"):
        open_fifo(missing, timeout=0)
    assert not missing.exists()


# --- snapcast: JSON-RPC --------------------------------------------------------------------------------------


def server_status(stream="default", muted=False, percent=30):
    return {
        "server": {
            "streams": [{"id": "default"}, {"id": "scout"}],
            "groups": [
                {
                    "id": "g1",
                    "name": "downstairs",
                    "stream_id": stream,
                    "muted": muted,
                    "clients": [{"id": "c1", "config": {"volume": {"muted": True, "percent": percent}}}],
                },
                {"id": "g2", "name": "office", "stream_id": "default", "muted": False, "clients": []},
            ],
        }
    }


class FakeSnapserver:
    """A snapserver control port: newline-delimited JSON-RPC over TCP, with a notification before every reply."""

    def __init__(self, fail=None):
        self.calls: list[tuple[str, dict | None]] = []
        self.fail = fail or set()

    async def handle(self, reader, writer):
        line = await reader.readline()
        request = json.loads(line)
        self.calls.append((request["method"], request.get("params")))
        notice = {"jsonrpc": "2.0", "method": "Client.OnVolumeChanged", "params": {}}
        writer.write(json.dumps(notice).encode() + b"\r\n")
        if request["method"] in self.fail:
            reply = {"id": request["id"], "jsonrpc": "2.0", "error": {"code": -32603, "message": "nope"}}
        elif request["method"] == "Server.GetStatus":
            reply = {"id": request["id"], "jsonrpc": "2.0", "result": server_status()}
        else:
            reply = {"id": request["id"], "jsonrpc": "2.0", "result": {}}
        writer.write(json.dumps(reply).encode() + b"\r\n")
        await writer.drain()
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


def test_plan_switches_unmutes_sets_volume_and_undoes_each():
    cfg = snap_cfg("/x", stream="scout", volume=60, groups=["downstairs"], control="tcp://snap:1705")
    steps = plan(server_status(muted=True), cfg)
    assert [c for c, _ in steps] == [
        ("Group.SetStream", {"id": "g1", "stream_id": "scout"}),
        ("Group.SetMute", {"id": "g1", "mute": False}),
        ("Client.SetVolume", {"id": "c1", "volume": {"muted": False, "percent": 60}}),
    ]
    assert [u for _, u in steps] == [
        ("Group.SetStream", {"id": "g1", "stream_id": "default"}),
        ("Group.SetMute", {"id": "g1", "mute": True}),
        ("Client.SetVolume", {"id": "c1", "volume": {"muted": True, "percent": 30}}),
    ]
    with pytest.raises(ChannelError, match="no stream 'radio'"):
        plan(server_status(), snap_cfg("/x", stream="radio"))
    with pytest.raises(ChannelError, match="no group attic"):
        plan(server_status(), snap_cfg("/x", groups=["attic"]))
    assert plan(server_status(stream="scout"), snap_cfg("/x", stream="scout", unmute=False, groups=["g1"])) == []


@needs_fifo
async def test_snapcast_sets_volume_and_stream_then_restores_them(tmp_path):
    fifo = tmp_path / "scout.fifo"
    os.mkfifo(fifo)
    reader = FifoReader(fifo)
    reader.start()
    reader.opened.wait(5)
    async with FakeSnapserver() as snap:
        cfg = snap_cfg(
            fifo, control=f"tcp://127.0.0.1:{snap.port}", stream="scout", groups=["g1"], volume=70, restore_delay=0
        )
        result = await SnapcastChannel(cfg).play(speech(), message())
    reader.join(10)
    assert result.ok and "2 snapserver settings changed and restored" in result.detail
    assert snap.calls == [
        ("Server.GetStatus", None),
        ("Group.SetStream", {"id": "g1", "stream_id": "scout"}),
        ("Client.SetVolume", {"id": "c1", "volume": {"muted": False, "percent": 70}}),
        ("Client.SetVolume", {"id": "c1", "volume": {"muted": True, "percent": 30}}),
        ("Group.SetStream", {"id": "g1", "stream_id": "default"}),
    ]
    assert reader.data


async def test_snapcast_restores_even_when_playback_fails(tmp_path):
    async with FakeSnapserver() as snap:
        cfg = snap_cfg(tmp_path / "missing.fifo", control=f"tcp://127.0.0.1:{snap.port}", volume=70)
        with pytest.raises(ChannelError, match="no FIFO"):
            await SnapcastChannel(cfg).play(speech(), message())
    methods = [m for m, _ in snap.calls]
    assert methods == ["Server.GetStatus", "Client.SetVolume", "Client.SetVolume"]
    assert snap.calls[-1][1]["volume"] == {"muted": True, "percent": 30}


async def test_snapcast_reverts_a_setup_call_that_failed(tmp_path):
    # The snapserver may have applied a change whose reply was lost or an error: it is put back all the same.
    async with FakeSnapserver(fail={"Client.SetVolume"}) as snap:
        cfg = snap_cfg(tmp_path / "missing.fifo", control=f"tcp://127.0.0.1:{snap.port}", volume=70)
        with pytest.raises(ChannelError, match="Client.SetVolume"):
            await SnapcastChannel(cfg).play(speech(), message())
    assert [m for m, _ in snap.calls] == ["Server.GetStatus", "Client.SetVolume", "Client.SetVolume"]
    assert snap.calls[-1][1]["volume"] == {"muted": True, "percent": 30}


async def test_snapcast_rpc_errors_and_timeouts_are_bounded():
    async with FakeSnapserver(fail={"Server.GetStatus"}) as snap:
        with pytest.raises(ChannelError, match="Server.GetStatus.*nope"):
            await SnapcastRpc(f"tcp://127.0.0.1:{snap.port}").call("Server.GetStatus")

    async def silent(reader, writer):
        await asyncio.sleep(5)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        started = time.monotonic()
        with pytest.raises(ChannelError, match="timed out"):
            await SnapcastRpc(f"tcp://127.0.0.1:{port}", timeout=0.2).call("Server.GetStatus")
        assert time.monotonic() - started < 2
    finally:
        server.close()


async def test_snapcast_rpc_over_http_goes_to_the_configured_host_only():
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={"id": body["id"], "jsonrpc": "2.0", "result": {"ok": 1}})

    fetcher = functools.partial(afetch, transport=httpx.MockTransport(handler), resolve=lambda h, p: ["10.0.0.7"])
    rpc = SnapcastRpc("http://snapserver:1780/jsonrpc", fetcher=fetcher)
    assert await rpc.call("Server.GetRPCVersion") == {"ok": 1}
    assert seen[0].headers["host"] == "snapserver:1780" and seen[0].url.path == "/jsonrpc"


def test_snapcast_config_is_validated_per_entry(tmp_path):
    config = parse_config(
        {
            "channels": [
                {"kind": "snapcast", "name": "bad", "fifo": "/run/x.fifo", "sample_format": "44100:12:2"},
                {"kind": "snapcast", "name": "badctl", "fifo": "/run/x.fifo", "control": "ftp://snap"},
                {"kind": "snapcast", "name": "good", "fifo": "scout.fifo", "groups": None},
                {"kind": "text", "name": "log"},
            ]
        },
        tmp_path,
    )
    built = build_channels(config)
    assert [c.name for c in built.channels] == ["good", "log"]
    assert "12-bit" in built.errors["bad"] and "tcp://" in built.errors["badctl"]
    good = built.get("good")
    assert good.voice and good.config.fifo == tmp_path / "scout.fifo" and good.config.groups == []


# --- mask ----------------------------------------------------------------------------------------------------


def mask_health(*, screens=None, live=(), read_only=False, available=True, aliases=None):
    return {
        "version": "1.0",
        "driver": {"name": "energy", "available": available, "reason": None if available else "a2f unreachable"},
        "fps": 30,
        "read_only": read_only,
        "faces": 1,
        "live": list(live),
        "screens": screens if screens is not None else {},
        "aliases": aliases or {},
    }


READY = {"holly": [{"display": "flat", "audio_ready": True, "kiosk": True, "sound": "local"}]}


class FakeMask:
    def __init__(self, *healths, push_status=200):
        self.healths = list(healths)
        self.push_status = push_status
        self.requests: list[httpx.Request] = []

    def handler(self, request):
        self.requests.append(request)
        if request.url.path == "/health":
            body = self.healths.pop(0) if len(self.healths) > 1 else self.healths[0]
            return httpx.Response(200, json=body)
        if request.url.path == "/push":
            if self.push_status != 200:
                return httpx.Response(self.push_status, json={"detail": "no screen watching 'holly' can be heard"})
            return httpx.Response(200, json={"session_id": "s1", "frames": 6, "duration_ms": 200})
        return httpx.Response(404)

    @property
    def fetcher(self):
        return functools.partial(afetch, transport=httpx.MockTransport(self.handler), resolve=lambda h, p: ["10.0.0.5"])

    @property
    def pushes(self):
        return [r for r in self.requests if r.url.path == "/push"]


def mask_channel(fake, *, environ=None, sleeps=None, **kw):
    async def sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    cfg = MaskConfig(kind="mask", name="face", url="http://mask:8000", face="holly", **kw)
    return MaskChannel(cfg, environ=environ or {}, fetcher=fake.fetcher, sleep=sleep)


async def test_mask_screen_not_ready_is_skipped_and_nothing_is_pushed():
    fake = FakeMask(mask_health(screens={"holly": [{"display": "flat", "audio_ready": False}]}))
    result = await mask_channel(fake).play(speech(), message())
    assert result.ok and result.status == "skipped" and "can be heard" in result.detail
    assert fake.pushes == []
    no_screen = await mask_channel(FakeMask(mask_health())).play(speech(), message())
    assert no_screen.status == "skipped" and "no screen" in no_screen.detail
    busy = await mask_channel(FakeMask(mask_health(screens=READY, live=["holly"]))).play(speech(), message())
    assert busy.status == "skipped" and "already speaking" in busy.detail


async def test_mask_skip_is_reported_by_the_dispatcher_and_not_recorded(tmp_path):
    store = SqliteStore(tmp_path / "scout.db")
    fake = FakeMask(mask_health())
    channel = mask_channel(fake)

    class Synth:
        async def synthesise(self, text, message):
            return speech()

    dispatcher = Dispatcher(
        [BoundChannel(channel, channel.config)], synthesiser=Synth(), gates=OpenGates(), store=store
    )
    report = await dispatcher.deliver(message())
    assert report.get("face").status == "skipped" and report.ok
    assert store.delivered_channels(message().key) == set()
    assert dispatcher.stats[("face", "skipped")] == 1


async def test_mask_waits_for_a_screen_when_configured():
    fake = FakeMask(mask_health(), mask_health(), mask_health(screens=READY))
    sleeps = []
    result = await mask_channel(fake, sleeps=sleeps, wait_ready=10, poll_interval=1).play(speech(), message())
    assert result.ok and result.status is None and len(sleeps) == 2
    assert len(fake.pushes) == 1


async def test_mask_ready_screen_gets_pcm_with_the_right_headers():
    fake = FakeMask(mask_health(screens={"stock-01": READY["holly"]}, aliases={"holly": "stock-01"}))
    pcm = sine(rate=24000, channels=1)
    result = await mask_channel(fake, seed=7).play(speech(pcm), message())
    assert result.ok and result.status is None and "spoke 0.2s" in result.detail
    (push,) = fake.pushes
    assert push.method == "POST" and push.headers["host"] == "mask:8000"
    assert dict(push.url.params) == {"face": "holly", "seed": "7", "require_screen": "true"}
    assert push.headers["x-sample-rate"] == "24000"
    assert push.headers["x-audio-format"] == "pcm_s16le"
    assert push.headers["x-channels"] == "1"
    assert "authorization" not in push.headers
    assert push.content == to_s16le(decode(speech(pcm)))


async def test_mask_bearer_token_from_env_and_refusal_without_it():
    fake = FakeMask(mask_health(screens=READY))
    await mask_channel(fake, token_env="SCOUT_MASK_TOKEN", environ={"SCOUT_MASK_TOKEN": "s3cret"}).play(
        speech(), message()
    )
    assert {r.headers["authorization"] for r in fake.requests} == {"Bearer s3cret"}
    unset = FakeMask(mask_health(screens=READY))
    with pytest.raises(ChannelError, match="SCOUT_MASK_TOKEN is not set"):
        await mask_channel(unset, token_env="SCOUT_MASK_TOKEN").play(speech(), message())
    assert unset.requests == []


async def test_mask_push_refused_for_a_vanished_screen_is_skipped_and_errors_fail():
    gone = await mask_channel(FakeMask(mask_health(screens=READY), push_status=409)).play(speech(), message())
    assert gone.status == "skipped" and "can be heard" in gone.detail
    with pytest.raises(ChannelError, match="/push answered 503"):
        await mask_channel(FakeMask(mask_health(screens=READY), push_status=503)).play(speech(), message())
    with pytest.raises(ChannelError, match="read-only"):
        readiness(mask_health(screens=READY, read_only=True), "holly")
    with pytest.raises(ChannelError, match="driver is down: a2f unreachable"):
        readiness(mask_health(screens=READY, available=False), "holly")


def test_mask_external_sound_screen_counts_as_ready():
    screens = {"holly": [{"display": "hld", "audio_ready": True, "sound": "external"}]}
    assert readiness(mask_health(screens=screens), "holly")[0]


# --- mask over the source socket (transport: websocket) --------------------------------------------------------


class FakeSource:
    """argus-mask's ``WS /session/<face>/source`` on 127.0.0.1, as its server speaks it (argus_mask.server.app)."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.hellos: list[dict] = []
        self.frames: list[bytes] = []
        self.requests: list = []
        self.ended = False

    async def handler(self, ws):
        self.requests.append(ws.request)
        if self.mode in ("busy", "down"):  # refused before the hello is read, as mask does
            detail = "face 'holly' already has a source" if self.mode == "busy" else "the a2f driver is down: gone"
            await ws.send(json.dumps({"type": "error", "detail": detail}))
            await ws.close(code=1013, reason=detail)
            return
        self.hellos.append(json.loads(await ws.recv()))
        if self.mode == "silent":
            await ws.wait_closed()  # until scout gives up and closes
            return
        if self.mode == "big":
            await ws.send("x" * (128 * 1024))
            return
        await ws.send(json.dumps({"type": "ready", "session_id": "s1", "fps": 30}))
        async for data in ws:
            if isinstance(data, bytes):
                self.frames.append(data)
                if self.mode == "close_early":
                    await ws.close(code=1011, reason="driver failed")
                    return
            elif json.loads(data).get("type") == "end":
                self.ended = True
                break
        if self.mode == "no_done":
            await ws.wait_closed()  # until scout gives up and closes
            return
        samples = sum(len(f) for f in self.frames) // (2 * self.hellos[0]["channels"])
        duration_ms = round(samples * 1000 / self.hellos[0]["sample_rate"])
        await ws.send(json.dumps({"type": "done", "frames": 6, "duration_ms": duration_ms}))
        await ws.close()


@pytest.fixture
async def source_server():
    from websockets.asyncio.server import serve

    servers = []

    async def start(fake, **kw):
        server = await serve(fake.handler, "127.0.0.1", 0, compression=None, max_size=2**23, **kw)
        servers.append(server)
        return server.sockets[0].getsockname()[1]

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()


def ws_channel(port, *, healths=None, resolve=None, **kw):
    """A websocket mask channel at ``mask.test`` (which resolves only through ``resolve``: 127.0.0.1)."""
    cfg = MaskConfig(
        kind="mask", name="face", url=f"http://mask.test:{port}", face="holly", transport="websocket", **kw
    )
    fake = FakeMask(*(healths or [mask_health(screens=READY)]))

    async def sleep(seconds):
        return None

    resolver = resolve or (lambda host, p: ["127.0.0.1"] if host == "mask.test" else [])
    return MaskChannel(cfg, environ={}, fetcher=fake.fetcher, sleep=sleep, resolve=resolver), fake


def cloned(pcm: Pcm | None = None) -> SpeechAudio:
    return dataclasses.replace(speech(pcm), cloned=True, voice="narrator")


async def test_mask_ws_discloses_a_cloned_voice_and_not_a_synthetic_one(source_server):
    fake = FakeSource()
    port = await source_server(fake)
    channel, _ = ws_channel(port, seed=7, cloned_voice_consented=True)
    clone = await channel.play(cloned(), message())
    assert clone.ok and clone.status is None and "disclosed" in clone.detail
    plain = await channel.play(speech(), message())
    assert plain.ok and plain.status is None and "disclosed" not in plain.detail
    assert [h["disclose"] for h in fake.hellos] == [True, False]
    assert fake.hellos[0] == {"sample_rate": 22050, "channels": 1, "format": "s16le", "seed": 7, "disclose": True}


async def test_mask_ws_cloned_voice_needs_the_consent_attestation(source_server):
    """Mask's label asserts consent, which scout can't verify: a clone goes over the socket only on attestation."""
    fake = FakeSource()
    port = await source_server(fake)
    unattested, health = ws_channel(port)
    refused = await unattested.play(cloned(), message())
    assert refused.ok and refused.status == "skipped" and refused.permanent
    assert "cloned_voice_consented" in refused.detail
    assert health.requests == [] and fake.requests == []  # mask is never contacted
    assert (await unattested.play(speech(), message())).status is None  # a synthetic voice is unaffected
    assert [h["disclose"] for h in fake.hellos] == [False]

    attested, _ = ws_channel(port, cloned_voice_consented=True)
    assert attested.config.allow_cloned_voice is False  # the http flag plays no part
    pushed = await attested.play(cloned(), message())
    assert pushed.ok and pushed.status is None and "disclosed" in pushed.detail
    assert [h["disclose"] for h in fake.hellos] == [False, True]


async def test_mask_http_still_refuses_a_clone_whatever_the_attestation():
    http = FakeMask(mask_health(screens=READY))
    refused = await mask_channel(http, cloned_voice_consented=True).play(cloned(), message())
    assert refused.status == "skipped" and refused.permanent and "allow_cloned_voice" in refused.detail
    assert http.requests == []  # mask is never asked


async def test_mask_ws_pcm_arrives_intact_in_capped_frames(source_server, monkeypatch):
    import argus_scout.channels.mask as mask_module

    monkeypatch.setattr(mask_module, "WS_FRAME_BYTES", 4001)  # odd: frames must still hold whole samples
    fake = FakeSource()
    port = await source_server(fake)
    channel, _ = ws_channel(port)
    pcm = sine(seconds=0.5, channels=2)
    result = await channel.play(speech(pcm), message())
    assert result.ok and "0.5s" in result.detail
    assert b"".join(fake.frames) == to_s16le(decode(speech(pcm)))
    assert len(fake.frames) > 1 and all(len(f) <= 4001 and len(f) % 4 == 0 for f in fake.frames)
    assert fake.ended
    request = fake.requests[0]
    assert request.path == "/session/holly/source"
    assert request.headers["Host"] == f"mask.test:{port}"  # the configured name, though it connected to 127.0.0.1
    assert "Origin" not in request.headers  # mask's origin guard lets a client with no Origin through


async def test_mask_ws_busy_face_is_skipped_and_a_refusal_fails(source_server):
    busy = await ws_channel(await source_server(FakeSource("busy")))[0].play(speech(), message())
    assert busy.ok and busy.status == "skipped" and "already has a source" in busy.detail
    with pytest.raises(ChannelError, match="driver is down"):
        await ws_channel(await source_server(FakeSource("down")))[0].play(speech(), message())


async def test_mask_ws_not_ready_is_skipped_before_connecting(source_server):
    fake = FakeSource()
    channel, _ = ws_channel(await source_server(fake), healths=[mask_health()], cloned_voice_consented=True)
    result = await channel.play(cloned(), message())
    assert result.status == "skipped" and "not ready" in result.detail
    assert fake.requests == []


async def test_mask_ws_timeouts_and_early_close_fail_promptly(source_server, monkeypatch):
    import argus_scout.channels.mask as mask_module

    monkeypatch.setattr(mask_module, "WS_READY_TIMEOUT_S", 0.3)
    started = time.monotonic()
    with pytest.raises(ChannelError, match="no ready"):
        await ws_channel(await source_server(FakeSource("silent")))[0].play(speech(), message())
    with pytest.raises(ChannelError, match="driver failed"):
        await ws_channel(await source_server(FakeSource("close_early")))[0].play(speech(), message())
    with pytest.raises(ChannelError, match="outlasted"):
        await ws_channel(await source_server(FakeSource("no_done")), timeout=0.3)[0].play(speech(), message())
    assert time.monotonic() - started < 5


async def test_mask_ws_caps_what_mask_may_send(source_server):
    with pytest.raises(ChannelError, match="closed"):
        await ws_channel(await source_server(FakeSource("big")))[0].play(speech(), message())


async def test_mask_ws_goes_only_to_the_configured_host(source_server):
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    elsewhere = FakeSource()
    other = await source_server(elsewhere)

    def redirect(connection, request):
        return Response(302, "Found", Headers({"Location": f"ws://127.0.0.1:{other}/session/holly/source"}))

    fake = FakeSource()
    with pytest.raises(ChannelError, match="302"):
        await ws_channel(await source_server(fake, process_request=redirect))[0].play(speech(), message())
    assert elsewhere.requests == []  # the redirect was not followed

    with pytest.raises(ChannelError, match="cannot resolve"):
        await ws_channel(await source_server(FakeSource()), resolve=lambda h, p: [])[0].play(speech(), message())

    def broken(host, port):
        raise OSError("no such host")

    with pytest.raises(ChannelError, match="cannot resolve mask.test"):
        await ws_channel(await source_server(FakeSource()), resolve=broken)[0].play(speech(), message())


def test_mask_ws_url_follows_the_configured_scheme():
    from argus_scout.channels.mask import source_url

    assert source_url("http://mask:8000", "holly") == "ws://mask:8000/session/holly/source"
    assert source_url("https://faces.example/mask/", "a.b") == "wss://faces.example/mask/session/a.b/source"


def test_mask_ws_needs_websockets(monkeypatch):
    from argus_scout.channels.audio import MissingExtra

    for name in ("websockets", "websockets.asyncio", "websockets.asyncio.client"):
        monkeypatch.setitem(sys.modules, name, None)
    cfg = MaskConfig(kind="mask", name="face", url="http://mask:8000", face="holly", transport="websocket")
    with pytest.raises(MissingExtra, match=r"argus-scout\[mask\]"):
        MaskChannel(cfg)
    MaskChannel(cfg.model_copy(update={"transport": "http"}))  # http needs only numpy


# --- missing extra -------------------------------------------------------------------------------------------


def test_a_missing_extra_fails_only_that_channel(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)  # import numpy now raises ImportError
    config = parse_config(
        {
            "channels": [
                {"kind": "snapcast", "name": "house", "fifo": "/run/snapcast/scout.fifo"},
                {"kind": "mask", "name": "face", "url": "http://mask:8000", "face": "holly"},
                {"kind": "text", "name": "log"},
            ]
        },
        tmp_path,
    )
    built = build_channels(config)
    assert [c.name for c in built.channels] == ["log"]
    assert "install argus-scout[snapcast]" in built.errors["house"]
    assert "install argus-scout[mask]" in built.errors["face"]


def test_wav_helper_round_trip_through_stdlib_wave():
    data = to_wav(sine(rate=16000, channels=2))
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 2, 2)
