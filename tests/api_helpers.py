"""Shared setup for the HTTP API tests: a config with push and poll sources, and an app over a given store."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from argus_scout.config import parse_config
from argus_scout.server.app import create_app
from argus_scout.store import SqliteStore

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
TOKEN = "server-secret"
OWN_TOKEN = "hook-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_config(tmp_path: Path, **server: Any):
    return parse_config(
        {
            "sources": [
                {"kind": "webhook", "name": "hook"},  # no token_env: the server token applies
                {"kind": "webhook", "name": "own", "token_env": "OWN_TOKEN"},
                {"kind": "json_poll", "name": "poller", "url": "http://status.example/api"},
            ],
            "server": {"read_only": False, **server},
        },
        tmp_path,
    )


def make_app(store: SqliteStore, tmp_path: Path, **server: Any):
    return create_app(make_config(tmp_path, **server), store=store, clock=lambda: NOW, run_scheduler=False)


BASE_URL = "http://127.0.0.1:8400"
"""The host the API tests reach scout by: an IP literal, which the HostGuard lets through (``testserver``, the
TestClient default, is a DNS name it refuses)."""


def scout_client(app, **kw: Any):
    """A TestClient reaching ``app`` by :data:`BASE_URL` (or the ``base_url`` given)."""
    from fastapi.testclient import TestClient

    kw.setdefault("base_url", BASE_URL)
    return TestClient(app, **kw)
