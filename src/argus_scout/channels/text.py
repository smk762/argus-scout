"""The ``text`` channel: append each message to a plain-text file, readable with ``tail -f``.

::

    - kind: text
      name: log
      dir: .                      # relative to scout.yaml
      file: scout-{kind}.log      # {kind} and {date} (YYYY-MM-DD, UTC) are filled in per message
      rotate_bytes: 1048576       # rotate before the file grows past 1 MiB; leave out to never rotate
      keep: 3                     # rotated files kept (x.log.1 ... x.log.3)

**Fenced under ``dir``.** ``dir`` is the operator's choice; ``file`` and anything filled into it are not
trusted to stay inside it. Every write resolves the target through ``argus_cortex.fs.safe_join`` (symlinks
followed): a ``..``, an absolute path or a symlink that leads out of ``dir`` is refused, and the file is then
opened with ``O_NOFOLLOW``, so a symlink swapped in at the last component isn't followed either. A kind
filled into ``{kind}`` is reduced to safe characters first.

Message text came from producers or the model, so control and format characters (terminal escapes, bidi
overrides) are stripped before it is written.

The ``/wall`` view doesn't read this file: it reads the latest report from the store, so it works with or
without a text channel.
"""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from datetime import UTC
from pathlib import Path

from argus_cortex.fs import PathRefused, safe_join

from argus_scout.channels.base import ChannelError, Delivery, DeliveryResult
from argus_scout.config import TextChannel as TextChannelConfig

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def clean(text: str) -> str:
    """``text`` with control and format characters removed; newlines kept, tabs as four spaces, and the other
    whitespace controls (``\\r``, ``\\v``, ``\\f``) as one space. Other format characters (soft hyphens, bidi
    marks, zero-width spaces) are dropped, never spaced, so they don't split words; the zero-width joiner and
    non-joiner are kept, since emoji sequences and some scripts need them."""
    return "".join(_clean_char(ch) for ch in text.replace("\r\n", "\n"))


def _clean_char(ch: str) -> str:
    if ch in "\n\u200c\u200d" or not unicodedata.category(ch).startswith("C"):
        return ch
    if ch == "\t":
        return "    "
    return " " if ch in "\r\v\f" else ""


def _segment(value: str) -> str:
    return _UNSAFE.sub("_", value).strip("._") or "unknown"


def target_path(cfg: TextChannelConfig, message: Delivery) -> Path:
    """The file this message goes to, resolved and fenced under ``cfg.dir``, or :class:`ChannelError`."""
    name = cfg.file.format(kind=_segment(message.kind), date=message.generated_at.astimezone(UTC).date().isoformat())
    try:
        return safe_join(cfg.dir, name, label=f"text channel {cfg.name} file")
    except PathRefused as exc:
        raise ChannelError(str(exc)) from exc


def format_entry(message: Delivery) -> str:
    stamp = message.generated_at.astimezone(UTC).isoformat(timespec="seconds")
    header = f"=== {stamp}  {message.kind}  [{message.severity.value}]  {message.key} ==="
    return f"{clean(header)}\n{clean(message.text).rstrip()}\n\n"


def _rotate(path: Path, keep: int) -> None:
    for index in range(keep, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        newer = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        if newer.is_symlink():  # never move a link someone planted; drop it
            newer.unlink()
        elif newer.exists():
            os.replace(newer, older)


def append(cfg: TextChannelConfig, message: Delivery) -> Path:
    """Write ``message`` to its file (rotating first if it would grow past ``rotate_bytes``)."""
    path = target_path(cfg, message)
    root = Path(cfg.dir).resolve()
    data = format_entry(message).encode("utf-8")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # mkdir may have walked a symlinked component created in between: check the parent again.
    if not parent.resolve().is_relative_to(root):
        raise ChannelError(f"text channel {cfg.name} file: is outside its root")
    if cfg.rotate_bytes is not None:
        try:
            size = path.stat(follow_symlinks=False).st_size
        except FileNotFoundError:
            size = 0
        if size and size + len(data) > cfg.rotate_bytes:
            _rotate(path, cfg.keep)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags, 0o640)
    except OSError as exc:
        raise ChannelError(f"text channel {cfg.name}: cannot open {path.name}: {exc.strerror or exc}") from exc
    with os.fdopen(fd, "ab") as fh:
        fh.write(data)
    return path


class TextChannel:
    """The ``text`` channel kind."""

    kind = "text"
    config_model = TextChannelConfig

    def __init__(self, config: TextChannelConfig) -> None:
        self.config = config

    async def deliver(self, message: Delivery) -> DeliveryResult:
        path = await asyncio.to_thread(append, self.config, message)
        return DeliveryResult(ok=True, detail=f"appended to {path.name}")


__all__ = ["TextChannel", "append", "clean", "format_entry", "target_path"]
