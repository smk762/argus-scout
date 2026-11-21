"""The HTTP API. :func:`include_routes` mounts it; :mod:`argus_scout.server.guards` is its security boundary.

| Method | Path | Auth |
|---|---|---|
| POST | ``/api/events/{source}`` | the source's token, else the server token |
| POST | ``/api/events/{source}/{event_id}/ack`` | server token |
| POST | ``/api/recommendations/{id}/approve`` | server token |
| POST | ``/api/recommendations/{id}/reject`` | server token |
| POST | ``/api/reports``, ``/api/reports/{id}/deliver`` | server token |
| GET | ``/api/events``, ``/api/recommendations``, ``/api/recommendations/{id}``, ``/api/reports``, ``/api/reports/{id}``, ``/api/reports/latest``, ``/api/policy/explain``, ``/wall`` | none, or the server token with ``server.read_auth`` |

Every POST is also behind WriteGuard (cross-origin, and read-only when ``server.read_only``).
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from argus_scout.config import ServerConfig
from argus_scout.server.guards import read_auth
from argus_scout.server.routes import events, reads, recommendations, reports


def include_routes(app: FastAPI, server: ServerConfig) -> None:
    for router in (events.write_router(server), recommendations.write_router(server)):
        app.include_router(router)
    app.include_router(reports.write_router(server))
    guard = read_auth(server)
    read_deps = [Depends(guard)] if guard else []
    for router in (events.read_router(), recommendations.read_router(), reads.read_router()):
        app.include_router(router, dependencies=read_deps)
    app.include_router(reports.read_router(), dependencies=read_deps)  # after reads: /latest is declared first


__all__ = ["include_routes"]
