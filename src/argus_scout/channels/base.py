"""The channel plugin interface: where reports and status items go.

A **channel kind** is a class with a ``kind`` (the ``kind:`` a ``channels:`` entry in ``scout.yaml``
names) and a ``config_model``: a pydantic model, normally a subclass of
:class:`~argus_scout.config.ChannelSettings`, that validates one ``channels:`` entry of that kind. Scout
constructs it once per entry, with the validated config, and the instance is a :class:`Channel`:

- ``async deliver(message: Delivery) -> DeliveryResult``: send a text message (a file, a webhook, a chat).

A channel that speaks is a :class:`VoiceChannel`, which also has

- ``async play(audio: SpeechAudio, message: Delivery) -> DeliveryResult``: play speech that scout has
  **already synthesised**. A voice channel never synthesises: the dispatcher synthesises once per message
  through a :class:`Synthesiser` (argus-echo) and hands the same audio to every voice channel, so three
  speakers cost one synthesis and say exactly the same thing.

A channel fails by raising (any exception; :class:`ChannelError` for an expected failure) or by returning
a result with ``ok=False``. Either way the dispatcher records it and the other channels carry on. A failure
that retrying can't fix is ``ChannelError(..., permanent=True)`` (or ``DeliveryResult(permanent=True)``): the
redelivery sweep then leaves it alone.

**Delivery only.** A channel sends a message to the place its config names. It never triggers an action on
a monitored host, and outbound HTTP goes only to the channel's configured URL through ``argus_cortex.net``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from argus_scout.events import StatusEvent
from argus_scout.severity import Severity, coerce_severity

if TYPE_CHECKING:
    from argus_scout.store.base import Report

REPORT_SOURCE = "scout"
"""The source a report counts as when the heartbeat gates judge it (``sources.scout`` in the policy)."""

EVENT_KIND = "event"
"""The ``kind`` of a delivery built from an inbox item (reports carry their own kind: morning, status ...)."""


_FENCE = re.compile(r"```.*?```", re.S)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_LINE_MARK = re.compile(r"^\s*(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)", re.M)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~|`)(?=\S)(.+?)(?<=\S)\1")


def plain_speech(text: str) -> str:
    """``text`` with markdown stripped, for speech: code blocks dropped, links reduced to their text, headings,
    quotes, list markers and emphasis removed, whitespace collapsed. Deliberately simple."""
    text = _FENCE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _LINE_MARK.sub("", text)
    for _ in range(2):  # nested emphasis
        text = _EMPHASIS.sub(r"\2", text)
    text = text.replace("`", "")
    return " ".join(text.split())


class ChannelError(Exception):
    """A channel could not deliver (an expected failure: refused, unreachable, misconfigured).

    ``permanent=True`` says trying the same message again can't help (the receiver refused it: a 4xx other than
    408 or 429). The redelivery sweep never retries a permanent failure; anything else it retries.
    """

    def __init__(self, message: str = "", *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class SynthesisError(ChannelError):
    """Speech could not be synthesised. The dispatcher records it as a failure on every voice channel in play
    (permanent if this is), and text channels are unaffected."""


@dataclass(frozen=True)
class Delivery:
    """One message for the channels: a stored report or an inbox status item.

    ``key`` identifies the message for idempotency: ``report:<id>``, or for an event
    ``event:<source>/<event_id>:<content hash>:<announce count>``, so a changed or re-announced event
    is a new message while a repeat of the same announcement is not.
    """

    type: Literal["report", "event"]
    kind: str
    key: str
    text: str
    speakable: str
    """What to say aloud; empty when the message is not to be spoken."""
    severity: Severity
    generated_at: datetime
    title: str = ""
    report_id: int | None = None
    event: StatusEvent | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_report(cls, report: Report) -> Delivery:
        """A delivery for a stored report. ``meta`` may carry ``severity``, ``title`` and ``speakable``
        (text to say instead of the report text, or ``false`` for a report that is never spoken). Otherwise the
        spoken form is the report text with its markdown stripped (:func:`plain_speech`)."""
        meta = dict(report.meta or {})
        spoken = meta.get("speakable", True)
        speakable = spoken if isinstance(spoken, str) else (plain_speech(report.text) if spoken is not False else "")
        return cls(
            type="report",
            kind=report.kind,
            key=f"report:{report.id}",
            text=report.text,
            speakable=speakable.strip(),
            severity=coerce_severity(meta.get("severity")),
            generated_at=report.generated_at,
            title=str(meta.get("title") or f"{report.kind} report"),
            report_id=report.id,
            meta=meta,
        )

    @classmethod
    def from_event(cls, event: StatusEvent) -> Delivery:
        """A delivery for one inbox status item (for a group, pass its latest event)."""
        digest = hashlib.sha256(event.fingerprint.encode()).hexdigest()[:12]
        text = event.summary + (f"\n\n{event.body_md.strip()}" if event.body_md.strip() else "")
        return cls(
            type="event",
            kind=EVENT_KIND,
            key=f"event:{event.source}/{event.event_id}:{digest}:{event.broadcast_count}",
            text=text,
            speakable=event.summary if event.speakable else "",
            severity=event.severity,
            generated_at=event.generated_at,
            title=event.summary,
            event=event,
        )

    def as_event(self) -> StatusEvent:
        """The status event the heartbeat gates judge: the item itself, or a stand-in for a report."""
        if self.event is not None:
            return self.event
        return StatusEvent(
            event_id=self.key,
            source=REPORT_SOURCE,
            severity=self.severity,
            generated_at=self.generated_at,
            summary=self.title or self.kind,
            speakable=bool(self.speakable),
            quiet_exempt=bool(self.meta.get("quiet_exempt", False)),
        )


@dataclass(frozen=True)
class SpeechAudio:
    """Synthesised speech, ready to play. ``data`` is the encoded audio in ``format``."""

    data: bytes
    format: str = "wav"
    """``wav``, ``mp3``, ``ogg``, or ``pcm_s16le`` (raw little-endian 16-bit samples)."""
    sample_rate: int = 22050
    channels: int = 1
    text: str = ""
    """What was said."""
    voice: str | None = None


DeliveryStatus = Literal["sent", "failed", "duplicate", "filtered", "disabled", "suppressed"]


@dataclass(frozen=True)
class DeliveryResult:
    """What happened to one message on one channel.

    A channel returns ``DeliveryResult(ok=True, detail=...)`` (or ``ok=False, error=...``); the dispatcher
    fills in ``channel`` and ``status`` and adds results for channels it skipped:

    - ``sent`` / ``failed``: the channel was tried (recorded in the store)
    - ``duplicate``: already sent on this channel, so not sent again
    - ``filtered``: below the channel's ``min_severity``, or not one of its ``kinds``
    - ``disabled``: ``enabled: false``
    - ``suppressed``: a voice channel the voice gate (quiet hours, voice policy) or a missing synthesiser held back
    """

    ok: bool
    detail: str = ""
    error: str | None = None
    channel: str = ""
    status: DeliveryStatus | None = None
    permanent: bool = False
    """A failure retrying can't fix (see :class:`ChannelError`); the redelivery sweep leaves it alone."""


@runtime_checkable
class Channel(Protocol):
    kind: str
    config_model: type[BaseModel]

    async def deliver(self, message: Delivery) -> DeliveryResult: ...


@runtime_checkable
class VoiceChannel(Channel, Protocol):
    async def play(self, audio: SpeechAudio, message: Delivery) -> DeliveryResult: ...


@runtime_checkable
class Synthesiser(Protocol):
    """Text to speech. argus-echo implements it; scout calls it at most once per delivery."""

    async def synthesise(self, text: str, message: Delivery) -> SpeechAudio | None:
        """Speech for ``text``, or None when there is no engine to ask (voice is then skipped, unrecorded).

        An engine that is configured but fails (down, busy, refused) raises, preferably :class:`SynthesisError`:
        the dispatcher then records a failure on every voice channel, which the redelivery sweep may retry."""
        ...


class NullSynthesiser:
    """No speech engine: synthesises nothing, so voice channels are skipped and text still goes out."""

    async def synthesise(self, text: str, message: Delivery) -> SpeechAudio | None:
        return None


__all__ = [
    "EVENT_KIND",
    "REPORT_SOURCE",
    "Channel",
    "ChannelError",
    "Delivery",
    "DeliveryResult",
    "DeliveryStatus",
    "NullSynthesiser",
    "SpeechAudio",
    "SynthesisError",
    "Synthesiser",
    "VoiceChannel",
    "plain_speech",
]
