"""The HTTP security boundary: walk every route and prove each write is guarded, structurally and by request.

The CLAUDE.md "Must never happen" row on unguarded state-changing routes points here. A new POST/PUT/PATCH/DELETE
route without ``write_auth`` (or the push route's ``source_auth``) fails ``test_every_write_route_is_guarded``,
and the request tests below are driven by the same walk, so a new write route is exercised automatically.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from api_helpers import AUTH, OWN_TOKEN, TOKEN, make_app, scout_client
from fastapi import Depends, FastAPI
from starlette.routing import Mount

import argus_scout.server
from argus_scout.server.guards import (
    ACTION_MAX_BYTES,
    EVENTS_MAX_BYTES,
    HOST_OPEN_PATHS,
    HOST_REFUSAL,
    READ_ONLY_REFUSAL,
    SAFE_METHODS,
    install_guards,
    iter_routes,
    unguarded_routes,
    write_auth,
)
from argus_scout.store import SqliteStore

SERVER_SRC = Path(argus_scout.server.__file__).parent
PARAMS = {"source": "hook", "event_id": "e1", "rec_id": "r1", "rule_id": "api-nope"}


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setenv("SCOUT_API_TOKEN", TOKEN)
    monkeypatch.setenv("OWN_TOKEN", OWN_TOKEN)


@pytest.fixture
def store():
    with SqliteStore(":memory:") as s:
        yield s


def write_routes(app: FastAPI) -> list[tuple[str, str]]:
    """``(method, concrete path)`` for every unsafe method of every route."""
    out = []
    for view in iter_routes(app):
        path = re.sub(r"\{(\w+)(:\w+)?\}", lambda m: PARAMS[m.group(1)], view.path)
        out += [(method, path) for method in sorted(view.methods - SAFE_METHODS)]
    return out


# --- the walk ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("read_only", [True, False])
def test_every_write_route_is_guarded(store, tmp_path, read_only):
    app = make_app(store, tmp_path, read_only=read_only)
    assert unguarded_routes(app, read_only=read_only) == []
    assert len(write_routes(app)) == 5, "the walk found a different set of write routes: update the tests"


def test_walker_flags_an_unguarded_route(store, tmp_path):
    """Proof the walk bites: a deliberately unguarded POST on a throwaway app is reported."""
    app = make_app(store, tmp_path)

    @app.post("/oops")
    def oops() -> None: ...

    @app.delete("/also-fine", dependencies=[Depends(write_auth(app_config_server(tmp_path)))])
    def fine() -> None: ...

    problems = unguarded_routes(app)
    assert problems == ["['POST'] /oops: no write_auth or source_auth dependency"]


def app_config_server(tmp_path):
    from api_helpers import make_config

    return make_config(tmp_path).server


def test_walker_flags_missing_guards_and_unseeable_routes(tmp_path):
    bare = FastAPI()
    assert unguarded_routes(bare) == [
        "app: no HostGuard installed (a rebinding Host could read the API)",
        "app: no WriteGuard(cross_site_refuse) installed",
    ]

    server = app_config_server(tmp_path)
    install_guards(bare, server)  # read_only is off in this config
    assert unguarded_routes(bare, read_only=True) == [
        "app: read_only is set but no WriteGuard(constant_refuse) is installed"
    ]

    bare.add_api_route("/odd", lambda: None, methods=["PURGE"], dependencies=[Depends(write_auth(server))])
    bare.add_route("/raw", lambda request: None, methods=["POST"])
    bare.mount("/sub", FastAPI())

    @bare.websocket("/ws")
    async def ws(websocket) -> None: ...

    problems = unguarded_routes(bare)
    assert "['PURGE'] /odd: WriteGuard does not gate ['PURGE']" in problems
    assert "['POST'] /raw: not an APIRoute, so its guards can't be checked" in problems
    assert "Mount /sub: not an APIRoute, so its guards can't be checked" in problems
    assert "APIWebSocketRoute /ws: not an APIRoute, so its guards can't be checked" in problems


# --- the same walk, by request -------------------------------------------------------------------------------


def test_no_token_configured_refuses_every_write(store, tmp_path, monkeypatch):
    monkeypatch.delenv("SCOUT_API_TOKEN")
    with scout_client(make_app(store, tmp_path)) as client:
        for method, path in write_routes(client.app):
            response = client.request(method, path, headers=AUTH, json={"by": "me"})
            assert response.status_code == 503, f"{method} {path} is open with no token configured"


def test_token_env_null_closes_every_write(store, tmp_path):
    with scout_client(make_app(store, tmp_path, token_env=None)) as client:
        for method, path in write_routes(client.app):
            assert client.request(method, path, headers=AUTH, json={"by": "me"}).status_code == 503, path


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}])
def test_missing_or_wrong_token_is_401(store, tmp_path, headers):
    with scout_client(make_app(store, tmp_path)) as client:
        for method, path in write_routes(client.app):
            response = client.request(method, path, headers=headers, json={"by": "me"})
            assert response.status_code == 401, f"{method} {path}"
            assert response.headers["www-authenticate"] == "Bearer"


def test_read_only_refuses_every_write_even_with_the_token(store, tmp_path):
    with scout_client(make_app(store, tmp_path, read_only=True)) as client:
        for method, path in write_routes(client.app):
            response = client.request(method, path, headers=AUTH, content=b"not json")
            assert response.status_code == 403, f"{method} {path}"
            assert response.json()["detail"] == READ_ONLY_REFUSAL
        assert client.get("/api/events").status_code == 200  # reads still work


def test_read_only_is_the_default(store, tmp_path):
    from argus_scout.config import parse_config
    from argus_scout.server.app import create_app

    app = create_app(parse_config({}, tmp_path), store=store, run_scheduler=False)
    assert unguarded_routes(app, read_only=True) == []
    with scout_client(app) as client:
        assert client.post("/api/recommendations/r1/approve", headers=AUTH, json={"by": "me"}).status_code == 403


def test_untrusted_origin_is_refused(store, tmp_path):
    trusted = "https://ui.example"
    with scout_client(make_app(store, tmp_path, allowed_origins=[trusted])) as client:
        for method, path in write_routes(client.app):
            response = client.request(method, path, headers={**AUTH, "Origin": "https://evil.example"}, json={})
            assert response.status_code == 403, f"{method} {path}"
            assert "cross-site" in response.json()["detail"]
            # A trusted origin gets past the guard to the route (which then 404s or validates).
            response = client.request(method, path, headers={**AUTH, "Origin": trusted}, json={"by": "me"})
            assert response.status_code != 403, f"{method} {path}"


def test_same_host_dns_name_origin_is_refused(store, tmp_path):
    """A DNS-rebound page sends its own name as Origin and Host: only IP literals, localhost and trusted_hosts pass.
    The HostGuard refuses it (400) before the origin guard; with the name trusted, another origin is still a 403."""
    rebound = {**AUTH, "Origin": "http://scout.example:8400"}
    with scout_client(make_app(store, tmp_path), base_url="http://scout.example:8400") as client:
        response = client.post("/api/recommendations/r1/approve", headers=rebound, json={})
        assert response.status_code == 400
    app = make_app(store, tmp_path, trusted_hosts=["scout.example"])
    with scout_client(app, base_url="http://scout.example:8400") as client:
        other = {**AUTH, "Origin": "http://evil.example:8400"}
        assert client.post("/api/recommendations/r1/approve", headers=other, json={}).status_code == 403
        assert client.post("/api/recommendations/r1/approve", headers=rebound, json={}).status_code == 422


def test_walker_flags_a_host_guard_that_is_not_outermost(store, tmp_path):
    from argus_scout.budget import ManualOriginMiddleware

    app = make_app(store, tmp_path)
    assert unguarded_routes(app) == []
    app.add_middleware(ManualOriginMiddleware)  # now outside the HostGuard
    assert unguarded_routes(app) == ["app: HostGuard is not the outermost middleware"]


def test_oversized_bodies_are_413(store, tmp_path):
    with scout_client(make_app(store, tmp_path)) as client:
        for method, path in write_routes(client.app):
            cap = EVENTS_MAX_BYTES if path == "/api/events/hook" else ACTION_MAX_BYTES
            response = client.request(method, path, headers=AUTH, content=b" " * (cap + 1))
            assert response.status_code == 413, f"{method} {path}"


def test_oversized_chunked_body_is_413(store, tmp_path):
    """No Content-Length: read_body counts the stream and stops at the cap."""

    def chunks():
        for _ in range(ACTION_MAX_BYTES // 1024 + 2):
            yield b" " * 1024

    with scout_client(make_app(store, tmp_path)) as client:
        response = client.post("/api/recommendations/r1/approve", headers=AUTH, content=chunks())
        assert response.status_code == 413


def test_bad_content_length_is_400(store, tmp_path):
    with scout_client(make_app(store, tmp_path)) as client:
        headers = {**AUTH, "Content-Length": "abc"}
        assert client.post("/api/recommendations/r1/approve", headers=headers).status_code == 400


@pytest.mark.parametrize("read_only", [True, False])
def test_the_host_guard_covers_every_route(store, tmp_path, read_only):
    """Every route the walk finds, every method, read or write: a DNS-rebinding Host is a 400 before anything else
    (read-only, the origin guard, auth). Only HOST_OPEN_PATHS answer it."""
    app = make_app(store, tmp_path, read_only=read_only)
    requests = [
        (method, re.sub(r"\{(\w+)(:\w+)?\}", lambda m: PARAMS[m.group(1)], view.path))
        for view in iter_routes(app)
        for method in sorted(view.methods - {"HEAD"})
    ]
    assert len(requests) > 10
    with scout_client(app, base_url="http://rebound.example:8400") as client:
        for method, path in requests:
            response = client.request(method, path, headers={**AUTH, "Origin": "http://rebound.example:8400"})
            if path in HOST_OPEN_PATHS:
                assert response.status_code == 200, path
            else:
                assert response.status_code == 400, f"{method} {path}"
                assert response.json()["detail"] == HOST_REFUSAL


def test_auth_runs_before_the_body_is_read(store, tmp_path):
    """An unauthenticated caller gets 401 even for an oversized body: scout never buffers it."""
    with scout_client(make_app(store, tmp_path)) as client:
        for method, path in write_routes(client.app):
            assert client.request(method, path, content=b" " * (EVENTS_MAX_BYTES + 1)).status_code == 401, path


# --- reads ---------------------------------------------------------------------------------------------------

READS = [
    "/api/events",
    "/api/recommendations",
    "/api/approvals/rules",
    "/api/reports/latest",
    "/api/policy/explain",
    "/wall",
]


def test_reads_are_open_by_default(store, tmp_path):
    with scout_client(make_app(store, tmp_path)) as client:
        for path in READS:
            assert client.get(path).status_code in (200, 404), path


def test_read_auth_closes_the_read_api(store, tmp_path, monkeypatch):
    with scout_client(make_app(store, tmp_path, read_auth=True)) as client:
        for path in READS:
            assert client.get(path).status_code == 401, path
            assert client.get(path, headers=AUTH).status_code in (200, 404), path
        # Liveness and scrape targets stay open.
        assert client.get("/health").status_code == 200
        assert client.get("/metrics").status_code == 200
        monkeypatch.delenv("SCOUT_API_TOKEN")
        assert client.get("/wall", headers=AUTH).status_code == 503  # fails closed


def test_every_get_route_is_known(store, tmp_path):
    """A new read route must be added to READS (and so to the read_auth test) or to the always-open list."""
    app = make_app(store, tmp_path)
    always_open = {"/health", "/metrics", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    gets = {v.path for v in iter_routes(app) if "GET" in v.methods}
    assert gets - always_open == set(READS) | {"/api/recommendations/{rec_id}"}


# --- filesystem paths ----------------------------------------------------------------------------------------

FS_APIS = re.compile(
    r"\bopen\(|\bPath\(|pathlib|os\.path|FileResponse|StaticFiles|shutil|\.(?:read|write)_(?:text|bytes)"
    r"|\bos\.(?:remove|unlink|rename|replace|listdir|scandir|walk)\b"
)


def test_no_route_touches_the_filesystem(store, tmp_path):
    """No route takes a filesystem path: nothing is mounted, and the route modules use no file API. A route that
    needs one must fence the caller's path under a configured root (argus_cortex.fs) and add a test for it."""
    app = make_app(store, tmp_path)
    assert not [v for v in iter_routes(app) if isinstance(v.route, Mount)]
    modules = [SERVER_SRC / "guards.py", SERVER_SRC / "deps.py", *sorted((SERVER_SRC / "routes").glob("*.py"))]
    assert len(modules) > 3
    for module in modules:
        hits = [m.group(0) for m in FS_APIS.finditer(module.read_text())]
        assert not hits, f"{module.name} uses a filesystem API ({hits}): fence caller-supplied paths under a root"
