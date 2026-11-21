"""Speech audio for the voice channels: decode what the synthesiser gave, convert it to what a sink wants.

A :class:`~argus_scout.channels.base.SpeechAudio` arrives as one of:

- ``wav``: a RIFF/WAVE file. PCM integer samples of 8, 16, 24 or 32 bits, or IEEE float (32 or 64 bit),
  including ``WAVE_FORMAT_EXTENSIBLE``. The header's rate and channel count win over the dataclass's.
- ``pcm_s16le``: raw little-endian 16-bit samples, interleaved, described by ``sample_rate`` and ``channels``.
- ``pcm_f32le``: the same, 32-bit float.

Anything else (``mp3``, ``ogg``) is refused with :class:`AudioFormatError`: scout has no decoder for it, and
argus-echo can produce WAV or raw PCM.

Conversion (channel count, sample rate, sample width) needs **numpy**, which is not a core dependency: it comes
with the ``snapcast`` and ``mask`` extras. :func:`require_numpy` gives the error to show when it is missing.
Resampling is linear interpolation: plenty for speech, and no extra dependency.
"""

from __future__ import annotations

import io
import struct
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from argus_scout.channels.base import ChannelError, SpeechAudio

if TYPE_CHECKING:
    import numpy as np

_PCM = 0x0001
_FLOAT = 0x0003
_EXTENSIBLE = 0xFFFE
RAW_FORMATS = {"pcm_s16le": ("<i2", 2), "pcm_f32le": ("<f4", 4)}
SUPPORTED_FORMATS = ("wav", *RAW_FORMATS)


class AudioFormatError(ChannelError):
    """The audio can't be decoded or converted (an unsupported format, a malformed file)."""


class MissingExtra(ChannelError):
    """An optional dependency a channel needs is not installed."""


def require_numpy(extra: str) -> Any:
    """The numpy module, or :class:`MissingExtra` naming the extra that installs it."""
    try:
        import numpy
    except ImportError as exc:
        raise MissingExtra(f"the {extra} channel needs numpy: install argus-scout[{extra}]") from exc
    return numpy


def _np() -> Any:
    try:
        import numpy
    except ImportError as exc:
        raise MissingExtra("converting speech audio needs numpy: install argus-scout[snapcast] or [mask]") from exc
    return numpy


@dataclass(frozen=True)
class Pcm:
    """Decoded audio: float32 samples in ``[-1, 1]``, shape ``(frames, channels)``."""

    samples: np.ndarray
    sample_rate: int

    @property
    def channels(self) -> int:
        return int(self.samples.shape[1])

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


@dataclass(frozen=True)
class SampleFormat:
    """A target layout, as Snapcast writes it: ``rate:bits:channels`` (``48000:16:2``).

    Snapcast stores 24-bit samples in 4 bytes (the low three carry the value), so :attr:`sample_bytes` is 4 for
    both 24 and 32 bits.
    """

    rate: int
    bits: int
    channels: int

    @classmethod
    def parse(cls, text: str) -> SampleFormat:
        try:
            rate, bits, channels = (int(p) for p in str(text).split(":"))
        except ValueError:
            raise ValueError(f"{text!r} is not rate:bits:channels (e.g. 48000:16:2)") from None
        if not 8000 <= rate <= 192000:
            raise ValueError(f"sample rate {rate} is out of range (8000-192000)")
        if bits not in (16, 24, 32):
            raise ValueError(f"{bits}-bit samples are not supported (16, 24 or 32)")
        if not 1 <= channels <= 8:
            raise ValueError(f"{channels} channels is out of range (1-8)")
        return cls(rate, bits, channels)

    @property
    def sample_bytes(self) -> int:
        return 2 if self.bits == 16 else 4

    @property
    def bytes_per_second(self) -> int:
        return self.rate * self.channels * self.sample_bytes

    def __str__(self) -> str:
        return f"{self.rate}:{self.bits}:{self.channels}"


# --- decoding ------------------------------------------------------------------------------------------------


def _chunks(data: bytes) -> dict[bytes, bytes]:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioFormatError("not a WAV file (no RIFF/WAVE header)")
    found: dict[bytes, bytes] = {}
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos : pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = data[pos + 8 : pos + 8 + size]
        found.setdefault(cid, body)
        if cid == b"data":
            break  # streamed WAVs may claim a huge size; whatever follows is all data
        pos += 8 + size + (size & 1)
    if b"fmt " not in found or b"data" not in found:
        raise AudioFormatError("WAV file without a fmt and a data chunk")
    return found


def _decode_wav(data: bytes) -> Pcm:
    np = _np()
    chunks = _chunks(data)
    fmt = chunks[b"fmt "]
    if len(fmt) < 16:
        raise AudioFormatError("WAV fmt chunk is too short")
    tag, channels, rate, _, align, bits = struct.unpack_from("<HHIIHH", fmt)
    if tag == _EXTENSIBLE and len(fmt) >= 26:
        tag = struct.unpack_from("<H", fmt, 24)[0]
    if not channels or not rate or not bits:
        raise AudioFormatError("WAV header with no channels, rate or sample size")
    width = bits // 8 if bits % 8 == 0 else (align // channels if channels else 0)
    raw = chunks[b"data"]
    raw = raw[: len(raw) - len(raw) % (width * channels)] if width else b""
    if tag == _PCM and width in (1, 2, 4):
        dtype = {1: "u1", 2: "<i2", 4: "<i4"}[width]
        ints = np.frombuffer(raw, dtype=dtype).astype(np.float32)
        samples = (ints - 128.0) / 128.0 if width == 1 else ints / float(2 ** (8 * width - 1))
    elif tag == _PCM and width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        samples = ints.astype(np.float32) / float(2**23)
    elif tag == _FLOAT and width in (4, 8):
        samples = np.frombuffer(raw, dtype="<f4" if width == 4 else "<f8").astype(np.float32)
    else:
        raise AudioFormatError(f"WAV encoding {tag:#06x} with {bits}-bit samples is not supported")
    return Pcm(samples.reshape(-1, channels).astype(np.float32, copy=False), rate)


def decode(audio: SpeechAudio) -> Pcm:
    """``audio`` as :class:`Pcm`, or :class:`AudioFormatError` for a format scout can't read."""
    np = _np()
    fmt = (audio.format or "").lower()
    if fmt == "wav":
        return _decode_wav(audio.data)
    if fmt in RAW_FORMATS:
        dtype, width = RAW_FORMATS[fmt]
        if audio.channels < 1 or audio.sample_rate < 1:
            raise AudioFormatError(f"{fmt} needs a sample_rate and channels")
        frame = width * audio.channels
        raw = audio.data[: len(audio.data) - len(audio.data) % frame]
        samples = np.frombuffer(raw, dtype=dtype).astype(np.float32)
        if fmt == "pcm_s16le":
            samples = samples / 32768.0
        return Pcm(samples.reshape(-1, audio.channels), audio.sample_rate)
    raise AudioFormatError(
        f"audio format {audio.format!r} is not supported (send {', '.join(SUPPORTED_FORMATS)}); "
        "ask the synthesiser for WAV or raw PCM"
    )


# --- conversion ----------------------------------------------------------------------------------------------


def remix(pcm: Pcm, channels: int) -> Pcm:
    """``pcm`` with ``channels`` channels: mono is copied to every channel; anything else is averaged to mono
    first (so a stereo voice reaches a mono sink whole)."""
    if pcm.channels == channels:
        return pcm
    np = _np()
    mono = pcm.samples if pcm.channels == 1 else pcm.samples.mean(axis=1, keepdims=True)
    return Pcm(np.repeat(mono, channels, axis=1).astype(np.float32, copy=False), pcm.sample_rate)


def resample(pcm: Pcm, rate: int) -> Pcm:
    """``pcm`` at ``rate`` by linear interpolation."""
    if pcm.sample_rate == rate or pcm.frames == 0:
        return Pcm(pcm.samples, rate) if pcm.frames == 0 else pcm
    np = _np()
    frames = max(1, round(pcm.frames * rate / pcm.sample_rate))
    src = np.arange(pcm.frames, dtype=np.float64)
    dst = np.arange(frames, dtype=np.float64) * (pcm.sample_rate / rate)
    out = np.empty((frames, pcm.channels), dtype=np.float32)
    for ch in range(pcm.channels):
        out[:, ch] = np.interp(dst, src, pcm.samples[:, ch])
    return Pcm(out, rate)


def silence(fmt: SampleFormat, ms: int) -> bytes:
    """``ms`` milliseconds of silence in ``fmt``."""
    return bytes(fmt.rate * ms // 1000 * fmt.channels * fmt.sample_bytes)


def encode(pcm: Pcm, fmt: SampleFormat) -> bytes:
    """``pcm`` converted to ``fmt`` (channels, then rate) and packed as little-endian integers."""
    np = _np()
    pcm = resample(remix(pcm, fmt.channels), fmt.rate)
    clipped = np.clip(pcm.samples, -1.0, 1.0).astype(np.float64)
    scale = {16: 32767, 24: 2**23 - 1, 32: 2**31 - 1}[fmt.bits]
    dtype = "<i2" if fmt.bits == 16 else "<i4"
    return np.round(clipped * scale).astype(dtype).tobytes()


def to_s16le(pcm: Pcm) -> bytes:
    """``pcm`` as raw little-endian 16-bit samples, at its own rate and channel count."""
    return encode(pcm, SampleFormat(pcm.sample_rate, 16, pcm.channels))


def to_wav(pcm: Pcm, bits: int = 16) -> bytes:
    """``pcm`` as a 16- or 32-bit integer PCM WAV file (stdlib ``wave``)."""
    if bits not in (16, 32):
        raise AudioFormatError("WAV output is 16 or 32 bit")
    body = encode(pcm, SampleFormat(pcm.sample_rate, bits, pcm.channels))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(pcm.channels)
        w.setsampwidth(bits // 8)
        w.setframerate(pcm.sample_rate)
        w.writeframes(body)
    return out.getvalue()


__all__ = [
    "SUPPORTED_FORMATS",
    "AudioFormatError",
    "MissingExtra",
    "Pcm",
    "SampleFormat",
    "decode",
    "encode",
    "remix",
    "require_numpy",
    "resample",
    "silence",
    "to_s16le",
    "to_wav",
]
