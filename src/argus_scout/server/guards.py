"""The HTTP security boundary: who may write, which writes are refused outright, and how much a body may hold.

Reaching scout's port is host access (there is no auth by default), so every state-changing route sits behind
**two independent layers**:

1. **WriteGuard** (argus-cortex, pure ASGI, gates every POST/PUT/PATCH/DELETE by method, so it covers routes
   added later without per-route wiring). :func:`install_guards` adds, in the suite's order (as argus-mask):

   - ``cross_site_refuse(server.allowed_origins)``: an untrusted ``Origin`` on a write is a ``403``. A request with
     no ``Origin`` (curl, scripts, producers) passes, as does a same-host IP-literal or ``localhost`` origin.
   - ``constant_refuse`` when ``server.read_only`` is set (the default): every write is a ``403``, before auth.

2. **A bearer token, failing closed.** Every write route depends on exactly one of:

   - :func:`write_auth`: argus-cortex's ``auth.bearer`` on ``server.token_env``. Unset or empty token is a
     ``503`` (the route is closed), a missing or wrong one a ``401``. With ``token_env: null`` the route is
     always closed.
   - :func:`source_auth`, for ``POST /api/events/{source}`` only: the push source's own ``authorize``. For the
     built-in ``webhook`` kind that is :func:`argus_scout.sources.webhook.check_token`: the source's own
     ``token_env`` if it names one, **else the server token**, with the same 503/401 semantics. A per-source
     token lets a producer push as that one source and nothing else; it can't ack, approve or reject. The
     server token works on every write route, webhook pushes included (unless the source names its own).

Both dependencies carry an :data:`AUTH_MARK` attribute, which :func:`unguarded_routes` (and the route-walking
test) uses to prove that every write route has one.

**Bodies are bounded**: :func:`read_json` and :func:`read_bounded` go through ``argus_cortex.limits.read_body``
(``413`` over the cap, ``400`` on a malformed ``Content-Length``). Write routes take ``Request`` and read the
body themselves, never as a FastAPI body parameter: FastAPI reads a declared body *before* it runs the
dependencies, which would let an unauthenticated caller make scout buffer a body.

**Reads** are open unless ``server.read_auth`` is set, in which case :func:`read_auth` is the same bearer check
on the read API. ``/health`` and ``/metrics`` are always open (they hold no event or report content).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from argus_cortex.auth import bearer
from argus_cortex.limits import BadInput, TooLarge, read_body
from argus_cortex.server import UNSAFE_METHODS, WriteGuard, constant_refuse, cross_site_refuse
from fastapi import FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute

from argus_scout.config import ServerConfig
from argus_scout.server.deps import get_runtime
from argus_scout.sources import BoundSource, SourceAuthError

EVENTS_MAX_BYTES = 256 * 1024
"""Cap on a ``POST /api/events/{source}`` body: room for :data:`~argus_scout.sources.webhook.MAX_EVENTS`
envelopes of a couple of KiB each."""
ACTION_MAX_BYTES = 8 * 1024
"""Cap on an ack, approve or reject body (``{by, reason}``)."""

READ_ONLY_REFUSAL = "scout is read-only (server.read_only): state-changing routes are refused"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

AUTH_MARK = "__scout_auth__"
"""Attribute naming the auth a dependency enforces (``"bearer"`` or ``"source-token"``)."""

Dependency = Callable[[HTTPConnection], Awaitable[None]]


def _mark(dep: Any, kind: str) -> Any:
    setattr(dep, AUTH_MARK, kind)
    return dep


def install_guards(app: FastAPI, server: ServerConfig) -> None:
    """Add the WriteGuards. Call once, after any other middleware, so the guards run first."""
    app.add_middleware(WriteGuard, refuse=cross_site_refuse(server.allowed_origins))
    if server.read_only:
        # Added last, so outermost: read-only refuses before anything else looks at the request.
        app.add_middleware(WriteGuard, refuse=constant_refuse(READ_ONLY_REFUSAL))


def _closed(why: str) -> Dependency:
    async def closed(conn: HTTPConnection) -> None:
        raise HTTPException(status_code=503, detail=why)

    return closed


def bearer_dependency(server: ServerConfig) -> Dependency:
    """``auth.bearer`` on ``server.token_env``; always closed when no variable is named."""
    if not server.token_env:
        return _closed("server.token_env is not set, so this route is closed")
    return bearer(server.token_env)


def write_auth(server: ServerConfig) -> Dependency:
    """The bearer check every write route (but the push route) depends on."""
    return _mark(bearer_dependency(server), "bearer")


def read_auth(server: ServerConfig) -> Dependency | None:
    """The bearer check on the read API when ``server.read_auth`` is set, else None (reads are open)."""
    return bearer_dependency(server) if server.read_auth else None


def source_auth(server: ServerConfig) -> Callable[..., Awaitable[BoundSource]]:
    """A dependency resolving ``{source}`` to a push source and checking the caller may push as it.

    Unknown source: 404. A source that doesn't take pushes (a poller): 405. Then the source's ``authorize``,
    with the server token as the fallback: no token configured is a 503, a wrong one a 401.
    """

    async def require_source_token(source: str, request: Request) -> BoundSource:
        bound = get_runtime(request).sources.get(source)
        if bound is None:
            raise HTTPException(status_code=404, detail=f"no source named {source!r}")
        if not bound.pushes:
            raise HTTPException(
                status_code=405, detail=f"source {source!r} is a {bound.kind} source; it takes no pushes"
            )
        try:
            bound.plugin.authorize(  # type: ignore[attr-defined]
                bound.config, request.headers.get("authorization"), fallback_env=server.token_env
            )
        except SourceAuthError as exc:
            headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
            raise HTTPException(status_code=exc.status, detail=str(exc), headers=headers) from None
        return bound

    return _mark(require_source_token, "source-token")


# --- bodies --------------------------------------------------------------------------------------------------


async def read_bounded(request: Request, max_bytes: int, *, what: str = "request body") -> bytes:
    """The request body, at most ``max_bytes`` (413 over it, 400 on a bad ``Content-Length``)."""
    try:
        return await read_body(request, max_bytes, what=what)
    except TooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except BadInput as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


async def read_json[M: BaseModel](request: Request, model: type[M], max_bytes: int = ACTION_MAX_BYTES) -> M:
    """A bounded JSON body validated as ``model``. An empty body is ``{}``; bad JSON 400, invalid 422."""
    raw = await read_bounded(request, max_bytes)
    try:
        data = json.loads(raw) if raw.strip() else {}
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="body is not JSON") from None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_context=False, include_input=False)
        ) from None


# --- the route walk ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteView:
    """One route as it is served: its effective path, methods and dependencies (router-level ones included)."""

    route: BaseRoute
    """The route as declared (``APIRoute``, ``Mount``, a websocket route...)."""
    path: str
    methods: frozenset[str]
    dependant: Any | None
    """FastAPI's resolved dependency tree, for an ``APIRoute``."""


def iter_routes(app: FastAPI) -> list[RouteView]:
    """Every route ``app`` serves, flattened through included routers.

    FastAPI 0.16x keeps an included router as one lazy entry in ``app.routes`` and resolves its routes (with
    the prefix and the router-level dependencies) through ``iter_route_contexts``; older versions copy them
    into ``app.routes`` directly. This reads either, so the walk can't miss a route inside a router.
    """
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # pragma: no cover — FastAPI before lazy router inclusion
        contexts: list[Any] = list(app.routes)
        originals = contexts
    else:
        contexts = list(iter_route_contexts(app.routes))
        originals = [rc.route for rc in contexts]
    views = []
    for original, ctx in zip(originals, contexts, strict=True):
        path = getattr(ctx, "path", None) or getattr(original, "path", None) or repr(original)
        methods = frozenset(getattr(ctx, "methods", None) or ())
        dependant = getattr(ctx, "dependant", None) if isinstance(original, APIRoute) else None
        views.append(RouteView(original, path, methods, dependant))
    return views


def _auth_kinds(dependant: Any) -> set[str]:
    kinds: set[str] = set()
    stack = list(dependant.dependencies) if dependant is not None else []
    while stack:
        dep = stack.pop()
        kind = getattr(dep.call, AUTH_MARK, None)
        if kind:
            kinds.add(kind)
        stack.extend(dep.dependencies)
    return kinds


def _guards(app: FastAPI) -> list[str]:
    """The WriteGuard predicates installed on ``app``, named by the factory that made them."""
    names = []
    for mw in app.user_middleware:
        if mw.cls is WriteGuard:
            refuse = mw.kwargs.get("refuse")
            names.append(getattr(refuse, "__qualname__", "").split(".<locals>")[0])
    return names


def unguarded_routes(app: FastAPI, *, read_only: bool | None = None) -> list[str]:
    """Every route that could change state without passing the guards, as ``"METHODS path: why"``.

    A route can change state if it takes any method other than GET/HEAD/OPTIONS, or is a websocket or a
    mount (which this walk can't see into, and WriteGuard doesn't gate). Each such route must be an
    ``APIRoute`` whose methods WriteGuard gates, carrying a dependency marked with :data:`AUTH_MARK`; and the
    app must have the cross-origin guard (plus the read-only guard when ``read_only``). Empty: all guarded.
    """
    problems: list[str] = []
    guards = _guards(app)
    if "cross_site_refuse" not in guards:
        problems.append("app: no WriteGuard(cross_site_refuse) installed")
    if read_only and "constant_refuse" not in guards:
        problems.append("app: read_only is set but no WriteGuard(constant_refuse) is installed")
    for view in iter_routes(app):
        problems.extend(_route_problems(view))
    return problems


def _route_problems(view: RouteView) -> list[str]:
    if not isinstance(view.route, APIRoute):
        if view.methods and view.methods <= SAFE_METHODS:
            return []
        what = sorted(view.methods) if view.methods else type(view.route).__name__
        return [f"{what} {view.path}: not an APIRoute, so its guards can't be checked"]
    unsafe = view.methods - SAFE_METHODS
    if not unsafe:
        return []
    problems = []
    if unsafe - UNSAFE_METHODS:
        problems.append(f"{sorted(unsafe)} {view.path}: WriteGuard does not gate {sorted(unsafe - UNSAFE_METHODS)}")
    if not _auth_kinds(view.dependant):
        problems.append(f"{sorted(unsafe)} {view.path}: no write_auth or source_auth dependency")
    return problems


__all__ = [
    "ACTION_MAX_BYTES",
    "AUTH_MARK",
    "EVENTS_MAX_BYTES",
    "READ_ONLY_REFUSAL",
    "SAFE_METHODS",
    "RouteView",
    "iter_routes",
    "install_guards",
    "read_auth",
    "read_bounded",
    "read_json",
    "source_auth",
    "unguarded_routes",
    "write_auth",
]
