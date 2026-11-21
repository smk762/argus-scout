"""Recommendations over HTTP: list them, read one with its audit trail, and let a person approve or reject.

``POST /api/recommendations/{id}/approve`` and ``/reject`` are **manual** transitions: approve sets
``approval_source="manual"`` (never a standing rule), and both record who decided, and why, in the store's
transition audit trail. Only a ``proposed`` recommendation can be approved or rejected; anything else is a
``409``.

**A decision is on the content the person saw.** Every save bumps a recommendation's ``revision`` (shown by the
GETs and on ``/wall``), and the engine rewrites a proposal's title, action, severity, evidence and expiry while it
stays ``proposed``. So the body names the ``revision`` the person viewed, and the store compares it in the same
transaction as the write (:meth:`~argus_scout.store.base.Store.update_recommendation`): a mismatch is a ``409``
"changed since you viewed it; reload", never a yes to wording nobody read. The decision is applied to the row as
stored inside that transaction, never to a copy read earlier, so it can't revert an engine refresh either; and the
engine's own load-then-save carries the revision it read, so it can't overwrite a person's decision.

There is no route past ``approved``: recording that a person carried a recommendation out (``deployed``,
``retired``) is left out of v0.1, and scout itself never deploys anything.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from argus_scout.config import ServerConfig
from argus_scout.recommendations import Recommendation, Status
from argus_scout.server.deps import get_runtime
from argus_scout.server.guards import read_json, write_auth
from argus_scout.server.routes.models import DecisionBody
from argus_scout.store.base import StaleRecommendation, Store, StoreConflict, Transition

LIST_LIMIT = 500


class NotDecidable(Exception):
    """The stored recommendation isn't ``proposed``: nothing is written."""


def transition_dict(t: Transition) -> dict[str, Any]:
    return {
        "from": t.from_status.value if t.from_status else None,
        "to": t.to_status.value,
        "at": t.at.isoformat(),
        "by": t.by,
        "source": t.source,
        "rule_id": t.rule_id,
        "reason": t.reason,
    }


def with_history(store: Store, rec: Recommendation) -> dict[str, Any]:
    return {**rec.to_dict(), "history": [transition_dict(t) for t in store.recommendation_history(rec.id)]}


def write_router(server: ServerConfig) -> APIRouter:
    router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])

    async def decide(request: Request, rec_id: str, *, approve: bool) -> dict[str, Any]:
        runtime = get_runtime(request)
        body = await read_json(request, DecisionBody)
        done = "approved" if approve else "rejected"

        def change(rec: Recommendation) -> None:
            now = runtime.inbox.now()  # the app's clock, as the engine's, so the audit trail orders correctly
            if not (rec.approve(now=now) if approve else rec.reject(body.reason, now=now)):
                raise NotDecidable(
                    f"recommendation {rec_id!r} is {rec.status.value}; only a proposed one can be {done}"
                )

        def apply() -> dict[str, Any]:
            try:
                rec = runtime.store.update_recommendation(
                    rec_id, change, expected_revision=body.revision, by=body.by, reason=body.reason
                )
            except NotDecidable as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            except StaleRecommendation:
                raise HTTPException(
                    status_code=409,
                    detail=f"recommendation {rec_id!r} changed since you viewed it (revision {body.revision}); reload",
                ) from None
            except StoreConflict as exc:
                raise HTTPException(status_code=409, detail=f"{exc}; reload and decide again") from None
            if rec is None:
                raise HTTPException(status_code=404, detail=f"no recommendation {rec_id!r}")
            return with_history(runtime.store, rec)

        return await run_in_threadpool(apply)

    @router.post("/{rec_id}/approve", dependencies=[Depends(write_auth(server))])
    async def approve(rec_id: str, request: Request) -> dict[str, Any]:
        return await decide(request, rec_id, approve=True)

    @router.post("/{rec_id}/reject", dependencies=[Depends(write_auth(server))])
    async def reject(rec_id: str, request: Request) -> dict[str, Any]:
        return await decide(request, rec_id, approve=False)

    return router


def read_router() -> APIRouter:
    router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])

    @router.get("")
    def list_recommendations(
        request: Request,
        status: Annotated[list[Status] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=LIST_LIMIT)] = 100,
    ) -> dict[str, Any]:
        recs = get_runtime(request).store.list_recommendations(status or None, limit=limit)
        return {"count": len(recs), "recommendations": [r.to_dict() for r in recs]}

    @router.get("/{rec_id}")
    def get_recommendation(rec_id: str, request: Request) -> dict[str, Any]:
        store = get_runtime(request).store
        rec = store.get_recommendation(rec_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"no recommendation {rec_id!r}")
        return with_history(store, rec)

    return router


__all__ = ["read_router", "transition_dict", "with_history", "write_router"]
