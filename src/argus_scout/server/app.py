"""HTTP server: the scheduler's host, ``/health``, ``/metrics`` and the API.

:func:`create_app` builds the app from a :class:`~argus_scout.config.ScoutConfig`. Its lifespan opens the
store, registers the scheduler's jobs and starts the scheduler, and stops both on shutdown. Building the
app has no side effects; nothing is opened until the app starts.

Routes: ``/health`` and ``/metrics`` (see :mod:`argus_scout.server.metrics`), always open; the API in
:mod:`argus_scout.server.routes`. Every state-changing route goes behind argus-cortex's ``auth.bearer`` (or a
push source's token) and ``WriteGuard``: see :mod:`argus_scout.server.guards`, and ``tests/test_api_guards.py``,
which walks the routes and fails on an unguarded one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

import argus_scout
from argus_scout.budget import DailyBudget, ManualOriginMiddleware
from argus_scout.channels import Channels, Delivery, Dispatcher, DispatchReport, Synthesiser, build_dispatcher
from argus_scout.config import ScoutConfig
from argus_scout.cycles import register_jobs
from argus_scout.heartbeat import PolicyFile
from argus_scout.inbox import Inbox
from argus_scout.prometheus import PrometheusClient
from argus_scout.scheduler import Clock, Scheduler, Sleep, utc_now
from argus_scout.server.deps import get_runtime
from argus_scout.server.guards import install_guards
from argus_scout.server.metrics import ScoutCollector, build_registry
from argus_scout.server.routes import include_routes
from argus_scout.sources import Sources, build_sources
from argus_scout.events import StatusEvent
from argus_scout.store.base import Report, Store
from argus_scout.store.sqlite import SqliteStore
from argus_scout.tariff import TariffFile

USAGE_STATE_FILE = "usage.json"
"""Daily usage counts, kept beside the store so a crash loop can't reset the caps."""


@dataclass
class Runtime:
    """Everything the running app holds; available as ``app.state.runtime`` while it runs."""

    config: ScoutConfig
    store: Store
    policy_file: PolicyFile
    inbox: Inbox
    tariff: TariffFile
    budget: DailyBudget
    scheduler: Scheduler
    registry: CollectorRegistry
    sources: Sources = field(default_factory=Sources)
    channels: Channels = field(default_factory=Channels)
    dispatcher: Dispatcher = field(default_factory=lambda: Dispatcher([]))

    async def deliver(self, report: Report, *, force: bool = False) -> DispatchReport:
        """Send a stored report to every channel that should get it (the narrative's delivery seam).

        Idempotent: a report already sent on a channel isn't sent there again, even after a restart."""
        return await self.dispatcher.deliver(Delivery.from_report(report), force=force)

    async def deliver_event(self, event: StatusEvent) -> DispatchReport:
        """Send one inbox status item (the latest event of a group) to the channels."""
        return await self.dispatcher.deliver(Delivery.from_event(event))


def build_runtime(
    config: ScoutConfig,
    *,
    store: Store | None = None,
    budget: DailyBudget | None = None,
    inbox: Inbox | None = None,
    sources: Sources | None = None,
    prometheus: PrometheusClient | None = None,
    channels: Channels | None = None,
    synthesiser: Synthesiser | None = None,
    clock: Clock = utc_now,
    sleep: Sleep | None = None,
) -> Runtime:
    if store is None:
        store = SqliteStore(config.store.path)
    if budget is None:
        db = str(config.store.path)
        budget = DailyBudget(state_path=None if db == ":memory:" else Path(db).parent / USAGE_STATE_FILE)
    policy_file = PolicyFile(config.files.heartbeat)
    tariff = TariffFile(config.files.tariff)
    scheduler = Scheduler(store, clock=clock, **({"sleep": sleep} if sleep else {}))
    inbox = inbox or Inbox(store, clock=clock)
    sources = sources if sources is not None else build_sources(config)
    register_jobs(scheduler, config, store, policy_file, inbox=inbox, sources=sources, prometheus=prometheus)
    channels, dispatcher = build_dispatcher(
        config,
        store=store,
        policy_file=policy_file,
        channels=channels,
        synthesiser=synthesiser,
        budget=budget,
        clock=clock,
    )
    collector = ScoutCollector(
        store=store,
        budget=budget,
        tariff=tariff,
        scheduler=scheduler,
        tz=config.schedule.tz,
        clock=clock,
    )
    registry = build_registry(collector)
    return Runtime(
        config,
        store,
        policy_file,
        inbox,
        tariff,
        budget,
        scheduler,
        registry,
        sources=sources,
        channels=channels,
        dispatcher=dispatcher,
    )


def create_app(
    config: ScoutConfig | None = None,
    *,
    store: Store | None = None,
    budget: DailyBudget | None = None,
    inbox: Inbox | None = None,
    sources: Sources | None = None,
    clock: Clock = utc_now,
    sleep: Sleep | None = None,
    run_scheduler: bool = True,
) -> FastAPI:
    """The scout app. ``store``, ``budget``, ``inbox``, ``sources``, ``clock`` and ``sleep`` are seams for tests and
    embedding; a ``store`` passed in is left open on shutdown (whoever made it closes it)."""
    config = config or ScoutConfig()
    owns_store = store is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = build_runtime(
            config, store=store, budget=budget, inbox=inbox, sources=sources, clock=clock, sleep=sleep
        )
        app.state.runtime = runtime
        if run_scheduler:
            runtime.scheduler.start()
        try:
            yield
        finally:
            await runtime.scheduler.stop()
            if owns_store:
                runtime.store.close()

    app = FastAPI(title="argus-scout", version=argus_scout.__version__, lifespan=lifespan)
    app.add_middleware(ManualOriginMiddleware)
    install_guards(app, config.server)  # after other middleware: the guards run first
    include_routes(app, config.server)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": argus_scout.__version__}

    @app.get("/metrics")
    def metrics(request: Request) -> Response:
        runtime: Runtime | None = getattr(request.app.state, "runtime", None)
        if runtime is None:
            return Response("scout is not running\n", status_code=503, media_type="text/plain")
        return Response(generate_latest(runtime.registry), media_type=CONTENT_TYPE_LATEST)

    return app


__all__ = ["Runtime", "build_runtime", "create_app", "get_runtime"]
