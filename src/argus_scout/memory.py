"""Plain-file memory: a directory of markdown notes folded into briefings.

``files.memory`` names a directory. The operator writes notes in it (``*.md``, one level): preferences,
standing facts, things to ignore. Scout **reads** those and never edits them. Two kinds of ``*.md`` are not
notes and never reach a prompt: ``README.md`` (any case), which is for people browsing the directory, and any
name starting with ``_`` (drafts, templates, things set aside). See :func:`is_note`.

Scout writes one file of its own there, :data:`SCOUT_FILE`, a summary of recent rejections and their reasons,
rewritten whenever a briefing is generated, so the operator can see what scout has been told not to raise
again. ``memory.write_rejections: false`` turns that off (a read-only mount); the reports still quote the
rejections, which come from the store, not this file. If the directory can't be written (checked once at
startup, and on the first write that is refused), scout logs one warning and stops trying.

**Every path stays under the root.** :meth:`Memory.resolve` refuses an absolute name, any ``..`` part,
and anything whose real path (after following symlinks) lands outside the root's real path, so a symlink
in the directory can't make scout read ``/etc/passwd`` or write over a file elsewhere. Scout's own file is
replaced atomically and never written through a symlink.

Notes are capped (per file and in total) before they reach a prompt: small local models have small
context windows. A note that can't be read is skipped and logged; it never stops a briefing.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog

from argus_scout.engine import Rejection

log = structlog.get_logger()

SCOUT_FILE = "scout-recent-rejections.md"
"""The only file scout writes in the memory directory."""
MAX_NOTE_BYTES = 16 * 1024
"""A note larger than this is read only up to here."""
MAX_NOTES = 50


NOT_WRITABLE = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})
"""Write errors that mean the directory can't be written at all: retrying each report would only log again."""


def is_note(name: str) -> bool:
    """Whether a ``*.md`` file in the memory directory is an operator note for the prompt: not ``README.md`` (any
    case), not a name starting with ``_``, and not scout's own :data:`SCOUT_FILE`."""
    return name.lower() != "readme.md" and not name.startswith("_") and name != SCOUT_FILE


def _nearest_existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


class MemoryPathError(ValueError):
    """A path that would leave the memory root."""


@dataclass(frozen=True)
class Note:
    name: str
    text: str


def render_rejection(rejection: Rejection) -> str:
    """One rejection as a line: title, severity, and the reason quoted verbatim."""
    reason = " ".join(rejection.reason.split()) or "no reason given"
    when = rejection.rejected_at.date().isoformat()
    return f'"{rejection.title}" ({rejection.severity.value}), rejected {when}: reason "{reason}"'


class Memory:
    """The memory directory. ``root=None`` means no memory: no notes, nothing written.

    ``write_rejections=False`` means :meth:`write_rejections` never writes. It also stops by itself, with one
    warning, once the directory turns out not to be writable (:meth:`check_writable`, or a refused write)."""

    def __init__(self, root: str | Path | None, *, max_chars: int = 2000, write_rejections: bool = True) -> None:
        self.root = Path(root).expanduser() if root else None
        self.max_chars = max_chars
        self.writes = write_rejections and self.root is not None

    def _stop_writing(self, error: str) -> None:
        self.writes = False
        log.warning("memory.not_writable", root=str(self.root), error=error, file=SCOUT_FILE,
                    hint="scout won't try to write its rejections file again until restarted; "
                    "set memory.write_rejections: false to silence this")  # fmt: skip

    def check_writable(self) -> bool:
        """Whether scout may write its file: probed once (at startup). A directory, or the nearest existing parent
        it would be created in, that this process can't write turns writing off with one warning."""
        if not self.writes or self.root is None:
            return False
        target = _nearest_existing(self.root)
        if not os.access(target, os.W_OK | os.X_OK):
            self._stop_writing(f"{target} is not writable")
        return self.writes

    def resolve(self, name: str) -> Path:
        """The path of ``name`` inside the root, or :class:`MemoryPathError` if it would leave it."""
        if self.root is None:
            raise MemoryPathError("no memory directory configured")
        candidate = Path(name)
        if not name or candidate.is_absolute() or ".." in candidate.parts:
            raise MemoryPathError(f"{name!r} is not a plain name inside the memory directory")
        root = self.root.resolve()
        path = self.root / candidate
        real = path.resolve()
        if not real.is_relative_to(root):
            raise MemoryPathError(f"{name!r} resolves outside the memory directory")
        return path

    def notes(self) -> list[Note]:
        """The operator's notes, by name. Anything unreadable, outside the root or not a file is skipped."""
        if self.root is None or not self.root.is_dir():
            return []
        out: list[Note] = []
        for entry in [e for e in sorted(self.root.glob("*.md")) if is_note(e.name)][:MAX_NOTES]:
            try:
                path = self.resolve(entry.name)
                if not path.is_file():
                    continue
                with path.open("rb") as fh:
                    text = fh.read(MAX_NOTE_BYTES).decode("utf-8", errors="replace")
            except (MemoryPathError, OSError) as exc:
                log.warning("memory.note_skipped", note=entry.name, error=str(exc))
                continue
            text = text.strip()
            if text:
                out.append(Note(entry.name, text))
        return out[:MAX_NOTES]

    def write_rejections(self, rejections: Iterable[Rejection]) -> Path | None:
        """Rewrite scout's own summary of recent rejections. Returns its path, or None when nothing was written: no
        root, ``write_rejections`` off, or the directory not writable (the first refusal is logged, then writing
        stops). Other errors raise.

        Atomic (temp file, then replace), and a symlink at the target is replaced, never written through.
        """
        if self.root is None or not self.writes:
            return None
        try:
            return self._write(rejections)
        except OSError as exc:
            if exc.errno not in NOT_WRITABLE:
                raise
            self._stop_writing(str(exc))
            return None

    def _write(self, rejections: Iterable[Rejection]) -> Path:
        assert self.root is not None
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.resolve(SCOUT_FILE)
        lines = [
            "# Recent rejections",
            "",
            "Written by scout; edits here are overwritten. Scout won't re-propose these or argue with the reasons.",
            "",
        ]
        rendered = [f"- {render_rejection(r)}" for r in rejections]
        lines += rendered or ["- none"]
        tmp = self.resolve(f".{SCOUT_FILE}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        if tmp.is_symlink():
            tmp.unlink()
        fd = os.open(tmp, flags, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
        return path


__all__ = ["MAX_NOTE_BYTES", "SCOUT_FILE", "Memory", "MemoryPathError", "Note", "is_note", "render_rejection"]
