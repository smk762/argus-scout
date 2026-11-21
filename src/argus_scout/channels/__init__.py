"""Delivery channels as plugins: where reports and status items go.

See :mod:`argus_scout.channels.base` for the interface, :mod:`argus_scout.channels.registry` for how kinds
are found (the ``argus_scout.channels`` entry-point group) and bound to config entries, and
:mod:`argus_scout.channels.dispatcher` for the fan-out. :func:`build_dispatcher` wires the three together
from a config, the way the server and the CLI both do.
"""

from __future__ import annotations

from argus_scout.budget import DailyBudget
from argus_scout.channels.base import (
    EVENT_KIND,
    REPORT_SOURCE,
    Channel,
    ChannelError,
    Delivery,
    DeliveryResult,
    NullSynthesiser,
    SpeechAudio,
    Synthesiser,
    VoiceChannel,
)
from argus_scout.channels.dispatcher import Dispatcher, DispatchReport, Gates, HeartbeatGates, OpenGates
from argus_scout.channels.registry import (
    ENTRY_POINT_GROUP,
    BoundChannel,
    Channels,
    Registry,
    build_channels,
    load_registry,
)
from argus_scout.config import ScoutConfig
from argus_scout.heartbeat import PolicyFile
from argus_scout.scheduler import Clock, utc_now
from argus_scout.store.base import Store


def build_dispatcher(
    config: ScoutConfig,
    *,
    store: Store | None = None,
    policy_file: PolicyFile | None = None,
    channels: Channels | None = None,
    synthesiser: Synthesiser | None = None,
    budget: DailyBudget | None = None,
    clock: Clock = utc_now,
) -> tuple[Channels, Dispatcher]:
    """The channels a config names and a :class:`Dispatcher` over the ones that loaded, gated by the
    heartbeat policy (``policy_file``, else the one ``files.heartbeat`` names) in the schedule's timezone."""
    channels = channels if channels is not None else build_channels(config)
    gates = HeartbeatGates(policy_file or PolicyFile(config.files.heartbeat), tz=config.schedule.tz)
    dispatcher = Dispatcher(
        channels.channels, synthesiser=synthesiser, gates=gates, store=store, budget=budget, clock=clock
    )
    return channels, dispatcher


__all__ = [
    "ENTRY_POINT_GROUP",
    "EVENT_KIND",
    "REPORT_SOURCE",
    "BoundChannel",
    "Channel",
    "ChannelError",
    "Channels",
    "Delivery",
    "DeliveryResult",
    "DispatchReport",
    "Dispatcher",
    "Gates",
    "HeartbeatGates",
    "NullSynthesiser",
    "OpenGates",
    "Registry",
    "SpeechAudio",
    "Synthesiser",
    "VoiceChannel",
    "build_channels",
    "build_dispatcher",
    "load_registry",
]
