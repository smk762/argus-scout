"""The signals file: which rule kinds it holds, and loading it.

``kind:`` picks a rule's schema; without it a rule is ``threshold``
(:class:`~argus_scout.signals.SignalRule`). ``tariff_shift`` and ``tariff_plan`` are the tariff-aware kinds
(:mod:`argus_scout.tariff_rules`). This module sits above both, so neither imports it.

**Bad rules degrade.** An invalid rule is skipped and reported; the others still load. The file is
re-read when it changes (:class:`SignalRulesFile`), and a file that won't parse at all gives no rules
plus an error, never a crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import ValidationError

from argus_scout.signals import RuleBase, SignalRule
from argus_scout.tariff_rules import TariffPlanRule, TariffShiftRule

log = structlog.get_logger()


def rule_kinds() -> dict[str, type[RuleBase]]:
    """``kind`` -> rule class."""
    return {"threshold": SignalRule, "tariff_shift": TariffShiftRule, "tariff_plan": TariffPlanRule}


@dataclass
class RuleSet:
    """What a rules file loaded to: the good rules, and why any were skipped."""

    rules: list[SignalRule | TariffShiftRule | TariffPlanRule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    file_error: str | None = None


def _describe(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if p != "for_") or "rule"
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {'unknown key' if err['type'] == 'extra_forbidden' else msg}")
    return "; ".join(parts)


def parse_rules(data: Any) -> RuleSet:
    """Rules from parsed YAML (``{rules: [...]}``). A bad rule is skipped with an error; the rest load."""
    result = RuleSet()
    if data is None:
        return result
    if not isinstance(data, dict):
        result.file_error = "expected a mapping with a 'rules' list"
        return result
    unknown = sorted(set(data) - {"rules"})
    if unknown:
        result.errors.append(f"unknown top-level keys ignored: {', '.join(map(str, unknown))}")
    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        result.file_error = "'rules' must be a list"
        return result
    seen: set[str] = set()
    kinds = rule_kinds()
    for i, raw in enumerate(raw_rules):
        label = f"rule {i}"
        if isinstance(raw, dict) and raw.get("name"):
            label = f"rule {i} ({raw['name']})"
        if not isinstance(raw, dict):
            result.errors.append(f"{label}: expected a mapping")
            continue
        kind = raw.get("kind", "threshold")
        model = kinds.get(kind) if isinstance(kind, str) else None
        if model is None:
            result.errors.append(f"{label}: kind: unknown kind {kind!r} (one of {', '.join(kinds)})")
            continue
        try:
            rule = model.model_validate(raw)
        except ValidationError as exc:
            result.errors.append(f"{label}: {_describe(exc)}")
            continue
        if rule.name in seen:
            result.errors.append(f"{label}: duplicate name; the first one is kept")
            continue
        seen.add(rule.name)
        result.rules.append(rule)  # type: ignore[arg-type]
    return result


def load_signal_rules(path: str | Path) -> RuleSet:
    """Rules from a YAML file. Never raises for bad content: it lands in ``file_error`` / ``errors``."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        return RuleSet(file_error=str(exc))
    return parse_rules(data)


class SignalRulesFile:
    """Signal rules backed by a YAML file, re-read when its mtime changes (as :class:`PolicyFile` does).

    A missing file gives no rules. A bad edit gives the rules that still parse; a file that fails to
    parse at all gives none, with ``file_error`` set. Neither stops scout.
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = str(path) if path else ""
        self._mtime: float | None = None
        self._cached = RuleSet()

    def get(self) -> RuleSet:
        if not self.path:
            return RuleSet()
        try:
            mtime = Path(self.path).stat().st_mtime
        except OSError:
            self._mtime, self._cached = None, RuleSet()
            return self._cached
        if mtime == self._mtime:
            return self._cached
        ruleset = load_signal_rules(self.path)
        if ruleset.file_error:
            log.warning("signals.load_failed", path=self.path, error=ruleset.file_error)
        for error in ruleset.errors:
            log.warning("signals.bad_rule", path=self.path, error=error)
        log.info("signals.rules_loaded", path=self.path, count=len(ruleset.rules))
        self._mtime, self._cached = mtime, ruleset
        return ruleset


__all__ = ["RuleSet", "SignalRulesFile", "load_signal_rules", "parse_rules", "rule_kinds"]
