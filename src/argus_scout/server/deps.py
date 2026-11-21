"""Request-scoped helpers shared by the routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException
from starlette.requests import HTTPConnection

if TYPE_CHECKING:
    from argus_scout.server.app import Runtime


def get_runtime(conn: HTTPConnection) -> Runtime:
    """The running app's :class:`~argus_scout.server.app.Runtime`, or 503 before startup / after shutdown."""
    runtime = getattr(conn.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="scout is not running")
    return runtime


__all__ = ["get_runtime"]
