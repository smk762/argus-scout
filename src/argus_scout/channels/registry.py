"""Which channel kinds exist, and building one channel per ``channels:`` entry in the config.

Kinds come from the ``argus_scout.channels`` entry-point group: the entry point's *name* is the kind and its
*value* is the channel class (anything callable with one validated config that has ``kind`` and
``config_model``). A third-party package adds a kind with::

    [project.entry-points."argus_scout.channels"]
    matrix = "my_package.scout:MatrixChannel"

and a ``channels:`` entry of ``kind: matrix`` is validated by that class's ``config_model`` and handed to
``MatrixChannel(config)``. The built-in ``text`` and ``webhook`` are registered the same way, and are also
known without entry points. A plugin can't take over a built-in kind.

**Forgiving, per channel**, exactly like sources: a plugin that fails to import, an unknown kind, an entry its
plugin rejects or a constructor that raises is an error for that one entry; the other channels still load.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from argus_scout.channels.base import Channel, VoiceChannel
from argus_scout.channels.text import TextChannel
from argus_scout.channels.webhook import WebhookChannel
from argus_scout.config import PluginChannel, ScoutConfig
from argus_scout.severity import Severity

log = structlog.get_logger(__name__)

ENTRY_POINT_GROUP = "argus_scout.channels"
BUILTINS: dict[str, Any] = {"text": TextChannel, "webhook": WebhookChannel}


@dataclass
class Registry:
    """The channel kinds that loaded (``kind -> class``), and the plugins that didn't (``kind -> reason``)."""

    kinds: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def get(self, kind: str) -> Any | None:
        return self.kinds.get(kind)


@dataclass(frozen=True)
class BoundChannel:
    """One ``channels:`` entry: its validated config and the channel built from it."""

    channel: Channel
    config: Any

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def kind(self) -> str:
        return self.channel.kind

    @property
    def voice(self) -> bool:
        return isinstance(self.channel, VoiceChannel)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", True))

    @property
    def min_severity(self) -> Severity:
        return Severity(getattr(self.config, "min_severity", Severity.INFO.value))

    @property
    def kinds(self) -> list[str]:
        return list(getattr(self.config, "kinds", None) or [])

    @property
    def timeout(self) -> float:
        return float(getattr(self.config, "timeout", None) or 30.0)


@dataclass
class Channels:
    """Every channel that loaded, and an error per entry that didn't (``name -> reason``)."""

    channels: list[BoundChannel] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> BoundChannel | None:
        return next((c for c in self.channels if c.name == name), None)


def _check_plugin(kind: str, obj: Any) -> Any:
    if not callable(obj) or not isinstance(getattr(obj, "kind", None), str) or not hasattr(obj, "config_model"):
        raise TypeError("not a channel plugin: needs a kind, a config_model, and to be constructible from a config")
    if obj.kind != kind:
        raise TypeError(f"the entry point is named {kind!r} but the plugin's kind is {obj.kind!r}")
    if not (inspect.isclass(obj.config_model) and issubclass(obj.config_model, BaseModel)):
        raise TypeError("config_model must be a pydantic model class")
    if inspect.isclass(obj) and not callable(getattr(obj, "deliver", None)):
        raise TypeError("a channel must have async deliver(message)")
    return obj


def load_registry(eps: Iterable[EntryPoint] | None = None) -> Registry:
    """The built-in kinds plus every plugin in the ``argus_scout.channels`` entry-point group."""
    registry = Registry(kinds=dict(BUILTINS))
    for ep in entry_points(group=ENTRY_POINT_GROUP) if eps is None else eps:
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001 — any import failure disables that plugin only
            registry.errors[ep.name] = f"{ep.value} failed to load: {type(exc).__name__}: {exc}"
            log.warning("channel.plugin.failed", kind=ep.name, error=registry.errors[ep.name])
            continue
        if ep.name in BUILTINS:
            if obj is not BUILTINS[ep.name]:
                registry.errors[ep.name] = f"{ep.value} ignored: {ep.name!r} is a built-in kind"
                log.warning("channel.plugin.failed", kind=ep.name, error=registry.errors[ep.name])
            continue
        if ep.name in registry.kinds:
            registry.errors[ep.name] = f"{ep.value} ignored: kind {ep.name!r} is already provided"
            continue
        try:
            registry.kinds[ep.name] = _check_plugin(ep.name, obj)
        except Exception as exc:  # noqa: BLE001
            registry.errors[ep.name] = f"{ep.value}: {exc}"
            log.warning("channel.plugin.failed", kind=ep.name, error=registry.errors[ep.name])
    return registry


def _format(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = "unknown key" if err["type"] == "extra_forbidden" else err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def build_channels(config: ScoutConfig, registry: Registry | None = None) -> Channels:
    """Build a channel for every ``channels:`` entry. A bad entry is an error for that entry only."""
    registry = registry or load_registry()
    built = Channels()
    for entry in config.channels:
        plugin = registry.get(entry.kind)
        if plugin is None:
            reason = registry.errors.get(entry.kind)
            known = ", ".join(sorted(registry.kinds))
            built.errors[entry.name] = (
                f"channel kind {entry.kind!r} did not load: {reason}"
                if reason
                else f"unknown channel kind {entry.kind!r} (installed: {known})"
            )
            continue
        if isinstance(entry, PluginChannel):
            try:
                cfg = plugin.config_model.model_validate(entry.model_dump(), context={"base_dir": config.base_dir})
            except ValidationError as exc:
                built.errors[entry.name] = f"{entry.kind}: {_format(exc)}"
                continue
        else:
            cfg = entry
        try:
            channel = plugin(cfg)
        except Exception as exc:  # noqa: BLE001 — a constructor that fails disables that channel only
            built.errors[entry.name] = f"{entry.kind}: could not start: {type(exc).__name__}: {exc}"
            continue
        if not isinstance(channel, Channel):
            built.errors[entry.name] = f"{entry.kind}: the plugin did not build a channel (needs async deliver)"
            continue
        built.channels.append(BoundChannel(channel, cfg))
    for name, reason in built.errors.items():
        log.warning("channel.config.failed", channel=name, error=reason)
    return built


__all__ = ["BUILTINS", "ENTRY_POINT_GROUP", "BoundChannel", "Channels", "Registry", "build_channels", "load_registry"]
