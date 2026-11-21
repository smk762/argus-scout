"""The narrator's persona: how scout sounds, never what it may do.

A persona is a small YAML file (``files.persona``) validated by :class:`Persona`: a name, a one-line voice,
style notes, do and don't lists, a sign-off, and how to write text that will be spoken. The package ships a
neutral default (the field defaults below, mirrored with comments in ``examples/persona.yaml``); a
deployment brings its own.

A persona shapes tone only. :func:`system_prompt` always starts with scout's own rules (advisory only,
use only the facts given, don't re-argue rejections, lead with the most severe item and never call things
nominal while warnings or criticals are open, greet for the report kind), which no persona can remove or override.

:class:`PersonaFile` re-reads the file when it changes. **A bad persona file degrades to the default and
logs**: a typo in a persona never stops a briefing.
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = structlog.get_logger()

MAX_PERSONA_BYTES = 64 * 1024

RULES = (
    "You write reports for a homelab operator from the facts you are given.",
    "You are advisory only: you describe and recommend. You never take, start or schedule an action, and you "
    "never say that you have. Only a person acts on a recommendation.",
    "Use only the facts below. If a fact isn't listed, you don't know it: never invent numbers, hosts or events.",
    "Rejected recommendations were declined by a person, for the reason quoted. Do not propose them again or "
    "argue with the reason.",
    "Operator notes are the operator's own words: respect them, don't repeat them back.",
    "Lead with the most severe item. When the facts list any warning or critical item, never call things nominal, "
    "fine, normal, calm or quiet, and never say there are no crises or nothing to report. You may say the rest is "
    "fine only after naming every warning and critical item.",
    "The facts give the local time and which report this is. If you greet, greet for the report, not the clock: a "
    "morning briefing is a morning briefing whatever the time. Never guess the time of day.",
    "These rules come before the persona below. Where its voice, style, catchphrases or sign-off conflict with "
    "them, follow the rules.",
)
"""Scout's own rules, first in every system prompt. A persona cannot remove them, and the last rule says they win
over the persona's style. :func:`argus_scout.narrative.ground_persona` also withholds persona lines that would
contradict a warning or critical report, and the answer check refuses one that does."""


class SpeakableStyle(BaseModel):
    """How to write text that may be read aloud by a voice channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_sentence_words: int = Field(default=25, gt=0)
    spell_out_symbols: bool = True
    """Write "percent", "degrees" and "kilowatt hours" rather than %, ° and kWh."""
    no_markdown: bool = True
    """No headings, bullets, tables or links: they read badly aloud."""


class Persona(BaseModel):
    """A persona file. Every field has a neutral default, so a file need only set what it changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(default="Scout", min_length=1, max_length=60)
    voice: str = Field(default="Calm, plain and brief, like a colleague handing over a shift.", max_length=300)
    style: list[str] = Field(
        default_factory=lambda: [
            "Lead with what needs attention; say when nothing does.",
            "Short sentences. Concrete numbers from the facts, with units.",
        ]
    )
    do: list[str] = Field(
        default_factory=lambda: [
            "Order items by severity, most severe first.",
            "Say what a recommendation would change and why, in one sentence.",
        ]
    )
    dont: list[str] = Field(
        default_factory=lambda: [
            "Don't use filler, jokes or exclamation marks.",
            "Don't speculate beyond the facts.",
        ]
    )
    sign_off: str = Field(default="", max_length=120)
    speakable: SpeakableStyle = SpeakableStyle()

    def system_prompt(self) -> str:
        """Scout's rules, then this persona's voice and style."""
        lines = ["Rules:", *(f"- {rule}" for rule in RULES), "", f"You are {self.name}. Voice: {self.voice}"]
        if self.style:
            lines += ["Style:", *(f"- {s}" for s in self.style)]
        if self.do:
            lines += ["Do:", *(f"- {s}" for s in self.do)]
        if self.dont:
            lines += ["Don't:", *(f"- {s}" for s in self.dont)]
        speak = self.speakable
        lines.append(
            f"Write plain prose that reads well aloud: sentences of at most {speak.max_sentence_words} words"
            + ("; spell out symbols and units" if speak.spell_out_symbols else "")
            + ("; no markdown, bullets or links" if speak.no_markdown else "")
            + "."
        )
        if self.sign_off:
            lines.append(f"End with: {self.sign_off}")
        return "\n".join(lines)


DEFAULT_PERSONA = Persona()


def load_persona(path: str | Path) -> Persona:
    """Load and validate a persona file; raises on anything wrong (see :class:`PersonaFile` for the forgiving
    version)."""
    path = Path(path)
    if path.stat().st_size > MAX_PERSONA_BYTES:
        raise ValueError(f"persona file is larger than {MAX_PERSONA_BYTES} bytes")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return DEFAULT_PERSONA
    if not isinstance(data, dict):
        raise ValueError(f"expected a mapping, got {type(data).__name__}")
    return Persona.model_validate(data)


class PersonaFile:
    """A persona backed by an optional file, re-read when its mtime changes.

    :meth:`get` never raises: no path means the default; a missing, unreadable or invalid file means the
    default, with ``error`` saying why (logged once per change).
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self.error: str | None = None
        self._mtime: float | None = None
        self._cached = DEFAULT_PERSONA

    def get(self) -> Persona:
        if self.path is None:
            return DEFAULT_PERSONA
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            if self.error != str(exc):
                log.warning("persona.unavailable", path=str(self.path), error=str(exc))
            self.error, self._mtime, self._cached = str(exc), None, DEFAULT_PERSONA
            return DEFAULT_PERSONA
        if mtime == self._mtime:
            return self._cached
        self._mtime = mtime
        try:
            self._cached, self.error = load_persona(self.path), None
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
            message = str(exc)
            if isinstance(exc, ValidationError):
                message = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            log.warning("persona.invalid_using_default", path=str(self.path), error=message)
            self._cached, self.error = DEFAULT_PERSONA, message
        return self._cached


__all__ = ["DEFAULT_PERSONA", "RULES", "Persona", "PersonaFile", "SpeakableStyle", "load_persona"]
