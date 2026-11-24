"""The ``allow_hosts`` entry for a configured URL, shared by the sources, the channels and the Prometheus client."""

from __future__ import annotations

from urllib.parse import urlsplit


def allow_host(url: str) -> str:
    """The ``host:port`` entry for ``allow_hosts`` that lets a fetch reach the configured URL itself."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
