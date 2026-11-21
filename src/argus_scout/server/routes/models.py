"""Request bodies for the write routes. Unknown keys are refused, so a typo can't be silently ignored."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

BY_MAX = 100
REASON_MAX = 2000
PATTERN_MAX = 200


def _no_controls(value: str) -> str:
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        raise ValueError("must not contain control or format characters")
    return value


def _newlines(value: str) -> str:
    r"""``\r\n`` and a lone ``\r`` as ``\n``: a textarea or a Windows client sends CRLF."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _no_controls_but_newlines(value: str) -> str:
    _no_controls(value.replace("\n", " ").replace("\t", " "))
    return value


By = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=BY_MAX), AfterValidator(_no_controls)
]
"""Who did it: a person's name or handle, recorded in the audit trail."""
Reason = Annotated[
    str,
    BeforeValidator(lambda v: _newlines(v) if isinstance(v, str) else v),
    StringConstraints(strip_whitespace=True, max_length=REASON_MAX),
    AfterValidator(_no_controls_but_newlines),
]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AckBody(_Body):
    by: By


class DecisionBody(_Body):
    """An approve or reject: who decided, why, and the ``revision`` of the recommendation they viewed (from
    ``GET /api/recommendations/{id}``). Required: a decision is refused (409) if the content changed since."""

    by: By
    reason: Reason = ""
    revision: Annotated[int, Field(ge=0, strict=True)]


def _regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"not a valid regular expression: {exc}") from None
    return value


TitlePattern = Annotated[
    str, StringConstraints(min_length=1, max_length=PATTERN_MAX), AfterValidator(_no_controls), AfterValidator(_regex)
]


class StandingBody(_Body):
    """ "And keep approving things like this": the arguments of
    :meth:`~argus_scout.approvals.StandingApprovals.add_from`, which builds the rule from the approved recommendation
    (its analyzer, its title unless ``title_pattern`` is given, its impact and confidence).

    ``scope: analyzer`` covers matching proposals from this analyzer until the rule is deleted (or until ``until``, if
    given); ``scope: until`` requires ``until``. ``once`` is a plain approve, so it isn't accepted here: omit
    ``standing`` instead. ``max_impact`` widens the rule to ``medium``; ``high`` is never allowed."""

    scope: Literal["analyzer", "until"]
    until: AwareDatetime | None = None
    title_pattern: TitlePattern | None = None
    max_impact: Literal["low", "medium"] | None = None

    @model_validator(mode="after")
    def _until_needed(self) -> StandingBody:
        if self.scope == "until" and self.until is None:
            raise ValueError("scope 'until' needs until")
        return self


class ApproveBody(DecisionBody):
    """An approve, optionally creating a runtime standing rule from the recommendation in the same transaction."""

    standing: StandingBody | None = None


class RuleDeleteBody(_Body):
    """Who deleted a runtime standing rule, for the log."""

    by: By


__all__ = ["AckBody", "ApproveBody", "DecisionBody", "RuleDeleteBody", "StandingBody"]
