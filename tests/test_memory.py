"""Plain-file memory: operator notes read-only, scout's own file only, every path fenced under the root."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from argus_scout.engine import Rejection
from argus_scout.memory import MAX_NOTE_BYTES, SCOUT_FILE, Memory, MemoryPathError
from argus_scout.severity import Severity

REJECTION = Rejection("r1", "k", "Move backups to off-peak", "  backups must finish before 06:00 ", Severity.NOTICE,
                      datetime(2026, 6, 1, tzinfo=UTC))  # fmt: skip


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "memory"
    path.mkdir()
    return path


def test_reads_operator_notes(root):
    (root / "prefs.md").write_text("Quote costs per month.\n")
    (root / "empty.md").write_text("   \n")
    (root / "other.txt").write_text("not a note")
    (root / "big.md").write_text("y" * (MAX_NOTE_BYTES + 100))
    notes = {n.name: n.text for n in Memory(root).notes()}
    assert notes.keys() == {"big.md", "prefs.md"}
    assert notes["prefs.md"] == "Quote costs per month."
    assert len(notes["big.md"]) == MAX_NOTE_BYTES


@pytest.mark.parametrize("name", ["../escape.md", "/etc/passwd", "a/../../escape.md", ""])
def test_traversal_is_refused(root, name):
    with pytest.raises(MemoryPathError):
        Memory(root).resolve(name)


def test_symlink_out_of_root_is_refused(root, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("outside the root")
    (root / "link.md").symlink_to(secret)
    (root / "dirlink").symlink_to(tmp_path)
    memory = Memory(root)
    with pytest.raises(MemoryPathError):
        memory.resolve("link.md")
    with pytest.raises(MemoryPathError):
        memory.resolve("dirlink/secret.md")
    assert memory.notes() == []  # the symlinked note is skipped, never read


def test_symlink_inside_root_is_allowed(root):
    (root / "real.md").write_text("inside")
    (root / "alias.md").symlink_to(root / "real.md")
    assert [n.name for n in Memory(root).notes()] == ["alias.md", "real.md"]


def test_scout_writes_only_its_own_file(root):
    (root / "prefs.md").write_text("mine")
    memory = Memory(root)
    path = memory.write_rejections([REJECTION])
    assert path == root / SCOUT_FILE
    text = path.read_text()
    assert '"Move backups to off-peak" (notice), rejected 2026-06-01: reason "backups must finish before 06:00"' in text
    assert (root / "prefs.md").read_text() == "mine"
    assert sorted(p.name for p in root.iterdir()) == ["prefs.md", SCOUT_FILE]
    assert [n.name for n in memory.notes()] == ["prefs.md"]  # its own file isn't read back as a note


def test_scout_file_symlinked_out_is_never_written_through(root, tmp_path):
    victim = tmp_path / "victim.md"
    victim.write_text("keep me")
    (root / SCOUT_FILE).symlink_to(victim)
    with pytest.raises(MemoryPathError):
        Memory(root).write_rejections([REJECTION])
    assert victim.read_text() == "keep me"


def test_creates_root_and_no_root_means_no_memory(tmp_path):
    assert Memory(tmp_path / "new").write_rejections([]) == tmp_path / "new" / SCOUT_FILE
    assert Memory(None).write_rejections([REJECTION]) is None
    assert Memory(None).notes() == []
    with pytest.raises(MemoryPathError):
        Memory(None).resolve("x.md")
