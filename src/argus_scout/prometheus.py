"""A read-only client for the Prometheus HTTP API (PromQL only).

Prometheus is scout's source of truth for telemetry. Scout asks it questions with PromQL
(``/api/v1/query`` and ``/api/v1/query_range``) and never scrapes, probes or connects to the hosts
Prometheus monitors. The only URL this client ever fetches is under the configured ``base_url``:

- every request goes through ``argus_cortex.net.afetch`` with the configured host (and port) as the
  only allowlisted host, so a LAN Prometheus is reachable but nothing else on the LAN is;
- redirects are not followed (``max_redirects=0``), so a misbehaving Prometheus can't bounce a query
  somewhere else;
- only GET is used; there is no write, admin or remote-write path in this module.

**Unknown is not all clear.** Any failure (unreachable, timeout, an error status, a malformed reply)
raises a :class:`PrometheusError`. Callers must treat that as "unknown" and must never read an empty
result into an outage: :func:`argus_scout.signals.evaluate` records the rule as unknown and the engine
does not clear anything on its account.
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from argus_cortex.net import FetchFailed, Reply, UrlRefused, afetch

from argus_scout.sources.json_poll import allow_host

MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_S = 10.0
DEADLINE_S = 30.0
MAX_SERIES = 2000

Fetcher = Callable[..., Awaitable[Reply]]


class PrometheusError(Exception):
    """A query could not be answered. The answer is unknown; it is not "nothing is wrong"."""


class PrometheusUnavailable(PrometheusError):
    """Prometheus could not be reached, timed out, refused the fetch, or answered with a server error."""


class PrometheusQueryError(PrometheusError):
    """Prometheus answered, and rejected the query (``bad_data``, ``execution``, ``timeout``, ...)."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type


class PrometheusResponseError(PrometheusError):
    """Prometheus answered with something that isn't a well-formed API response."""


@dataclass(frozen=True)
class Sample:
    """One point of an instant vector: a series' labels, its value and the evaluation time."""

    labels: Mapping[str, str]
    value: float
    timestamp: datetime

    @property
    def metric(self) -> str:
        return self.labels.get("__name__", "")


@dataclass(frozen=True)
class Series:
    """One series of a range query: its labels and its ``(timestamp, value)`` points, oldest first."""

    labels: Mapping[str, str]
    points: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def values(self) -> list[float]:
        return [v for _, v in self.points]


def _ts(value: Any) -> datetime:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise PrometheusResponseError(f"bad timestamp {value!r}") from exc
    if not math.isfinite(seconds):
        raise PrometheusResponseError(f"bad timestamp {value!r}")
    return datetime.fromtimestamp(seconds, UTC)


def _point(raw: Any) -> tuple[datetime, float]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise PrometheusResponseError(f"expected [timestamp, value], got {raw!r}")
    try:
        value = float(raw[1])  # Prometheus sends values as strings, including "NaN" and "+Inf"
    except (TypeError, ValueError) as exc:
        raise PrometheusResponseError(f"bad sample value {raw[1]!r}") from exc
    return _ts(raw[0]), value


def _labels(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise PrometheusResponseError(f"expected a label set, got {type(raw).__name__}")
    return {str(k): str(v) for k, v in raw.items()}


def _data(body: bytes) -> dict[str, Any]:
    """The ``data`` of a success response; raises the matching error otherwise."""
    try:
        doc = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrometheusResponseError("the response is not JSON") from exc
    if not isinstance(doc, dict):
        raise PrometheusResponseError("the response is not a JSON object")
    if doc.get("status") == "error":
        raise PrometheusQueryError(str(doc.get("errorType") or "error"), str(doc.get("error") or "unknown error"))
    if doc.get("status") != "success" or not isinstance(doc.get("data"), dict):
        raise PrometheusResponseError("the response has no success status and data")
    return doc["data"]


def parse_vector(body: bytes) -> list[Sample]:
    """Samples from an instant query response. A scalar result is one sample with no labels."""
    data = _data(body)
    kind, result = data.get("resultType"), data.get("result")
    if kind == "scalar":
        at, value = _point(result)
        return [Sample({}, value, at)]
    if kind != "vector":
        raise PrometheusResponseError(f"expected a vector or scalar result, got {kind!r}")
    if not isinstance(result, list):
        raise PrometheusResponseError("the vector result is not a list")
    if len(result) > MAX_SERIES:
        raise PrometheusResponseError(f"{len(result)} series is more than {MAX_SERIES}; narrow the query")
    samples = []
    for item in result:
        if not isinstance(item, dict):
            raise PrometheusResponseError("a vector item is not an object")
        at, value = _point(item.get("value"))
        samples.append(Sample(_labels(item.get("metric", {})), value, at))
    return samples


def parse_matrix(body: bytes) -> list[Series]:
    """Series from a range query response."""
    data = _data(body)
    kind, result = data.get("resultType"), data.get("result")
    if kind != "matrix":
        raise PrometheusResponseError(f"expected a matrix result, got {kind!r}")
    if not isinstance(result, list):
        raise PrometheusResponseError("the matrix result is not a list")
    if len(result) > MAX_SERIES:
        raise PrometheusResponseError(f"{len(result)} series is more than {MAX_SERIES}; narrow the query")
    series = []
    for item in result:
        if not isinstance(item, dict) or not isinstance(item.get("values"), list):
            raise PrometheusResponseError("a matrix item is not an object with values")
        series.append(Series(_labels(item.get("metric", {})), [_point(p) for p in item["values"]]))
    return series


def _seconds(value: timedelta | float) -> str:
    seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
    if seconds <= 0:
        raise ValueError("step must be positive")
    return f"{seconds:g}"


class PrometheusClient:
    """Read-only PromQL over HTTP, against the one configured Prometheus.

    ``fetcher`` replaces ``argus_cortex.net.afetch`` (tests pass a stub; nothing else should).
    ``headers`` are sent with every request, for a Prometheus behind an auth proxy.
    """

    def __init__(
        self,
        base_url: str,
        *,
        fetcher: Fetcher | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"{base_url!r} is not an http(s) URL")
        self.base_url = base_url.rstrip("/")
        self.allow_hosts = [allow_host(self.base_url)]
        self._fetch = fetcher or afetch
        self._headers = {"Accept": "application/json", **(headers or {})}
        self._timeout = timeout

    def url(self, path: str, params: Mapping[str, str]) -> str:
        """The full URL for an API path; always under ``base_url``."""
        return f"{self.base_url}/{path.lstrip('/')}?{urlencode(params)}"

    async def _get(self, path: str, params: Mapping[str, str]) -> bytes:
        url = self.url(path, params)
        try:
            reply = await self._fetch(
                url,
                method="GET",
                headers=self._headers,
                allow_hosts=self.allow_hosts,
                timeout=self._timeout,
                deadline=DEADLINE_S,
                max_bytes=MAX_BYTES,
                max_redirects=0,
            )
        except (UrlRefused, OSError) as exc:
            raise PrometheusUnavailable(f"prometheus: {exc}") from exc
        # Prometheus reports a bad query as 400/422 with an error body; parse that for the reason.
        if reply.status in (400, 422):
            _data(reply.body)
            raise PrometheusQueryError("bad_data", f"answered {reply.status}")
        if 300 <= reply.status < 400:
            raise PrometheusUnavailable(f"prometheus: answered a redirect ({reply.status}); redirects are not followed")
        try:
            reply.raise_for_status()
        except FetchFailed as exc:
            raise PrometheusUnavailable(f"prometheus: {exc}") from exc
        return reply.body

    async def query(self, promql: str, *, at: datetime | None = None) -> list[Sample]:
        """An instant query (``/api/v1/query``), evaluated at ``at`` or Prometheus's now."""
        params = {"query": promql}
        if at is not None:
            params["time"] = f"{at.timestamp():.3f}"
        return parse_vector(await self._get("/api/v1/query", params))

    async def query_range(
        self, promql: str, *, start: datetime, end: datetime, step: timedelta | float
    ) -> list[Series]:
        """A range query (``/api/v1/query_range``); ``step`` is a timedelta or seconds."""
        if end < start:
            raise ValueError("end is before start")
        params = {
            "query": promql,
            "start": f"{start.timestamp():.3f}",
            "end": f"{end.timestamp():.3f}",
            "step": _seconds(step),
        }
        return parse_matrix(await self._get("/api/v1/query_range", params))


__all__ = [
    "PrometheusClient",
    "PrometheusError",
    "PrometheusQueryError",
    "PrometheusResponseError",
    "PrometheusUnavailable",
    "Sample",
    "Series",
    "parse_matrix",
    "parse_vector",
]
