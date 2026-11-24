"""The ``suite_status`` source: the Argus suite's health and versions, as status events.

    - kind: suite_status
      name: suite

Each poll is one reading of Prometheus (:func:`argus_scout.suite.read_suite`: ``up``, the ``/health`` probe and
``argus_build_info`` for every series with an ``argus_peer`` label) turned into status events
(:func:`argus_scout.suite.suite_events`): a service down, one that stays degraded, a version change, and two
hosts running different versions of one peer.

**Prometheus only.** The one host this source contacts is the configured Prometheus, through
:class:`~argus_scout.prometheus.PrometheusClient` (read-only PromQL, no redirects). It never connects to a service
it reports on, and it has no route, no writes and nothing it can act with.

**Cleared conditions.** A reading is the suite's whole current state, so with ``resolve_missing`` (the default)
the poll job closes this source's events a later reading no longer has. A reading that can't be completed raises
:class:`~argus_scout.sources.base.SourceError`: nothing is ingested and nothing is resolved, so a Prometheus
outage never reads as the suite being fine.
"""

from __future__ import annotations

from datetime import UTC, datetime

from argus_scout.config import SuiteStatusSource
from argus_scout.prometheus import PrometheusClient, PrometheusError
from argus_scout.sources.base import PollResult, SourceError
from argus_scout.suite import SuiteStatus, read_suite, suite_events


async def read_once(
    cfg: SuiteStatusSource, *, client: PrometheusClient | None = None, now: datetime | None = None
) -> SuiteStatus:
    """One reading of the suite for ``cfg``. :class:`SourceError` when there is no Prometheus to ask or it can't
    answer. ``client`` is a seam for tests; otherwise the client is built from ``cfg.prometheus_url``."""
    if client is None:
        if not cfg.prometheus_url:
            raise SourceError(f"{cfg.name}: no Prometheus to ask (set services.prometheus_url)")
        client = PrometheusClient(cfg.prometheus_url)
    try:
        return await read_suite(client, cfg, now or datetime.now(UTC))
    except PrometheusError as exc:
        raise SourceError(f"{cfg.name}: suite status is unknown: {exc}") from exc


async def poll_once(
    cfg: SuiteStatusSource, *, client: PrometheusClient | None = None, now: datetime | None = None
) -> PollResult:
    return PollResult(suite_events(await read_once(cfg, client=client, now=now), cfg))


class SuiteStatusPlugin:
    """The ``suite_status`` source plugin (pull only)."""

    kind = "suite_status"
    config_model = SuiteStatusSource

    async def poll(self, cfg: SuiteStatusSource) -> PollResult:
        return await poll_once(cfg)


plugin = SuiteStatusPlugin()

__all__ = ["SuiteStatusPlugin", "plugin", "poll_once", "read_once"]
