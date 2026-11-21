"""Standing approval rules over HTTP: list them, and delete a runtime one.

- ``GET /api/approvals/rules``: every rule, file and runtime, each with its ``origin`` (``file`` from
  ``files.approvals``, ``api`` created at runtime by an approve with ``standing``), ``expires_at`` and ``expired``.
- ``DELETE /api/approvals/rules/{rule_id}``: removes a **runtime** rule (body ``{"by": ...}``, logged with the
  rule). A file rule is a ``409``: it lives in the approvals file, so edit that. Unknown is a ``404``.

Runtime rules are created by ``POST /api/recommendations/{id}/approve`` with ``standing`` (see
:mod:`argus_scout.server.routes.recommendations`), never from scratch: a rule always starts from a recommendation a
person approved, so the guardrails have something to check.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from argus_scout.config import ServerConfig
from argus_scout.server.deps import get_runtime
from argus_scout.server.guards import read_json, write_auth
from argus_scout.server.routes.models import RuleDeleteBody

PREFIX = "/api/approvals/rules"


def write_router(server: ServerConfig) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["approvals"])

    @router.delete("/{rule_id}", dependencies=[Depends(write_auth(server))])
    async def delete_rule(rule_id: str, request: Request) -> dict[str, Any]:
        runtime = get_runtime(request)
        body = await read_json(request, RuleDeleteBody)

        def apply() -> dict[str, Any]:
            approvals = runtime.approvals
            if approvals.remove(rule_id, by=body.by):
                return {"rule_id": rule_id, "deleted": True, "by": body.by}
            if approvals.get(rule_id) is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"rule {rule_id!r} comes from the approvals file: edit the approvals file to remove it",
                )
            raise HTTPException(status_code=404, detail=f"no standing rule {rule_id!r}")

        return await run_in_threadpool(apply)

    return router


def read_router() -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["approvals"])

    @router.get("")
    def list_rules(request: Request) -> dict[str, Any]:
        runtime = get_runtime(request)
        now = runtime.inbox.now()
        rules = []
        for rule in runtime.approvals.rules():
            rules.append(rule.to_dict() | {"expired": rule.expired(now)})
        return {"count": len(rules), "rules": rules}

    return router


__all__ = ["read_router", "write_router"]
