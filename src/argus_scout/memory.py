"""Plain-file memory: a directory of markdown notes folded into briefings.

``files.memory`` names a directory. The operator writes notes in it (``*.md``, one level, any names):
preferences, standing facts, things to ignore. Scout **reads** those and never edits them. Scout writes one
file of its own there, :data:`SCOUT_FILE`, a summary of recent rejections and their reasons, rewritten
whenever a briefing is generated, so the operator can see what scout has been told not to raise again.

**Every path stays under the root.** :meth:`Memory.resolve` refuses an absolute name, any ``..`` part,
and anything whose real path (after following symlinks) lands outside the root's real path, so a symlink
in the directory can't make scout read ``/etc/passwd`` or write over a file elsewhere. Scout's own file is
replaced atomically and never written through a symlink.

Notes are capped (per file and in total) before they reach a prompt: small local models have small
context windows. A note that can't be read is skipped and logged; it never stops a briefing.
"""

from __future__ import annotations

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
    """The memory directory. ``root=None`` means no memory: no notes, nothing written."""

    def __init__(self, root: str | Path | None, *, max_chars: int = 2000) -> None:
        self.root = Path(root).expanduser() if root else None
        self.max_chars = max_chars

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
        for entry in sorted(self.root.glob("*.md"))[: MAX_NOTES + 1]:
            if entry.name == SCOUT_FILE:
                continue
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
        """Rewrite scout's own summary of recent rejections. Returns its path, or None with no root.

        Atomic (temp file, then replace), and a symlink at the target is replaced, never written through.
        """
        if self.root is None:
            return None
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


__all__ = ["MAX_NOTE_BYTES", "SCOUT_FILE", "Memory", "MemoryPathError", "Note", "render_rejection"]
