"""Plain-file memory: operator notes read-only, scout's own file only, every path fenced under the root."""

from __future__ import annotations

import errno
import os
from datetime import UTC, datetime

import pytest
from structlog.testing import capture_logs

from argus_scout.engine import Rejection
from argus_scout.memory import MAX_NOTE_BYTES, MAX_NOTES, SCOUT_FILE, Memory, MemoryPathError, is_note
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
    (root / f".{SCOUT_FILE}.tmp").symlink_to(victim)
    memory = Memory(root)
    for _ in range(2):  # replaced, not raised, and writing carries on
        assert memory.write_rejections([REJECTION]) == root / SCOUT_FILE
    assert victim.read_text() == "keep me"
    assert not (root / SCOUT_FILE).is_symlink() and "Recent rejections" in (root / SCOUT_FILE).read_text()
    assert not (root / f".{SCOUT_FILE}.tmp").exists()


def test_creates_root_and_no_root_means_no_memory(tmp_path):
    assert Memory(tmp_path / "new").write_rejections([]) == tmp_path / "new" / SCOUT_FILE
    assert Memory(None).write_rejections([REJECTION]) is None
    assert Memory(None).notes() == []
    with pytest.raises(MemoryPathError):
        Memory(None).resolve("x.md")


# --- which files are notes -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["README.md", "readme.md", "Readme.MD", "_draft.md", "_.md", SCOUT_FILE])
def test_readme_and_underscore_files_are_not_notes(root, name):
    (root / name).write_text("for people, not the prompt")
    (root / "prefs.md").write_text("mine")
    assert [n.name for n in Memory(root).notes()] == ["prefs.md"]
    assert not is_note(name) and is_note("prefs.md") and is_note("my_readme.md") and is_note("README-ops.md")


def test_skipped_files_do_not_use_up_the_note_cap(root):
    for i in range(MAX_NOTES):
        (root / f"_{i:02}.md").write_text("set aside")
    (root / "z.md").write_text("real")
    assert [n.name for n in Memory(root).notes()] == ["z.md"]


# --- not writing ---------------------------------------------------------------------------------------------


def test_write_rejections_off_writes_nothing(root):
    memory = Memory(root, write_rejections=False)
    assert memory.write_rejections([REJECTION]) is None
    assert not memory.check_writable()
    assert list(root.iterdir()) == []


def test_an_unwritable_directory_is_one_warning_then_left_alone(root, monkeypatch):
    memory = Memory(root)
    calls = []

    def refuse(rejections):
        calls.append(1)
        raise PermissionError(errno.EROFS, "Read-only file system", str(root))

    monkeypatch.setattr(memory, "_write", refuse)
    with capture_logs() as logs:
        assert memory.write_rejections([REJECTION]) is None
        assert memory.write_rejections([REJECTION]) is None
        assert memory.write_rejections([REJECTION]) is None
    assert len(calls) == 1  # never tried again
    assert [entry["event"] for entry in logs] == ["memory.not_writable"]


def test_other_write_errors_still_raise(root, monkeypatch):
    memory = Memory(root)

    def full(rejections):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(memory, "_write", full)
    with pytest.raises(OSError, match="No space"):
        memory.write_rejections([REJECTION])
    assert memory.writes  # a full disk may clear: keep trying


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user for permissions")
def test_startup_check_catches_a_read_only_directory(root):
    root.chmod(0o555)
    try:
        memory = Memory(root)
        with capture_logs() as logs:
            assert memory.check_writable() is False
            assert memory.write_rejections([REJECTION]) is None
        assert [entry["event"] for entry in logs] == ["memory.not_writable"]
        assert list(root.iterdir()) == []
    finally:
        root.chmod(0o755)
    assert Memory(root / "not-yet").check_writable()  # created on first write, under a writable parent
