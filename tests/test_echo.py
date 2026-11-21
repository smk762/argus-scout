"""argus-echo: the shared HTTP contract and the resident contract, over a stubbed transport (no network)."""

from __future__ import annotations

import array
import functools
import io
import json
import math
import sys
import wave
from datetime import UTC, datetime

import argus_cortex.gpu as gpu
import httpx
import pytest
from argus_cortex.net import afetch

from argus_scout.channels import (
    BoundChannel,
    ChannelError,
    Delivery,
    DeliveryResult,
    Dispatcher,
    EchoSynthesiser,
    NullSynthesiser,
    OpenGates,
    SynthesisError,
    build_dispatcher,
)
from argus_scout.channels.echo import ECHO_VOICE_KEYS, StreamFormat, build_request, to_s16le
from argus_scout.config import ECHO_REQUEST_FIELDS, ConfigError, EchoConfig, TextChannel, parse_config
from argus_scout.store import Report, SqliteStore

T0 = datetime(2026, 11, 21, 7, 30, tzinfo=UTC)
ECHO = EchoConfig(url="http://echo:8000", voice_clone_id="narrator", language="en", extra={"temperature": 0.7})
WARM = {"model_state": "gpu", "busy": False, "idle_s": 3.0, "engine": "xtts", "model": "xtts-v2", "device": "cuda"}
STREAM_HEADERS = {"X-Sample-Rate": "24000", "X-Audio-Format": "pcm_f32le", "X-Channels": "1"}


def f32(*samples: float) -> bytes:
    data = array.array("f", samples)
    if sys.byteorder == "big":
        data.byteswap()
    return data.tobytes()


def s16(data: bytes) -> list[int]:
    out = array.array("h")
    out.frombytes(data)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tolist()


class Echo:
    """A stub argus-echo engine behind httpx.MockTransport, recording every request."""

    def __init__(self, *, health=200, health_body=None, status=200, body=None, headers=None, down=False):
        self.health, self.health_body = health, WARM if health_body is None else health_body
        self.status, self.headers = status, STREAM_HEADERS if headers is None else headers
        self.body = f32(0.0, 0.5, -1.0, 2.0) if body is None else body
        self.down = down
        self.requests: list[httpx.Request] = []
        self.calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("refused")
        if request.url.path == "/health":
            return httpx.Response(self.health, json=self.health_body)
        assert request.url.path == "/synthesise/stream" and request.method == "POST"
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"}, headers={"Retry-After": "7"})
        return httpx.Response(200, content=self.body, headers=self.headers)

    def fetcher(self):
        real = functools.partial(
            afetch, transport=httpx.MockTransport(self.handler), resolve=lambda host, port: ["10.0.0.9"]
        )

        async def fetch(url, **kw):
            self.calls.append({"url": url, **kw})
            return await real(url, **kw)

        return fetch

    @property
    def syntheses(self) -> int:
        return sum(r.url.path == "/synthesise/stream" for r in self.requests)


def synth(echo: Echo, cfg: EchoConfig = ECHO) -> EchoSynthesiser:
    return EchoSynthesiser(cfg, fetcher=echo.fetcher())


def message(text="Good morning. All quiet.") -> Delivery:
    return Delivery.from_report(Report(id=1, kind="morning", generated_at=T0, text=text))


# --- the contract ---------------------------------------------------------------------------------------------


async def test_request_and_response_follow_the_contract():
    echo = Echo()
    audio = await synth(echo).synthesise("Good morning.", message())
    health, stream = echo.requests
    assert (health.method, health.url.path) == ("GET", "/health")
    assert (stream.method, stream.url.path) == ("POST", "/synthesise/stream")
    assert stream.headers["host"] == "echo:8000" and stream.headers["content-type"] == "application/json"
    # SynthesiseRequest: the shared fields that are set, plus engine extras; nothing unset is sent.
    assert json.loads(stream.content) == {
        "text": "Good morning.",
        "voice_clone_id": "narrator",
        "language": "en",
        "temperature": 0.7,
    }
    # 24 kHz mono float32 from the headers, handed on as 16-bit PCM (clipped to [-1, 1]).
    assert (audio.format, audio.sample_rate, audio.channels, audio.voice) == ("pcm_s16le", 24000, 1, "narrator")
    assert s16(audio.data) == [0, 16384, -32767, 32767]
    assert audio.text == "Good morning."
    # Only the echo host is allowed, a POST is never redirected, the reply is bounded, and warm means `timeout`.
    call = echo.calls[1]
    assert call["allow_hosts"] == ["echo:8000"] and call["max_redirects"] == 0
    assert call["max_bytes"] == int(ECHO.max_seconds * 24000 * 4)
    assert call["timeout"] == call["deadline"] == ECHO.timeout


async def test_build_request_and_stream_format():
    cfg = EchoConfig(url="http://echo:8000", speaker="Claribel Dervla", instruction="slowly", emotion="calm")
    assert build_request(cfg, "hi") == {
        "text": "hi",
        "language": "en",
        "speaker": "Claribel Dervla",
        "instruction": "slowly",
        "emotion": "calm",
    }
    fmt = StreamFormat.from_headers({"x-sample-rate": "24000", "x-audio-format": "pcm_s16le", "x-channels": "2"})
    assert fmt.frame_bytes == 4 and fmt.seconds(96000) == 1.0
    with pytest.raises(ValueError, match="missing stream header"):
        StreamFormat.from_headers({"X-Sample-Rate": "24000"})
    with pytest.raises(ValueError, match="unknown audio format"):
        StreamFormat.from_headers({**STREAM_HEADERS, "X-Audio-Format": "mp3"})
    assert to_s16le(b"\x01\x00", "pcm_s16le") == b"\x01\x00"


async def test_wav_output_and_s16_input():
    pcm = array.array("h", [0, 1000, -1000, 0])
    if sys.byteorder == "big":
        pcm.byteswap()
    echo = Echo(body=pcm.tobytes(), headers={**STREAM_HEADERS, "X-Audio-Format": "pcm_s16le"})
    audio = await synth(echo, EchoConfig(url="http://echo:8000", format="wav")).synthesise("hi", message())
    assert audio.format == "wav"
    with wave.open(io.BytesIO(audio.data)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (24000, 1, 2, 4)


async def test_a_cold_model_gets_the_load_timeout():
    """Resident contract: 503 while loading, or model_state unloaded, means the model loads first."""
    for echo in (
        Echo(health=503, health_body={"status": "loading"}),
        Echo(health_body={**WARM, "model_state": "unloaded"}),
    ):
        await synth(echo).synthesise("hi", message())
        assert echo.calls[0]["timeout"] == ECHO.health_timeout
        assert echo.calls[1]["timeout"] == ECHO.load_timeout


async def test_unready_or_refusing_echo_raises_a_typed_error():
    echo = Echo(health_body={**WARM, "model_state": "error"})
    with pytest.raises(SynthesisError, match="model_state=error"):
        await synth(echo).synthesise("hi", message())
    assert echo.syntheses == 0  # never asked to speak

    with pytest.raises(SynthesisError, match="does not answer") as down:
        await synth(Echo(down=True)).synthesise("hi", message())
    assert not down.value.permanent

    with pytest.raises(SynthesisError, match=r"answered 503: nope \(retry after 7s\)") as busy:
        await synth(Echo(status=503)).synthesise("hi", message())
    assert not busy.value.permanent

    with pytest.raises(SynthesisError, match="answered 422") as refused:
        await synth(Echo(status=422)).synthesise("hi", message())
    assert refused.value.permanent and isinstance(refused.value, ChannelError)

    with pytest.raises(SynthesisError, match="mid-frame"):
        await synth(Echo(body=f32(0.1)[:3])).synthesise("hi", message())
    with pytest.raises(SynthesisError, match="no audio"):
        await synth(Echo(body=b"")).synthesise("hi", message())
    with pytest.raises(SynthesisError, match="missing stream header"):
        await synth(Echo(headers={})).synthesise("hi", message())
    short = EchoConfig(url="http://echo:8000", max_seconds=0.0001)
    with pytest.raises(SynthesisError):
        await synth(Echo(), short).synthesise("hi", message())


# --- with the dispatcher -----------------------------------------------------------------------------------


class Cfg:
    def __init__(self, name):
        self.name, self.enabled, self.min_severity, self.kinds, self.timeout = name, True, "info", [], 5.0


class Text:
    kind = "fake"
    config_model = TextChannel

    def __init__(self):
        self.got = []

    async def deliver(self, message):
        self.got.append(message)
        return DeliveryResult(ok=True)


class Speaker(Text):
    def __init__(self):
        super().__init__()
        self.played = []

    async def play(self, audio, message):
        self.played.append(audio)
        return DeliveryResult(ok=True, detail="played")


def dispatcher(echo, store=None, speakers=2):
    text, voices = Text(), [Speaker() for _ in range(speakers)]
    channels = [BoundChannel(text, Cfg("log"))] + [BoundChannel(s, Cfg(f"speaker{i}")) for i, s in enumerate(voices)]
    return Dispatcher(channels, synthesiser=synth(echo), gates=OpenGates(), store=store, clock=lambda: T0), text, voices


async def test_one_synthesis_for_every_voice_channel():
    echo = Echo()
    disp, text, (a, b) = dispatcher(echo, speakers=2)
    report = await disp.deliver(message())
    assert echo.syntheses == 1 and report.synthesised
    assert a.played == b.played and a.played[0] is b.played[0]
    assert sorted(report.sent) == ["log", "speaker0", "speaker1"]


async def test_echo_down_still_delivers_text_and_records_the_failure():
    store = SqliteStore(":memory:")
    disp, text, (speaker,) = dispatcher(Echo(down=True), store=store, speakers=1)
    report = await disp.deliver(message())
    assert report.sent == ["log"] and report.failed == ["speaker0"] and len(text.got) == 1
    assert speaker.played == []
    [failure] = [r for r in store.list_deliveries("report:1") if not r.ok]
    assert failure.channel == "speaker0" and "does not answer" in failure.error and not failure.permanent
    # A refusal echo won't change its mind about is recorded as permanent.
    store2 = SqliteStore(":memory:")
    disp, _, _ = dispatcher(Echo(status=400), store=store2, speakers=1)
    await disp.deliver(message())
    assert [r.permanent for r in store2.list_deliveries("report:1") if not r.ok] == [True]


async def test_no_gpu_lease_is_taken_around_synthesis(monkeypatch):
    """Echo takes its own lease: scout never asks a GPU coordinator for one around synthesis."""

    def forbidden(*args, **kwargs):
        raise AssertionError("scout took a GPU lease around echo synthesis")

    for name in ("coordinator_from_env", "build_coordinator"):
        monkeypatch.setattr(gpu, name, forbidden)
    for cls in (gpu.NullCoordinator, gpu.LeaseCoordinator):
        monkeypatch.setattr(cls, "lease", forbidden)
    echo = Echo()
    disp, _, (speaker,) = dispatcher(echo, speakers=1)
    report = await disp.deliver(message())
    assert report.ok and len(speaker.played) == 1


def test_build_dispatcher_picks_echo_when_configured():
    _, plain = build_dispatcher(parse_config({}))
    assert isinstance(plain.synthesiser, NullSynthesiser)
    _, voiced = build_dispatcher(parse_config({"services": {"echo": {"url": "http://echo:8000/"}}}))
    assert isinstance(voiced.synthesiser, EchoSynthesiser)
    assert voiced.synthesiser.config.url == "http://echo:8000"
    assert voiced.synthesis_timeout >= voiced.synthesiser.max_duration


# --- config ----------------------------------------------------------------------------------------------------


def test_echo_config_and_the_echo_url_break():
    config = parse_config({"services": {"echo": {"url": "http://echo:8000", "speaker": "0", "format": "wav"}}})
    echo = config.services.echo
    assert (echo.url, echo.speaker, echo.format, echo.language, echo.timeout) == (
        "http://echo:8000",
        "0",
        "wav",
        "en",
        60.0,
    )
    with pytest.raises(ConfigError, match=r"echo_url was replaced by echo: \{url: \.\.\.\}"):
        parse_config({"services": {"echo_url": "http://echo:8000"}})
    with pytest.raises(ConfigError, match="set speaker as its own key"):
        parse_config({"services": {"echo": {"url": "http://echo:8000", "extra": {"speaker": "x"}}}})
    with pytest.raises(ConfigError, match="format"):
        parse_config({"services": {"echo": {"url": "http://echo:8000", "format": "mp3"}}})
    assert parse_config({"services": {"echo": {"url": "http://echo:8000", "extra": None}}}).services.echo.extra == {}


def test_every_reserved_request_field_has_its_own_key():
    # A shared field refused under ``extra`` ("set it as its own key") must be a key, or it can't be set at all:
    # ``speed`` once was reserved but missing.
    assert set(ECHO_VOICE_KEYS) | {"text", "language"} == ECHO_REQUEST_FIELDS
    assert set(ECHO_VOICE_KEYS) <= set(EchoConfig.model_fields)
    for key in ECHO_REQUEST_FIELDS - {"text"}:
        with pytest.raises(ConfigError, match=f"set {key} as its own key"):
            parse_config({"services": {"echo": {"url": "http://echo:8000", "extra": {key: "x"}}}})


def test_speed_is_a_key_and_reaches_the_request():
    echo = parse_config({"services": {"echo": {"url": "http://echo:8000", "speed": 1.2}}}).services.echo
    assert echo.speed == 1.2
    assert build_request(echo, "hi") == {"text": "hi", "language": "en", "speed": 1.2}
    assert "speed" not in build_request(EchoConfig(url="http://echo:8000"), "hi")  # unset: not sent
    for bad in (0, -1, 10):
        with pytest.raises(ConfigError, match="speed"):
            parse_config({"services": {"echo": {"url": "http://echo:8000", "speed": bad}}})


# --- what voice channels receive -----------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["pcm_s16le", "wav"])
async def test_echo_output_feeds_the_snapcast_and_mask_conversion(fmt):
    """Whatever echo sends (float32 here), the audio scout hands on is a format channels/audio.py decodes."""
    pytest.importorskip("numpy")
    from argus_scout.channels.audio import SUPPORTED_FORMATS, SampleFormat, decode, encode
    from argus_scout.channels.audio import to_s16le as audio_to_s16le

    samples = [math.sin(i / 10) * 0.5 for i in range(2400)]  # 0.1 s at 24 kHz
    cfg = EchoConfig(url="http://echo:8000", format=fmt)
    audio = await synth(Echo(body=f32(*samples)), cfg).synthesise("hi", message())
    assert audio.format in SUPPORTED_FORMATS
    pcm = decode(audio)
    assert (pcm.sample_rate, pcm.channels, pcm.frames) == (24000, 1, 2400)
    assert abs(float(pcm.samples[10, 0]) - samples[10]) < 1e-3
    snap = encode(pcm, SampleFormat(48000, 16, 2))  # Snapcast's default stream format
    assert len(snap) == 4800 * 2 * 2
    assert len(audio_to_s16le(pcm)) == 2400 * 2  # what the mask channel pushes


async def test_speech_from_a_clone_is_marked_cloned():
    cloned = await synth(Echo()).synthesise("hi", message())
    assert cloned.cloned and cloned.voice == "narrator"
    builtin_cfg = EchoConfig(url="http://echo:8000", speaker="Claribel Dervla")
    designed_cfg = EchoConfig(url="http://echo:8000", voice_description="warm, low")
    assert not (await synth(Echo(), builtin_cfg).synthesise("hi", message())).cloned
    assert not (await synth(Echo(), designed_cfg).synthesise("hi", message())).cloned


def mask_health():
    return {
        "driver": {"available": True},
        "read_only": False,
        "live": [],
        "screens": {"holly": [{"display": "flat", "audio_ready": True}]},
        "aliases": {},
    }


class FakeMask:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request):
        self.requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json=mask_health())
        return httpx.Response(200, json={"session_id": "s", "frames": 3, "duration_ms": 100})

    @property
    def fetcher(self):
        return functools.partial(afetch, transport=httpx.MockTransport(self.handler), resolve=lambda h, p: ["10.0.0.5"])


def mask(fake, **kw):
    pytest.importorskip("numpy")
    from argus_scout.channels.mask import MaskChannel, MaskConfig

    cfg = MaskConfig(kind="mask", name="face", url="http://mask:8000", face="holly", **kw)
    return MaskChannel(cfg, environ={}, fetcher=fake.fetcher)


async def test_mask_refuses_a_cloned_voice_unless_allowed():
    """A cloned voice is never pushed to a face without disclosure: /push can't carry mask's disclosure label."""
    audio = await synth(Echo()).synthesise("hi", message())
    fake = FakeMask()
    refused = await mask(fake).play(audio, message())
    assert refused.ok and refused.status == "skipped" and refused.permanent
    assert "cloned voice" in refused.detail and "allow_cloned_voice" in refused.detail
    assert fake.requests == []  # mask is never even asked

    allowed = FakeMask()
    result = await mask(allowed, allow_cloned_voice=True).play(audio, message())
    assert result.ok and result.status is None
    assert [r.url.path for r in allowed.requests] == ["/health", "/push"]

    synthetic_cfg = EchoConfig(url="http://echo:8000", speaker="0")
    synthetic = await synth(Echo(), synthetic_cfg).synthesise("hi", message())
    plain = FakeMask()
    assert (await mask(plain).play(synthetic, message())).status is None
    assert len(plain.requests) == 2


async def test_a_refused_clone_is_reported_not_recorded_and_never_retried():
    from argus_scout.channels import RedeliveryJob
    from argus_scout.config import DeliveryConfig

    store = SqliteStore(":memory:")
    report = store.add_report("morning", T0, "All quiet.")
    face = mask(FakeMask())
    disp = Dispatcher(
        [BoundChannel(face, face.config)], synthesiser=synth(Echo()), gates=OpenGates(), store=store, clock=lambda: T0
    )
    result = await disp.deliver(Delivery.from_report(report))
    assert result.get("face").status == "skipped" and result.ok
    assert store.list_deliveries(f"report:{report.id}") == []
    assert (await RedeliveryJob(store, disp, DeliveryConfig(), clock=lambda: T0)()).reports == []
