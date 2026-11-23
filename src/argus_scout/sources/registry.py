"""Which source kinds exist, and binding each ``sources:`` entry in the config to one.

Kinds come from the ``argus_scout.sources`` entry-point group: the entry point's *name* is the kind
and its *value* is the plugin object (a class is instantiated with no arguments). A third-party
package adds a kind with::

    [project.entry-points."argus_scout.sources"]
    mqtt = "my_package.scout:MqttSource"

and a ``sources:`` entry of ``kind: mqtt`` is then validated by that plugin's ``config_model``.

The built-in ``webhook``, ``json_poll`` and ``suite_status`` are registered the same way, and are also known without
entry points (a source checkout that was never installed still works). A plugin can't take over a
built-in kind.

**Forgiving, per source.** A plugin that fails to import, an unknown kind or a ``sources:`` entry its
plugin rejects (a validation error, or *any* exception its model raises) is reported as an error for that
one entry; the other sources still load. Scout refuses to start only on a broken ``scout.yaml`` itself (see
:mod:`argus_scout.config`).
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from argus_scout.config import PluginSource, ScoutConfig, SuiteStatusSource
from argus_scout.sources.base import PollingSource, PushSource, Source
from argus_scout.sources.json_poll import plugin as json_poll_plugin
from argus_scout.sources.suite_status import plugin as suite_status_plugin
from argus_scout.sources.webhook import plugin as webhook_plugin

log = structlog.get_logger(__name__)

ENTRY_POINT_GROUP = "argus_scout.sources"
BUILTINS: dict[str, Source] = {
    "webhook": webhook_plugin,
    "json_poll": json_poll_plugin,
    "suite_status": suite_status_plugin,
}


@dataclass
class Registry:
    """The source kinds that loaded, and the plugins that didn't (``kind -> reason``)."""

    kinds: dict[str, Source] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def get(self, kind: str) -> Source | None:
        return self.kinds.get(kind)


@dataclass(frozen=True)
class BoundSource:
    """One ``sources:`` entry, validated by its plugin's model, with the plugin that handles it."""

    plugin: Source
    config: Any

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def kind(self) -> str:
        return self.plugin.kind

    @property
    def polls(self) -> bool:
        return isinstance(self.plugin, PollingSource)

    @property
    def pushes(self) -> bool:
        return isinstance(self.plugin, PushSource)


@dataclass
class Sources:
    """Every source that loaded, and an error per entry that didn't (``name -> reason``)."""

    sources: list[BoundSource] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> BoundSource | None:
        return next((s for s in self.sources if s.name == name), None)

    @property
    def polling(self) -> list[BoundSource]:
        return [s for s in self.sources if s.polls]

    @property
    def push(self) -> list[BoundSource]:
        return [s for s in self.sources if s.pushes]


def _check_plugin(kind: str, obj: Any) -> Source:
    if inspect.isclass(obj):
        obj = obj()
    if not isinstance(obj, Source):
        raise TypeError("not a source plugin: needs a kind and a config_model")
    if obj.kind != kind:
        raise TypeError(f"the entry point is named {kind!r} but the plugin's kind is {obj.kind!r}")
    if not (inspect.isclass(obj.config_model) and issubclass(obj.config_model, BaseModel)):
        raise TypeError("config_model must be a pydantic model class")
    if not isinstance(obj, PollingSource | PushSource):
        raise TypeError("a source must poll (async poll) or accept pushes (parse and authorize)")
    return obj


def load_registry(eps: Iterable[EntryPoint] | None = None) -> Registry:
    """The built-in kinds plus every plugin in the ``argus_scout.sources`` entry-point group."""
    registry = Registry(kinds=dict(BUILTINS))
    for ep in entry_points(group=ENTRY_POINT_GROUP) if eps is None else eps:
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001 — any import failure disables that plugin only
            registry.errors[ep.name] = f"{ep.value} failed to load: {type(exc).__name__}: {exc}"
            log.warning("source.plugin.failed", kind=ep.name, error=registry.errors[ep.name])
            continue
        if ep.name in BUILTINS:
            if obj is not BUILTINS[ep.name]:
                registry.errors[ep.name] = f"{ep.value} ignored: {ep.name!r} is a built-in kind"
                log.warning("source.plugin.failed", kind=ep.name, error=registry.errors[ep.name])
            continue
        if ep.name in registry.kinds:
            registry.errors[ep.name] = f"{ep.value} ignored: kind {ep.name!r} is already provided"
            continue
        try:
            registry.kinds[ep.name] = _check_plugin(ep.name, obj)
        except Exception as exc:  # noqa: BLE001
            registry.errors[ep.name] = f"{ep.value}: {exc}"
            log.warning("source.plugin.failed", kind=ep.name, error=registry.errors[ep.name])
    return registry


def _format(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = "unknown key" if err["type"] == "extra_forbidden" else err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def _check_bound(name: str, plugin: Source, cfg: Any) -> None:
    """What the rest of scout reads from a plugin's validated config without guarding: its ``name`` and, for a
    poller, a usable ``interval``. Checked here so a bad plugin model fails its own entry, not server startup."""
    if getattr(cfg, "name", None) != name:
        raise TypeError(f"the plugin's config model must keep name {name!r} (got {getattr(cfg, 'name', None)!r})")
    interval = getattr(cfg, "interval", None)
    usable = not isinstance(interval, bool) and isinstance(interval, int | float) and interval > 0
    if isinstance(plugin, PollingSource) and interval is not None and not usable:
        raise TypeError(f"interval must be a positive number of seconds, got {interval!r}")


def build_sources(config: ScoutConfig, registry: Registry | None = None) -> Sources:
    """Bind every ``sources:`` entry to its plugin. A bad entry is an error for that entry only."""
    registry = registry or load_registry()
    built = Sources()
    for entry in config.sources:
        plugin = registry.get(entry.kind)
        if plugin is None:
            reason = registry.errors.get(entry.kind)
            known = ", ".join(sorted(registry.kinds))
            built.errors[entry.name] = (
                f"source kind {entry.kind!r} did not load: {reason}"
                if reason
                else f"unknown source kind {entry.kind!r} (installed: {known})"
            )
            continue
        if isinstance(entry, PluginSource):
            try:
                cfg = plugin.config_model.model_validate(entry.model_dump(), context={"base_dir": config.base_dir})
                _check_bound(entry.name, plugin, cfg)
            except ValidationError as exc:
                built.errors[entry.name] = f"{entry.kind}: {_format(exc)}"
                continue
            except Exception as exc:  # noqa: BLE001 — a plugin's own validator may raise anything; it fails only itself
                built.errors[entry.name] = f"{entry.kind}: {type(exc).__name__}: {exc}"
                continue
        elif isinstance(entry, SuiteStatusSource) and entry.prometheus_url is None:
            # It asks the one Prometheus scout reads, unless the entry names its own.
            if config.services.prometheus_url is None:
                built.errors[entry.name] = f"{entry.kind}: no Prometheus to ask (set services.prometheus_url)"
                continue
            cfg = entry.model_copy(update={"prometheus_url": config.services.prometheus_url})
        else:
            cfg = entry
        built.sources.append(BoundSource(plugin, cfg))
    for name, reason in built.errors.items():
        log.warning("source.config.failed", source=name, error=reason)
    return built


__all__ = ["BUILTINS", "ENTRY_POINT_GROUP", "BoundSource", "Registry", "Sources", "build_sources", "load_registry"]
