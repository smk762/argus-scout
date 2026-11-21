"""HTTP server. Read-only for now: state-changing routes arrive with auth (see the epic).

:func:`create_app` builds the app from a :class:`~argus_scout.config.ScoutConfig`. Its lifespan opens the
store, registers the scheduler's jobs and starts the scheduler, and stops both on shutdown. Building the
app has no side effects; nothing is opened until the app starts.

Routes: ``/health`` and ``/metrics`` (see :mod:`argus_scout.server.metrics`). Both are read-only and need no
auth. Every state-changing route must go behind argus-cortex's ``auth.bearer`` and ``WriteGuard``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

import argus_scout
from argus_scout.budget import DailyBudget, ManualOriginMiddleware
from argus_scout.config import ScoutConfig
from argus_scout.cycles import register_jobs
from argus_scout.heartbeat import PolicyFile
from argus_scout.inbox import Inbox
from argus_scout.prometheus import PrometheusClient
from argus_scout.scheduler import Clock, Scheduler, Sleep, utc_now
from argus_scout.server.metrics import ScoutCollector, build_registry
from argus_scout.sources import Sources
from argus_scout.store.base import Store
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


def build_runtime(
    config: ScoutConfig,
    *,
    store: Store | None = None,
    budget: DailyBudget | None = None,
    inbox: Inbox | None = None,
    sources: Sources | None = None,
    prometheus: PrometheusClient | None = None,
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
    register_jobs(scheduler, config, store, policy_file, inbox=inbox, sources=sources, prometheus=prometheus)
    collector = ScoutCollector(
        store=store, budget=budget, tariff=tariff, scheduler=scheduler, tz=config.schedule.tz, clock=clock
    )
    return Runtime(config, store, policy_file, inbox, tariff, budget, scheduler, build_registry(collector))


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


__all__ = ["Runtime", "build_runtime", "create_app"]
