"""The shared fixtures keep their promises."""

from __future__ import annotations

import os
from pathlib import Path

COARSE_NS = 1_700_000_000 * 1_000_000_000


def test_write_file_gives_every_rewrite_a_later_mtime(write_file, monkeypatch):
    # A coarse filesystem clock: every write lands on the same mtime, as quick rewrites can on a real one.
    plain = Path.write_text

    def coarse(self, text, *args, **kwargs):
        written = plain(self, text, *args, **kwargs)
        os.utime(self, ns=(COARSE_NS, COARSE_NS))
        return written

    monkeypatch.setattr(Path, "write_text", coarse)
    stamps = [write_file("p.yaml", f"a: {i}").stat().st_mtime_ns for i in range(5)]  # same length every time
    assert all(b > a for a, b in zip(stamps, stamps[1:], strict=False))
