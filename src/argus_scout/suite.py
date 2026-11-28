"""Suite status: each Argus service's health and version, read from Prometheus and nowhere else.

Scout never probes a service. Prometheus does, and scout asks it (PromQL, through
:class:`~argus_scout.prometheus.PrometheusClient`) about every series that carries an ``argus_peer`` target label,
which argus-iris's ``/directory/sd`` puts on each service it lists (``docs/suite.md`` has the scrape config):

* ``up``: whether the service's ``/metrics`` was scraped. ``up`` of the health-probe job is left out: it says the
  blackbox exporter answered, not the service.
* ``probe_success`` and ``probe_http_status_code``: a blackbox probe of the service's ``/health`` whose module
  succeeds only on ``200`` with ``"status": "ok"`` in the body. With argus-cortex's ``ServiceHealth`` that gives
  ``ok`` (success), ``degraded`` (``200``, no success), ``starting`` (``503``) and no answer (status ``0``).
* ``argus_build_info{service, version}``: the gauge every service serves through argus-cortex.

One reading (:func:`read_suite`) is twelve instant queries, run together. **If any of them can't be answered the
reading fails** (:class:`~argus_scout.prometheus.PrometheusError`): a partial view is never reported, so an outage
can't read as "all clear" or resolve an open event.

**No state of its own.** "Down for two minutes" and "changed version in the last hour" are asked of Prometheus
(``max_over_time``, ``last_over_time``), so a restart of scout neither forgets nor repeats anything.

A service is one ``(argus_peer, node or instance, container)``. Its :attr:`Service.state`:

==========  ==========================================================================================
``ok``      ``/health`` answers ``ok``
``up``      it answers (``/metrics`` is scraped, or the probe got an answer) but its health isn't probed
``degraded``  ``/health`` answers ``200`` without ``ok``, or an error status
``starting``  ``/health`` answers ``503``
``down``    nothing answers, or it is no longer a scrape target (for up to ``forget_after``)
==========  ==========================================================================================

:func:`suite_events` turns a reading into status events (down, degraded, a version change, version skew) with
stable ids, so the inbox dedups them, the heartbeat policy gates them and the poll job resolves them when a later
reading no longer has them. :func:`render_report` is the on-request summary. Both only describe: nothing here acts.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from argus_scout.config import SuiteStatusSource
from argus_scout.events import StatusEvent
from argus_scout.severity import Severity, rank

if TYPE_CHECKING:  # prometheus imports the sources package, which imports this module: types only here
    from argus_scout.prometheus import PrometheusClient, Sample

State = Literal["ok", "up", "degraded", "starting", "down"]
STATE_ORDER: tuple[State, ...] = ("down", "degraded", "starting", "up", "ok")

PEER_LABEL = "argus_peer"
"""The target label that makes a series a suite service's (argus-iris's ``/directory/sd`` sets it)."""
TEXT_MAX = 80
"""Label values come from Prometheus: each is cut to this before it reaches a summary or a report."""

Key = tuple[str, str, str]


def _duration(seconds: float) -> str:
    """Seconds as people write them: ``120`` -> ``2m``, ``5400`` -> ``1h30m``. (Not ``signals.format_duration``:
    that module imports the Prometheus client, which imports the sources package, which imports this one.)"""
    left, parts = max(1, round(seconds)), []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        n, left = divmod(left, size)
        if n:
            parts.append(f"{n}{unit}")
    return "".join(parts)


def _range(seconds: float) -> str:
    return f"{max(1, round(seconds))}s"


def queries(cfg: SuiteStatusSource) -> dict[str, str]:
    """The PromQL of one reading, by name. Fixed shapes: only the job name and the windows come from the config,
    and the config model holds the job name to a plain token, so nothing in a query is free text."""
    up = f'up{{{PEER_LABEL}!="",job!="{cfg.health_job}"}}'
    code = f'probe_http_status_code{{{PEER_LABEL}!=""}}'
    ok = f'probe_success{{{PEER_LABEL}!=""}}'
    build = f'argus_build_info{{{PEER_LABEL}!=""}}'
    down, degraded = _range(cfg.down_for), _range(cfg.degraded_for)
    seen, deploy = _range(cfg.forget_after), _range(cfg.deploy_window)
    return {
        "up_now": up,
        "up_window": f"max_over_time({up}[{down}])",
        "up_seen": f"max_over_time({up}[{seen}])",
        # Whether the target was already there a whole ``down_for`` ago: a target scraped once and failed (the
        # service still starting when Prometheus first asked) has not been down "for ``down_for``".
        "up_before": f"{up} offset {down}",
        "code_now": code,
        "code_window": f"max_over_time({code}[{down}])",
        "code_seen": f"max_over_time({code}[{seen}])",
        "code_before": f"{code} offset {down}",
        "ok_now": ok,
        # Only for a target that was already probed a whole window ago: one that just appeared is not "degraded
        # for ten minutes" after its first probe.
        "ok_window": f"max_over_time({ok}[{degraded}]) and ({ok} offset {degraded})",
        "build_now": build,
        "build_recent": f"last_over_time({build}[{deploy}])",
    }


def _text(value: str) -> str:
    """A label value fit to print: one line, no control characters, at most :data:`TEXT_MAX` characters."""
    cleaned = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    return cleaned[:TEXT_MAX]


def key_of(labels: Mapping[str, str]) -> Key | None:
    """Which service a series belongs to, or ``None`` without an ``argus_peer``."""
    peer = _text(labels.get(PEER_LABEL, ""))
    if not peer:
        return None
    return peer, _text(labels.get("node") or labels.get("instance") or "?"), _text(labels.get("container", ""))


_RELEASE = re.compile(r"^v?(\d+(?:\.\d+)*)(.*)$")
_PRE = re.compile(r"(?:^|[.\-_]?)(?:dev|rc|a|b|alpha|beta|pre)\d*", re.IGNORECASE)


def version_key(version: str) -> tuple[tuple[int, ...], int] | None:
    """An ordering for the versions the suite uses (``0.14.2``, ``0.2.3.dev38``, ``0.1.5.dev1+g9b71751``): the
    numeric release, then a development or pre-release sorts before the release of the same number. ``None`` for
    anything else: scout then says versions differ, never which one is behind."""
    match = _RELEASE.match(version.strip())
    if not match:
        return None
    release = tuple(int(part) for part in match.group(1).split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    rest = match.group(2).split("+", 1)[0]
    if rest and not _PRE.fullmatch(rest):
        return None
    return release, 0 if rest else 1


@dataclass(frozen=True)
class Service:
    """One suite service as Prometheus sees it now."""

    peer: str
    where: str
    """Its ``node`` label (argus-iris's name for the host), or the scrape ``instance``."""
    container: str = ""
    package: str = ""
    """The package it runs (``argus-lens``): ``argus_build_info``'s ``service``, or the ``argus_service`` label."""
    state: State = "up"
    detail: str = ""
    held: bool = False
    """The state has lasted its threshold (``down_for`` for down, ``degraded_for`` for degraded or starting), so
    it is worth a status event. Always false for ``ok`` and ``up``."""
    versions: tuple[str, ...] = ()
    """The version it reports now. More than one only while a restart is in flight."""
    previous: tuple[str, ...] = ()
    """Other versions it reported within ``deploy_window``: a deploy happened."""
    behind: str = ""
    """A newer version of the same peer running on another host, when this one is older."""

    @property
    def key(self) -> Key:
        return self.peer, self.where, self.container

    @property
    def name(self) -> str:
        return f"{self.peer} on {self.where}"

    @property
    def version(self) -> str:
        return ", ".join(self.versions)


@dataclass(frozen=True)
class Skew:
    """One peer running different versions on different hosts."""

    peer: str
    versions: tuple[tuple[str, tuple[str, ...]], ...]
    """``(version, the hosts running it)``, oldest version first where the versions can be ordered."""

    @property
    def text(self) -> str:
        return ", ".join(f"{version} on {' and '.join(hosts)}" for version, hosts in self.versions)


@dataclass(frozen=True)
class SuiteStatus:
    """One reading of the suite."""

    at: datetime
    services: tuple[Service, ...] = ()
    skews: tuple[Skew, ...] = ()
    names: Mapping[Key, str] = field(default_factory=dict)
    """What to call each service: ``peer on host``, with the container added only where two share that."""

    def named(self, service: Service) -> str:
        return self.names.get(service.key, service.name)

    def in_state(self, *states: State) -> list[Service]:
        return [s for s in self.services if s.state in states]

    @property
    def changed(self) -> list[Service]:
        return [s for s in self.services if s.versions and s.previous]

    @property
    def behind(self) -> list[Service]:
        return [s for s in self.services if s.behind]


def _by_key(samples: Iterable[Sample]) -> dict[Key, list[Sample]]:
    found: dict[Key, list[Sample]] = {}
    for sample in samples:
        key = key_of(sample.labels)
        if key is not None:
            found.setdefault(key, []).append(sample)
    return found


def _worst(samples: dict[Key, list[Sample]], key: Key) -> float | None:
    """The highest value among a service's series (two scrape jobs may both carry it), ``None`` without any."""
    values = [s.value for s in samples.get(key, ()) if s.value == s.value]  # NaN never counts
    return max(values) if values else None


def _versions(samples: dict[Key, list[Sample]], key: Key) -> tuple[str, ...]:
    return tuple(sorted({_text(s.labels["version"]) for s in samples.get(key, ()) if s.labels.get("version")}))


def _label(samples: Iterable[dict[Key, list[Sample]]], key: Key, *names: str) -> str:
    for group in samples:
        for sample in group.get(key, ()):
            for name in names:
                if sample.labels.get(name):
                    return _text(sample.labels[name])
    return ""


def _state(key: Key, r: Mapping[str, dict[Key, list[Sample]]]) -> tuple[State, str, bool]:
    code, up = _worst(r["code_now"], key), _worst(r["up_now"], key)
    answered_in_window = (_worst(r["code_window"], key) or 0) > 0 or (_worst(r["up_window"], key) or 0) > 0
    if code is None and up is None:
        return "down", "no longer a scrape target", not answered_in_window
    if not ((code or 0) > 0 or (up or 0) > 0):
        watched = key in r["up_before"] or key in r["code_before"]
        return "down", "no answer", watched and not answered_in_window
    if code is None or code <= 0:
        return "up", "health not probed" if code is None else "the health probe got no answer", False
    if (_worst(r["ok_now"], key) or 0) > 0:
        return "ok", "", False
    never_ok = _worst(r["ok_window"], key) == 0
    if round(code) == 503:
        return "starting", "/health answers 503", never_ok
    if round(code) == 200:
        return "degraded", "/health does not answer ok", never_ok
    return "degraded", f"/health answers HTTP {round(code)}", never_ok


def _skews(services: list[Service]) -> tuple[list[Skew], dict[Key, str]]:
    """Peers that run more than one version, and for each service on an older one, the newest it is behind."""
    by_peer: dict[str, dict[str, list[str]]] = {}
    for service in services:
        if len(service.versions) == 1 and service.state != "down":
            by_peer.setdefault(service.peer, {}).setdefault(service.versions[0], []).append(service.where)
    skews: list[Skew] = []
    behind: dict[Key, str] = {}
    for peer, versions in sorted(by_peer.items()):
        if len(versions) < 2:
            continue
        keys = {v: version_key(v) for v in versions}
        comparable = all(k is not None for k in keys.values())
        ordered = sorted(versions, key=lambda v: (keys[v], v)) if comparable else sorted(versions)
        skews.append(Skew(peer, tuple((v, tuple(sorted(set(versions[v])))) for v in ordered)))
        if not comparable:
            continue
        newest = ordered[-1]
        for service in services:
            version = service.versions[0] if len(service.versions) == 1 else ""
            if service.peer == peer and version in keys and keys[version] < keys[newest]:  # type: ignore[operator]
                behind[service.key] = newest
    return skews, behind


def build_status(results: Mapping[str, list[Sample]], cfg: SuiteStatusSource, at: datetime) -> SuiteStatus:
    """A reading from the answers to :func:`queries` (every one of them: a missing answer is a ``KeyError``)."""
    # `up` of a probe job says the blackbox exporter answered, not the service. The query leaves out
    # ``health_job``; a probe job under another name is recognised by the probe series it also carries, so a
    # mismatched ``health_job`` can't make a dead service look scraped.
    probe_jobs = {s.labels.get("job") for name in ("code_now", "code_seen") for s in results[name]} - {None}
    r = {
        name: _by_key([s for s in results[name] if not (name.startswith("up_") and s.labels.get("job") in probe_jobs)])
        for name in queries(cfg)
    }
    keys = sorted(set().union(*(r[name] for name in ("up_now", "up_seen", "code_now", "code_seen"))))
    labelled = (r["build_now"], r["build_recent"], r["up_now"], r["code_now"], r["up_seen"], r["code_seen"])
    services = []
    for key in keys:
        state, detail, held = _state(key, r)
        versions = _versions(r["build_now"], key)
        recent = _versions(r["build_recent"], key)
        services.append(
            Service(
                peer=key[0],
                where=key[1],
                container=key[2],
                package=_label((r["build_now"], r["build_recent"]), key, "service")
                or _label(labelled, key, "argus_service"),
                state=state,
                detail=detail,
                held=held,
                versions=versions,
                previous=tuple(v for v in recent if v not in versions),
            )
        )
    skews, behind = _skews(services)
    services = [replace(s, behind=behind.get(s.key, "")) for s in services]
    shared: dict[tuple[str, str], int] = {}
    for service in services:
        shared[(service.peer, service.where)] = shared.get((service.peer, service.where), 0) + 1
    names = {
        s.key: f"{s.name} ({s.container})" if shared[(s.peer, s.where)] > 1 and s.container else s.name
        for s in services
    }
    return SuiteStatus(at=at, services=tuple(services), skews=tuple(skews), names=names)


async def read_suite(client: PrometheusClient, cfg: SuiteStatusSource, at: datetime) -> SuiteStatus:
    """One reading. Raises :class:`~argus_scout.prometheus.PrometheusError` if any query can't be answered: the
    suite's state is then unknown, which is never the same as nothing being wrong."""
    named = queries(cfg)
    answers = await asyncio.gather(*(client.query(promql) for promql in named.values()))
    return build_status(dict(zip(named, answers, strict=True)), cfg, at)


# --- status events ---------------------------------------------------------------------------------------------


def _event_id(source: str, condition: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:12]
    return f"{source}:{condition}:{digest}"


def _last_version(service: Service) -> str:
    return ", ".join(service.versions or service.previous)


def suite_events(status: SuiteStatus, cfg: SuiteStatusSource) -> list[StatusEvent]:
    """The status events a reading calls for. Ids are stable per condition and service (a version change's also
    names the new version), and a summary says nothing that changes while the condition lasts, so a later reading
    refreshes the same event and its absence resolves it."""
    sev = cfg.severity
    events: list[StatusEvent] = []

    def add(condition: str, parts: tuple[str, ...], severity: Severity, summary: str, body: str, **payload) -> None:
        events.append(
            StatusEvent(
                event_id=_event_id(cfg.name, condition, *parts),
                source=cfg.name,
                severity=severity,
                generated_at=status.at,
                summary=summary,
                body_md=body,
                payload={"condition": condition, **payload},
            )
        )

    for service in status.services:
        name, about = status.named(service), {"peer": service.peer, "host": service.where, "service": service.package}
        last = _last_version(service)
        if service.state == "down" and service.held:
            waited = _duration(cfg.down_for)
            body = f"{service.detail.capitalize()} for at least {waited}." + (
                f" Last version seen: {last}." if last else ""
            )
            add("down", service.key, sev.down, f"{name} is down", body, **about, state="down", version=last)
        elif service.state in ("degraded", "starting") and service.held:
            waited = _duration(cfg.degraded_for)
            what = "is degraded" if service.state == "degraded" else "is still starting"
            body = f"{service.detail}; it has not answered ok for {waited}." + (f" Version {last}." if last else "")
            add("health", service.key, sev.degraded, f"{name} {what}", body, **about, state=service.state)
        if service.versions and service.previous:
            old, new = ", ".join(service.previous), service.version
            add(
                "version",
                (*service.key, new),
                sev.version_change,
                f"{name} changed version from {old} to {new}",
                f"Seen within the last {_duration(cfg.deploy_window)}.",
                **about,
                version=new,
                previous=old,
            )
    for skew in status.skews:
        add(
            "skew",
            (skew.peer,),
            sev.version_skew,
            f"{skew.peer} runs different versions: {skew.text}",
            "Two hosts run different versions of the same service.",
            peer=skew.peer,
            versions={version: list(hosts) for version, hosts in skew.versions},
        )
    return events


# --- the report ------------------------------------------------------------------------------------------------


def worst_severity(status: SuiteStatus, cfg: SuiteStatusSource) -> Severity:
    """What a suite report is about, for the channels: the most severe condition a reading holds."""
    return max((e.severity for e in suite_events(status, cfg)), key=rank, default=Severity.INFO)


def _line(status: SuiteStatus, service: Service) -> str:
    version = service.version or (f"last seen {_last_version(service)}" if _last_version(service) else "no version")
    package = f" ({service.package})" if service.package and service.package != f"argus-{service.peer}" else ""
    detail = f": {service.detail}" if service.detail else ""
    return f"- {status.named(service)}{package}: {service.state}{detail}, {version}"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def render_report(status: SuiteStatus, cfg: SuiteStatusSource, *, local: datetime | None = None) -> str:
    """The suite status as plain text: what is down, degraded or out of date first, then every service with its
    version. Deterministic: the same reading gives the same text, and no model is asked."""
    at = (local or status.at).strftime("%Y-%m-%d %H:%M %Z").strip()
    out = [f"Suite status at {at}: {_count(len(status.services), 'service')}."]
    if not status.services:
        out += [
            "",
            f"Prometheus has no series with an {PEER_LABEL} label, so there is nothing to report on. That is not "
            "an all-clear: check that Prometheus scrapes the suite (docs/suite.md).",
        ]
        return "\n".join(out) + "\n"
    window = _duration(cfg.deploy_window)
    sections = [
        ("Down", [_line(status, s) for s in status.in_state("down")]),
        ("Degraded or starting", [_line(status, s) for s in status.in_state("degraded", "starting")]),
        (
            "Out of date",
            [f"- {status.named(s)}: {s.version}, behind {s.behind} running elsewhere" for s in status.behind],
        ),
        (
            "Running different versions",
            [f"- {k.peer}: {k.text}" for k in status.skews if not any(s.peer == k.peer for s in status.behind)],
        ),
        (
            f"Changed version in the last {window}",
            [f"- {status.named(s)}: {', '.join(s.previous)} to {s.version}" for s in status.changed],
        ),
    ]
    if not any(lines for _, lines in sections[:4]):
        out += ["", "Nothing is down, degraded or out of date."]
    for heading, lines in sections:
        if lines:
            out += ["", f"{heading} ({len(lines)})", *lines]
    order = {state: i for i, state in enumerate(STATE_ORDER)}
    listed = sorted(status.services, key=lambda s: (order[s.state], s.key))
    out += ["", "Services", *(_line(status, s) for s in listed)]
    return "\n".join(out) + "\n"


def spoken_summary(status: SuiteStatus) -> str:
    """One or two sentences to say aloud for a suite report: counts and names, never the whole table."""
    if not status.services:
        return "Suite status: Prometheus lists no suite services, so there is nothing to report on."
    parts = [f"Suite status: {_count(len(status.services), 'service')}."]
    for label, found in (
        ("down", status.in_state("down")),
        ("degraded or starting", status.in_state("degraded", "starting")),
        ("out of date", status.behind),
    ):
        if found:
            names = ", ".join(status.named(s) for s in found[:5]) + (" and more" if len(found) > 5 else "")
            parts.append(f"{len(found)} {label}: {names}.")
    if len(parts) == 1:
        parts.append("Nothing is down, degraded or out of date.")
    return " ".join(parts)


__all__ = [
    "PEER_LABEL",
    "Service",
    "Skew",
    "State",
    "SuiteStatus",
    "build_status",
    "key_of",
    "queries",
    "read_suite",
    "render_report",
    "spoken_summary",
    "suite_events",
    "version_key",
    "worst_severity",
]
