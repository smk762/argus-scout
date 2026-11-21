from __future__ import annotations

import os
from pathlib import Path

import pytest


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
