"""Status events over HTTP: producers push them, people ack them, anyone with read access lists them.

- ``POST /api/events/{source}``: a push source's producer sends one envelope or a list of them (see
  :mod:`argus_scout.sources.webhook`). Auth is the source's own token, else the server token
  (:func:`~argus_scout.server.guards.source_auth`). The body is taken whole or not at all: every envelope is
  validated before any is ingested. The reply gives each event's inbox outcome.
- ``POST /api/events/{source}/{event_id}/ack``: ``{"by": ...}``; server token only.
- ``GET /api/events``: open events, grouped by fingerprint.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from argus_scout.config import ServerConfig
from argus_scout.events import StatusEvent
from argus_scout.inbox import EventGroup, IngestResult, InvalidEvent, normalise
from argus_scout.server.deps import get_runtime
from argus_scout.server.guards import EVENTS_MAX_BYTES, read_bounded, read_json, source_auth, write_auth
from argus_scout.server.routes.models import AckBody
from argus_scout.sources import BoundSource


def _outcome(result: IngestResult) -> dict[str, Any]:
    return {
        "event_id": result.event.event_id,
        "outcome": result.outcome.value,
        "news": result.is_news,
        "reopened": result.reopened,
        "previous_severity": result.previous_severity.value if result.previous_severity else None,
        "duplicate_of": [{"source": s, "event_id": e} for s, e in result.duplicate_of],
    }


def group_dict(group: EventGroup) -> dict[str, Any]:
    latest = group.latest
    return {
        "fingerprint": group.fingerprint,
        "count": group.count,
        "source": latest.source,
        "severity": latest.severity.value,
        "summary": latest.summary,
        "event_ids": [e.event_id for e in group.events],
        "first_seen": group.first.ingested_at.isoformat(),
        "latest": latest.to_dict(),
    }


def write_router(server: ServerConfig) -> APIRouter:
    router = APIRouter(prefix="/api/events", tags=["events"])

    @router.post("/{source}")
    async def push(request: Request, bound: BoundSource = Depends(source_auth(server))) -> dict[str, Any]:  # noqa: B008
        runtime = get_runtime(request)
        body = await read_bounded(request, EVENTS_MAX_BYTES, what="event body")
        try:
            events: list[StatusEvent] = bound.plugin.parse(bound.config, body)  # type: ignore[attr-defined]
            now = runtime.inbox.now()
            for index, event in enumerate(events):
                try:
                    normalise(event, now=now)  # all or nothing: refuse the batch before ingesting any of it
                except InvalidEvent as exc:
                    raise InvalidEvent(f"event {index}: {exc}") from exc
        except InvalidEvent as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        def ingest() -> list[IngestResult]:
            return [runtime.inbox.ingest(event) for event in events]

        results = await run_in_threadpool(ingest)
        return {"source": bound.name, "accepted": len(results), "events": [_outcome(r) for r in results]}

    @router.post("/{source}/{event_id:path}/ack", dependencies=[Depends(write_auth(server))])
    async def ack(source: str, event_id: str, request: Request) -> dict[str, Any]:
        runtime = get_runtime(request)
        body = await read_json(request, AckBody)

        def do_ack() -> dict[str, Any]:
            entry = runtime.store.get_entry(source, event_id)
            if entry is None:
                raise HTTPException(status_code=404, detail=f"no event {event_id!r} from source {source!r}")
            if not runtime.inbox.ack(source, event_id, body.by):
                raise HTTPException(status_code=409, detail=f"event {event_id!r} is already acked")
            acked = runtime.store.get_event(source, event_id)
            at = acked.acked_at.isoformat() if acked and acked.acked_at else None
            return {"source": source, "event_id": event_id, "acked": True, "by": body.by, "at": at}

        return await run_in_threadpool(do_ack)

    return router


def read_router() -> APIRouter:
    router = APIRouter(prefix="/api/events", tags=["events"])

    @router.get("")
    def open_events(request: Request) -> dict[str, Any]:
        groups = get_runtime(request).inbox.open_grouped()
        return {"count": sum(g.count for g in groups), "groups": [group_dict(g) for g in groups]}

    return router


__all__ = ["group_dict", "read_router", "write_router"]
