"""The dispatcher: one message out to every channel, concurrently, each on its own clock.

:meth:`Dispatcher.deliver` takes a :class:`~argus_scout.channels.base.Delivery` and:

1. **Picks the channels.** A channel that is ``enabled: false``, whose ``min_severity`` the message is below,
   or whose ``kinds`` don't include the message's kind is skipped. So is a channel the message was already
   sent on: the store's ``deliveries`` table holds one successful row per (message, channel), so re-delivery
   is idempotent, across restarts too (``force=True`` sends again anyway).
2. **Sends text** to every non-voice channel at once, each under its own ``timeout``.
3. **Speaks**, if any voice channel is left: the heartbeat gates decide whether this message may be spoken
   now (quiet hours, rest hours, the voice policy; see :class:`HeartbeatGates`). If so, the speech is
   synthesised **once** through the :class:`~argus_scout.channels.base.Synthesiser` and the same audio goes
   to every voice channel. A gate that says no holds voice back and leaves text alone.

**Isolation.** A channel that raises, returns ``ok=False`` or runs past its timeout is logged, counted
(:attr:`Dispatcher.stats`) and recorded as a failed delivery; the others are unaffected, and a failed
channel is tried again on the next delivery of the same message. Nothing a channel does can stop the
dispatcher.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import structlog

from argus_scout.budget import DailyBudget
from argus_scout.channels.base import (
    Delivery,
    DeliveryResult,
    NullSynthesiser,
    SpeechAudio,
    Synthesiser,
)
from argus_scout.channels.registry import BoundChannel
from argus_scout.heartbeat import HeartbeatPolicy, PolicyFile, broadcast_gate, voice_gate
from argus_scout.scheduler import Clock, utc_now
from argus_scout.severity import rank
from argus_scout.store.base import Store

log = structlog.get_logger(__name__)

SYNTHESIS_TIMEOUT_S = 60.0


class Gates(Protocol):
    """Decides whether a message may be spoken now. :class:`HeartbeatGates` is the real one."""

    def voice(self, message: Delivery, now: datetime) -> tuple[bool, str]:
        """``(allowed, rule that decided)``."""
        ...


class HeartbeatGates:
    """The heartbeat policy's gates, applied to a delivery. Nothing here re-implements them.

    - **Rest hours**: nothing is spoken.
    - **Quiet hours and ack/expiry**: :func:`~argus_scout.heartbeat.broadcast_gate` (critical and
      ``quiet_exempt`` still get through).
    - **An inbox item**: :func:`~argus_scout.heartbeat.voice_gate` (its source's routing, ``speakable``,
      ``voice.min_severity``).
    - **A report**: it is scheduled, not an interruption, so ``voice.min_severity`` doesn't apply; the policy's
      routing for source ``scout`` does (``sources.scout.channel: text`` keeps reports off the speakers).

    The policy file is re-read through :class:`~argus_scout.heartbeat.PolicyFile`, so an edit applies on the next
    delivery and a bad edit means the defaults.
    """

    def __init__(self, policy_file: PolicyFile | None = None, *, tz: ZoneInfo | None = None) -> None:
        self.policy_file = policy_file or PolicyFile(None)
        self.tz = tz or ZoneInfo("UTC")

    def policy(self) -> HeartbeatPolicy:
        try:
            return self.policy_file.get()
        except Exception as exc:  # noqa: BLE001 — PolicyFile already falls back; this is the last line
            log.warning("dispatch.policy_unavailable", error=str(exc))
            return HeartbeatPolicy(path=self.policy_file.path, file_error=str(exc))

    def voice(self, message: Delivery, now: datetime) -> tuple[bool, str]:
        if not message.speakable:
            return False, "delivery.speakable=empty"
        policy = self.policy()
        local = now.astimezone(self.tz)
        if policy.rest_hours.contains(local):
            return False, "rest_hours"
        event = message.as_event()
        ok, rule = broadcast_gate(event, policy, now=now, quiet=policy.quiet_hours.contains(local))
        if not ok:
            return False, rule
        if message.type == "report":
            source_rule, key = policy.source_rule(event.source)
            if source_rule.channel != "voice":
                return False, f"{key}.channel={source_rule.channel}"
            return True, f"{rule}; {key}.channel=voice"
        return voice_gate(event, policy)


class OpenGates:
    """Always allows voice (tests, embedding)."""

    def voice(self, message: Delivery, now: datetime) -> tuple[bool, str]:
        return bool(message.speakable), "open" if message.speakable else "delivery.speakable=empty"


@dataclass
class DispatchReport:
    """What one :meth:`Dispatcher.deliver` did, channel by channel."""

    key: str
    results: list[DeliveryResult] = field(default_factory=list)
    voice_rule: str = ""
    """Why voice was or wasn't allowed (empty when no voice channel was in play)."""
    synthesised: bool = False

    def get(self, channel: str) -> DeliveryResult | None:
        return next((r for r in self.results if r.channel == channel), None)

    @property
    def sent(self) -> list[str]:
        return [r.channel for r in self.results if r.status == "sent"]

    @property
    def failed(self) -> list[str]:
        return [r.channel for r in self.results if r.status == "failed"]

    @property
    def ok(self) -> bool:
        return not self.failed


class Dispatcher:
    """Fans a :class:`Delivery` out to channels. See the module docs for the rules.

    ``store`` (optional) records every attempt and makes delivery idempotent; ``budget`` (optional) caps speech:
    one ``tts`` unit per synthesis and one ``speak`` unit per message spoken.
    """

    def __init__(
        self,
        channels: Sequence[BoundChannel],
        *,
        synthesiser: Synthesiser | None = None,
        gates: Gates | None = None,
        store: Store | None = None,
        budget: DailyBudget | None = None,
        clock: Clock = utc_now,
        synthesis_timeout: float = SYNTHESIS_TIMEOUT_S,
    ) -> None:
        self.channels = list(channels)
        self.synthesiser: Synthesiser = synthesiser or NullSynthesiser()
        self.gates: Gates = gates or HeartbeatGates()
        self.store = store
        self.budget = budget
        self.clock = clock
        self.synthesis_timeout = synthesis_timeout
        self.stats: Counter[tuple[str, str]] = Counter()
        """``(channel, status)`` -> count, since start."""
        self.syntheses = 0
        self._inflight: set[tuple[str, str]] = set()

    @property
    def voice_channels(self) -> list[BoundChannel]:
        return [c for c in self.channels if c.voice and c.enabled]

    # -- selection -------------------------------------------------------------------
    def _skip(self, bound: BoundChannel, message: Delivery, sent: set[str], force: bool) -> DeliveryResult | None:
        if not bound.enabled:
            return DeliveryResult(ok=True, channel=bound.name, status="disabled", detail="enabled: false")
        if rank(message.severity) < rank(bound.min_severity):
            detail = f"{message.severity.value} is below min_severity={bound.min_severity.value}"
            return DeliveryResult(ok=True, channel=bound.name, status="filtered", detail=detail)
        if bound.kinds and message.kind not in bound.kinds:
            detail = f"kind {message.kind!r} is not in kinds={bound.kinds}"
            return DeliveryResult(ok=True, channel=bound.name, status="filtered", detail=detail)
        if not force and (bound.name in sent or (message.key, bound.name) in self._inflight):
            return DeliveryResult(ok=True, channel=bound.name, status="duplicate", detail="already delivered")
        return None

    def _already_sent(self, key: str) -> set[str]:
        if self.store is None:
            return set()
        try:
            return self.store.delivered_channels(key)
        except Exception:  # noqa: BLE001 — a store hiccup must not stop delivery
            log.exception("dispatch.store_read_failed", key=key)
            return set()

    # -- running ---------------------------------------------------------------------
    def _record(self, bound: BoundChannel, message: Delivery, result: DeliveryResult) -> None:
        self.stats[(bound.name, result.status or "")] += 1
        if result.status == "skipped":
            log.info("dispatch.skipped", channel=bound.name, key=message.key, detail=result.detail)
            return  # not delivered, not failed: nothing to record, nothing to retry
        if result.status == "sent":
            log.info("dispatch.sent", channel=bound.name, key=message.key, detail=result.detail)
        else:
            log.warning("dispatch.failed", channel=bound.name, key=message.key, error=result.error)
        if self.store is None:
            return
        event = message.event
        try:
            self.store.record_delivery(
                message.key,
                bound.name,
                ok=result.status == "sent",
                error=result.error,
                permanent=result.permanent and result.status != "sent",
                at=self.clock(),
                report_id=message.report_id,
                event_source=event.source if event else None,
                event_id=event.event_id if event else None,
            )
        except Exception:  # noqa: BLE001
            log.exception("dispatch.record_failed", channel=bound.name, key=message.key)

    async def _run(
        self, bound: BoundChannel, message: Delivery, call: Callable[[], Awaitable[DeliveryResult]]
    ) -> DeliveryResult:
        try:
            result = await asyncio.wait_for(call(), timeout=bound.timeout)
            if not isinstance(result, DeliveryResult):
                result = DeliveryResult(ok=False, error=f"the channel returned {type(result).__name__}")
        except TimeoutError:
            result = DeliveryResult(ok=False, error=f"timed out after {bound.timeout:g}s")
        except Exception as exc:  # noqa: BLE001 — a failing channel never affects the others
            result = DeliveryResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
                permanent=bool(getattr(exc, "permanent", False)),
            )
        skipped = result.ok and result.status == "skipped"  # the channel declined (device not ready): not a failure
        result = replace(result, channel=bound.name, status="skipped" if skipped else "sent" if result.ok else "failed")
        if not result.ok and not result.error:
            result = replace(result, error="the channel reported a failure")
        self._record(bound, message, result)
        return result

    async def _synthesise(self, message: Delivery) -> SpeechAudio | None:
        self.syntheses += 1
        return await asyncio.wait_for(
            self.synthesiser.synthesise(message.speakable, message), timeout=self.synthesis_timeout
        )

    async def _speak(
        self, voice: list[BoundChannel], message: Delivery, report: DispatchReport
    ) -> list[DeliveryResult]:
        def held(why: str) -> list[DeliveryResult]:
            for bound in voice:
                self.stats[(bound.name, "suppressed")] += 1
            log.info("dispatch.voice_suppressed", key=message.key, rule=why)
            return [DeliveryResult(ok=True, channel=b.name, status="suppressed", detail=why) for b in voice]

        allowed, rule = self.gates.voice(message, self.clock())
        report.voice_rule = rule
        if not allowed:
            return held(rule)
        if self.budget is not None and not (self.budget.available("speak") and self.budget.try_consume("tts")):
            return held("budget: daily speech limit reached")
        try:
            audio = await self._synthesise(message)
        except Exception as exc:  # noqa: BLE001 — a synthesis failure fails voice, not text
            error = "speech synthesis timed out" if isinstance(exc, TimeoutError) else f"speech synthesis failed: {exc}"
            permanent = bool(getattr(exc, "permanent", False))
            log.warning("dispatch.synthesis_failed", key=message.key, error=error, permanent=permanent)
            failures = []
            for bound in voice:
                failure = DeliveryResult(
                    ok=False, error=error, channel=bound.name, status="failed", permanent=permanent
                )
                self._record(bound, message, failure)
                failures.append(failure)
            return failures
        if audio is None:
            return held("no speech synthesiser")
        report.synthesised = True
        results = list(
            await asyncio.gather(*(self._run(b, message, lambda b=b: b.channel.play(audio, message)) for b in voice))  # type: ignore[attr-defined]
        )
        if self.budget is not None and any(r.status == "sent" for r in results):
            self.budget.try_consume("speak")
        return results

    async def deliver(
        self, message: Delivery, *, force: bool = False, only: Iterable[str] | None = None
    ) -> DispatchReport:
        """Send ``message`` to every channel that should get it (only those named in ``only``, if given)."""
        report = DispatchReport(key=message.key)
        wanted = set(only) if only is not None else None
        sent = self._already_sent(message.key)
        text: list[BoundChannel] = []
        voice: list[BoundChannel] = []
        skipped: list[DeliveryResult] = []
        for bound in self.channels:
            if wanted is not None and bound.name not in wanted:
                continue
            skip = self._skip(bound, message, sent, force)
            if skip is not None:
                self.stats[(bound.name, skip.status or "")] += 1
                skipped.append(skip)
                continue
            (voice if bound.voice else text).append(bound)
        claimed = {(message.key, b.name) for b in text + voice}
        self._inflight |= claimed
        try:
            calls: list[Awaitable[DeliveryResult | list[DeliveryResult]]] = [
                self._run(b, message, lambda b=b: b.channel.deliver(message)) for b in text
            ]
            if voice:
                calls.append(self._speak(voice, message, report))
            outcomes = await asyncio.gather(*calls)
        finally:
            self._inflight -= claimed
        for outcome in outcomes:
            report.results.extend(outcome if isinstance(outcome, list) else [outcome])
        report.results.extend(skipped)
        order = {b.name: i for i, b in enumerate(self.channels)}
        report.results.sort(key=lambda r: order.get(r.channel, len(order)))
        log.info("dispatch.done", key=message.key, sent=report.sent, failed=report.failed)
        return report


__all__ = ["DispatchReport", "Dispatcher", "Gates", "HeartbeatGates", "OpenGates"]
