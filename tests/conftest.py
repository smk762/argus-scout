from __future__ import annotations

import os
from pathlib import Path

import pytest

SUITE_URLS = ("ARGUS_XTTS_URL", "ARGUS_TTS_URL", "ARGUS_MASK_URL")


@pytest.fixture(autouse=True)
def no_suite_settings(monkeypatch):
    """Tests never read a developer's suite settings file (``~/.config/argus/suite.env``) or the suite-wide service
    URLs exported in their shell: a config that leaves a ``url`` out would otherwise validate on one machine only."""
    monkeypatch.setenv("ARGUS_ENV_FILE", "off")
    for name in (*SUITE_URLS, "ARGUS_PEER", "ARGUS_BUILD"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def write_file(tmp_path):
    """Write a file and bump its mtime, so mtime-keyed caches see every write."""

    def write(name: str, text: str) -> Path:
        path = tmp_path / name
        path.write_text(text)
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + len(text) + 1))
        return path

    return write
