from __future__ import annotations

import ast
import functools
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from argus_cortex.net import Reply, UrlRefused, afetch

from argus_scout.prometheus import (
    PrometheusClient,
    PrometheusQueryError,
    PrometheusResponseError,
    PrometheusUnavailable,
    parse_matrix,
    parse_vector,
)

BASE = "http://prometheus.lan:9090"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
SRC = Path(__file__).resolve().parent.parent / "src" / "argus_scout"


def body(doc: object) -> bytes:
    return json.dumps(doc).encode()


def vector(*items: tuple[dict, str]) -> bytes:
    return body(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": m, "value": [T0.timestamp(), v]} for m, v in items],
            },
        }
    )


class Recorder:
    """A stub fetcher: records each call and answers from a queue (or one fixed reply)."""

    def __init__(self, *replies: Reply | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url: str, **kwargs) -> Reply:
        self.calls.append((url, kwargs))
        reply = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def ok(raw: bytes, status: int = 200) -> Reply:
    return Reply(status, {}, raw, BASE)


# --- parsing ------------------------------------------------------------------------------------------------


def test_parse_vector_labels_values_timestamps():
    samples = parse_vector(vector(({"__name__": "up", "instance": "node-a:9100"}, "1"), ({"instance": "b"}, "NaN")))
    assert samples[0].labels == {"__name__": "up", "instance": "node-a:9100"}
    assert samples[0].metric == "up"
    assert samples[0].value == 1.0
    assert samples[0].timestamp == T0
    assert math.isnan(samples[1].value)


def test_parse_scalar_is_one_unlabelled_sample():
    [sample] = parse_vector(body({"status": "success", "data": {"resultType": "scalar", "result": [1.5, "+Inf"]}}))
    assert sample.labels == {}
    assert sample.value == math.inf


def test_parse_matrix():
    raw = body(
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": {"instance": "a"}, "values": [[T0.timestamp(), "1"], [T0.timestamp() + 30, "2"]]}
                ],
            },
        }
    )
    [series] = parse_matrix(raw)
    assert series.labels == {"instance": "a"}
    assert series.values == [1.0, 2.0]
    assert series.points[1][0] == T0 + timedelta(seconds=30)


@pytest.mark.parametrize(
    ("raw", "error", "match"),
    [
        (b"<html>", PrometheusResponseError, "not JSON"),
        (b"[]", PrometheusResponseError, "not a JSON object"),
        (body({"status": "success"}), PrometheusResponseError, "no success status"),
        (body({"status": "error", "errorType": "bad_data", "error": "parse error"}), PrometheusQueryError, "bad_data"),
        (
            body({"status": "success", "data": {"resultType": "string", "result": [1, "x"]}}),
            PrometheusResponseError,
            "expected a vector",
        ),
        (
            body({"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1]}]}}),
            PrometheusResponseError,
            "expected \\[timestamp, value\\]",
        ),
        (
            body({"status": "success", "data": {"resultType": "vector", "result": [{"value": [1, "high"]}]}}),
            PrometheusResponseError,
            "bad sample value",
        ),
    ],
)
def test_parse_errors_are_typed(raw, error, match):
    with pytest.raises(error, match=match):
        parse_vector(raw)


def test_matrix_parser_refuses_a_vector():
    with pytest.raises(PrometheusResponseError, match="expected a matrix"):
        parse_matrix(vector(({}, "1")))


# --- the client -----------------------------------------------------------------------------------------------


async def test_query_builds_the_url_and_uses_the_guarded_fetch():
    fetch = Recorder(ok(vector(({"instance": "a"}, "3"))))
    client = PrometheusClient(BASE + "/", fetcher=fetch)
    [sample] = await client.query('up{job="node"}', at=T0)
    assert sample.value == 3.0
    [(url, kwargs)] = fetch.calls
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == BASE
    assert parts.path == "/api/v1/query"
    assert parse_qs(parts.query) == {"query": ['up{job="node"}'], "time": [f"{T0.timestamp():.3f}"]}
    assert kwargs["method"] == "GET"
    assert kwargs["allow_hosts"] == ["prometheus.lan:9090"]
    assert kwargs["max_redirects"] == 0


async def test_query_range_params():
    raw = body({"status": "success", "data": {"resultType": "matrix", "result": []}})
    fetch = Recorder(ok(raw))
    client = PrometheusClient(BASE, fetcher=fetch)
    assert await client.query_range("x", start=T0, end=T0 + timedelta(hours=1), step=timedelta(seconds=30)) == []
    query = parse_qs(urlsplit(fetch.calls[0][0]).query)
    assert query["step"] == ["30"]
    assert urlsplit(fetch.calls[0][0]).path == "/api/v1/query_range"
    with pytest.raises(ValueError, match="before start"):
        await client.query_range("x", start=T0, end=T0 - timedelta(1), step=30)


async def test_base_path_prefix_is_kept():
    fetch = Recorder(ok(vector()))
    await PrometheusClient(BASE + "/prom", fetcher=fetch).query("up")
    assert urlsplit(fetch.calls[0][0]).path == "/prom/api/v1/query"


@pytest.mark.parametrize(
    ("reply", "error", "match"),
    [
        (UrlRefused("could not connect to prometheus.lan"), PrometheusUnavailable, "could not connect"),
        (TimeoutError("timed out"), PrometheusUnavailable, "timed out"),
        (ok(b"", 503), PrometheusUnavailable, "503"),
        (ok(b"", 302), PrometheusUnavailable, "redirect"),
        (
            ok(body({"status": "error", "errorType": "bad_data", "error": "1:5: parse error"}), 400),
            PrometheusQueryError,
            "parse error",
        ),
        (ok(b"<html>bad gateway</html>", 422), PrometheusResponseError, "not JSON"),
        (ok(b"{}", 200), PrometheusResponseError, "no success status"),
    ],
)
async def test_failures_are_typed_and_never_an_empty_result(reply, error, match):
    client = PrometheusClient(BASE, fetcher=Recorder(reply))
    with pytest.raises(error, match=match):
        await client.query("up")


def test_refuses_a_non_http_url():
    with pytest.raises(ValueError):
        PrometheusClient("file:///etc/passwd")


# --- it talks to the configured Prometheus and nothing else ---------------------------------------------------


async def test_only_the_configured_prometheus_is_contacted():
    """The real afetch, stubbed resolver and transport (no network): every request goes to the configured host,
    and a redirect elsewhere is refused before anything is sent there."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("query") == "bounce":
            return httpx.Response(302, headers={"location": "http://node-a.lan:9100/metrics"})
        if request.url.path.endswith("query_range"):
            return httpx.Response(
                200, content=body({"status": "success", "data": {"resultType": "matrix", "result": []}})
            )
        return httpx.Response(200, content=vector(({"instance": "node-a.lan:9100"}, "1")))

    fetcher = functools.partial(afetch, transport=httpx.MockTransport(handler), resolve=lambda host, port: ["10.0.0.9"])
    client = PrometheusClient(BASE, fetcher=fetcher)
    await client.query("up")
    await client.query_range("up", start=T0, end=T0 + timedelta(minutes=5), step=60)
    with pytest.raises(PrometheusUnavailable):
        await client.query("bounce")
    assert len(seen) == 3
    assert {r.headers["host"] for r in seen} == {"prometheus.lan:9090"}
    assert {r.url.path for r in seen} <= {"/api/v1/query", "/api/v1/query_range"}
    assert {r.method for r in seen} == {"GET"}


NETWORK_MODULES = {"httpx", "requests", "aiohttp", "urllib3", "socket", "http.client", "urllib.request", "ssl"}
FETCH_NAMES = {"afetch", "fetch"}
# The only modules allowed to fetch at all: the Prometheus client, and the JSON status poller
# (which GETs status endpoints the config names; it is a source of status events, not a scraper), and the
# webhook channel (which POSTs messages to the one URL its config names; delivery, never a monitored host), and
# the argus-echo synthesiser (which asks the one speech server ``services.echo.url`` names for audio).
# The voice channels fetch their own delivery targets, never a monitored host: argus-mask (its /health and /push,
# at the one URL its config names) and the snapserver's JSON-RPC over HTTP (the one control URL).
FETCHERS = {
    "prometheus.py",
    "sources/json_poll.py",
    "channels/webhook.py",
    "channels/mask.py",
    "channels/snapcast.py",
    "channels/echo.py",
}
# Raw sockets (asyncio streams). Only the snapcast channel, for the snapserver's TCP JSON-RPC control port at the one
# host its config names: a delivery target (the speakers), not a monitored host.
RAW_SOCKET_CALLS = {"open_connection", "create_connection", "open_unix_connection", "start_server", "sock_connect"}
RAW_SOCKET_USERS = {"channels/snapcast.py"}
# WebSocket clients, which bypass argus_cortex.net. Only the mask channel, for argus-mask's source socket
# (``transport: websocket``, the one path that can carry mask's disclosure label for a cloned voice) at the one
# host its config names: a delivery target (a face), not a monitored host. It pins the connection itself: the
# configured host resolved once and checked with ``argus_cortex.net.check_url``, connected to those addresses only,
# no redirects, no proxy, capped messages and timeouts (``tests/test_voice_channels.py``, ``test_mask_ws_*``).
WEBSOCKET_USERS = {"channels/mask.py"}


def network_violations(rel: str, source: str) -> list[str]:
    """How the module at ``rel`` (relative to ``src/argus_scout``) would reach the network beyond what it may.

    Every import form counts: ``import x.y``, ``from x import y`` (checked as ``x`` and ``x.y``, so
    ``from urllib import request`` is ``urllib.request``), ``argus_cortex.net`` reached as ``import argus_cortex.net``,
    ``from argus_cortex import net`` or ``from argus_cortex.net import *``, and any ``.fetch`` / ``.afetch``
    attribute."""
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        fetches = False
        if isinstance(node, ast.Import):
            names = {a.name for a in node.names}
            fetches = any(n == "argus_cortex.net" or n.startswith("argus_cortex.net.") for n in names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            aliases = {a.name for a in node.names}
            names = {module} | {f"{module}.{a}" for a in aliases}
            fetches = (module == "argus_cortex.net" and bool(aliases & (FETCH_NAMES | {"*"}))) or (
                module == "argus_cortex" and bool(aliases & {"net", "*"})
            )
        elif isinstance(node, ast.Attribute):
            names = set()
            fetches = node.attr in FETCH_NAMES
        else:
            continue
        if fetches and rel not in FETCHERS:
            found.append(f"{rel}:{node.lineno} fetches over the network")
        bad = {n for n in names if n in NETWORK_MODULES or n.split(".")[0] in {"httpx", "requests", "aiohttp"}}
        if bad:
            found.append(f"{rel}:{node.lineno} imports {sorted(bad)}")
        if rel not in WEBSOCKET_USERS and {n for n in names if n.split(".")[0] == "websockets"}:
            found.append(f"{rel}:{node.lineno} opens WebSockets")
    if rel not in RAW_SOCKET_USERS:
        calls = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in RAW_SOCKET_CALLS} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in RAW_SOCKET_CALLS
        }
        if calls:
            found.append(f"{rel} opens raw sockets: {sorted(calls)}")
    return found


def test_no_module_contacts_hosts_directly():
    """Scout reads telemetry through PromQL only: no module opens sockets or HTTP clients of its own,
    and only the Prometheus client and the status poller use argus_cortex.net's fetch."""
    found = []
    for path in SRC.rglob("*.py"):
        found += network_violations(path.relative_to(SRC).as_posix(), path.read_text())
    assert found == []


@pytest.mark.parametrize(
    "source",
    [
        "from argus_cortex.net import fetch",
        "from argus_cortex.net import afetch as get",
        "from argus_cortex.net import *",
        "from argus_cortex import net",
        "import argus_cortex.net",
        "client.afetch(url)",
        "from urllib import request",
        "from http import client",
        "import urllib.request",
        "from httpx import AsyncClient",
        "import socket",
        "from websockets import client",
        "await asyncio.open_connection(host, 1705)",
    ],
)
def test_network_check_catches_every_import_form(source):
    assert network_violations("narrative.py", source), source


def test_network_check_allows_what_it_should():
    assert (
        network_violations("narrative.py", "from urllib.parse import urlsplit\nfrom argus_cortex.net import check_url")
        == []
    )
    assert network_violations("prometheus.py", "from argus_cortex.net import afetch\nawait afetch(url)") == []
