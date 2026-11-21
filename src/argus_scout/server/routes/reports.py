"""Reports over HTTP: write one on request, list and read them, and send a stored one to the channels.

- ``POST /api/reports``: ``{"kind": "morning"|"evening"|"status", "deliver": true, "no_llm": false}``. Written
  through the runtime's :class:`~argus_scout.cycles.Briefings`, the same path the scheduled briefing jobs take: the
  same narrator, the ``llm_calls`` daily cap (over it, the template), the GPU lease and the template fallback. Stored,
  then delivered through the dispatcher unless ``deliver`` is false (then nothing is sent or spoken). **One report of
  a kind at a time**: while one is being written, scheduled or on request, this is a ``409`` at once rather than a
  request held open for the length of a model call; retry when it's done.
- ``POST /api/reports/{id}/deliver``: ``{"force": false, "channels": null}``, as ``argus-scout deliver``.
  Idempotent: a channel that already has the report reports ``duplicate`` unless ``force``. ``channels`` names a
  subset (an unknown name is a ``422``). Voice follows the heartbeat gates and the speech caps, as for any delivery.

Both writes are **held to the daily caps** as the CLI and the scheduler are, ``budget.block_manual`` or not
(:class:`~argus_scout.cycles.Briefings`): over ``llm_calls`` a report comes from the template, over ``tts`` or
``speak`` voice is held back and text still goes.
- ``GET /api/reports?kind=&limit=``: stored reports, newest first (``limit`` 1-200, default 20).
- ``GET /api/reports/{id}``: one report. ``{id}`` is an integer, so it never shadows ``/api/reports/latest``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool

from argus_scout.budget import reset_manual, set_manual
from argus_scout.channels import Delivery, DispatchReport
from argus_scout.config import ServerConfig
from argus_scout.cycles import ReportBusy
from argus_scout.server.deps import get_runtime
from argus_scout.server.guards import read_json, write_auth
from argus_scout.server.routes.reads import report_dict

LIST_LIMIT = 200
MAX_CHANNELS = 50

Kind = Literal["morning", "evening", "status"]
ChannelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GenerateBody(_Body):
    kind: Kind
    deliver: bool = True
    no_llm: bool = False


class DeliverBody(_Body):
    force: bool = False
    channels: Annotated[list[ChannelName], Field(min_length=1, max_length=MAX_CHANNELS)] | None = None
    """Only these channels; null (the default) is every channel."""


def dispatch_dict(result: DispatchReport) -> dict[str, Any]:
    return {
        "sent": result.sent,
        "failed": result.failed,
        "voice_rule": result.voice_rule,
        "synthesised": result.synthesised,
        "results": [
            {"channel": r.channel, "status": r.status, "detail": r.detail, "error": r.error} for r in result.results
        ],
    }


def write_router(server: ServerConfig) -> APIRouter:
    router = APIRouter(prefix="/api/reports", tags=["reports"])

    @router.post("", dependencies=[Depends(write_auth(server))])
    async def generate(request: Request) -> dict[str, Any]:
        runtime = get_runtime(request)
        body = await read_json(request, GenerateBody)
        if runtime.briefings is None:  # pragma: no cover — build_runtime always sets it
            raise HTTPException(status_code=503, detail="scout has no report writer")
        try:
            written = await runtime.briefings.write(
                body.kind, use_llm=not body.no_llm, trigger="request", deliver=body.deliver, wait=False
            )
        except ReportBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        delivery = written.delivery if isinstance(written.delivery, DispatchReport) else None
        return {
            **report_dict(written.report),
            "delivered": written.delivered,
            "delivery": dispatch_dict(delivery) if delivery else None,
        }

    @router.post("/{report_id:int}/deliver", dependencies=[Depends(write_auth(server))])
    async def deliver(report_id: int, request: Request) -> dict[str, Any]:
        runtime = get_runtime(request)
        body = await read_json(request, DeliverBody)
        report = await run_in_threadpool(runtime.store.get_report, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"no report {report_id}")
        names = {b.name for b in runtime.dispatcher.channels}
        if not names:
            raise HTTPException(status_code=409, detail="no channels configured")
        unknown = sorted(set(body.channels or ()) - names)
        if unknown:
            raise HTTPException(status_code=422, detail=f"no channel named {', '.join(unknown)}")
        manual = set_manual(False)  # held to the speech caps, as `argus-scout deliver` is
        try:
            result = await runtime.dispatcher.deliver(
                Delivery.from_report(report), force=body.force, only=body.channels
            )
        finally:
            reset_manual(manual)
        return {"report_id": report.id, "kind": report.kind, **dispatch_dict(result)}

    return router


def read_router() -> APIRouter:
    router = APIRouter(prefix="/api/reports", tags=["reports"])

    @router.get("")
    def list_reports(
        request: Request,
        kind: Kind | None = None,
        limit: Annotated[int, Query(ge=1, le=LIST_LIMIT)] = 20,
    ) -> dict[str, Any]:
        reports = get_runtime(request).store.list_reports(limit, kind=kind)
        return {"count": len(reports), "reports": [report_dict(r) for r in reports]}

    @router.get("/{report_id:int}")
    def get_report(report_id: int, request: Request) -> dict[str, Any]:
        report = get_runtime(request).store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"no report {report_id}")
        return report_dict(report)

    return router


__all__ = ["DeliverBody", "GenerateBody", "dispatch_dict", "read_router", "write_router"]
