"""EAWF015 — advisory EARS requirement-shape lint."""

from __future__ import annotations

import re
from dataclasses import dataclass

RULE_CODE = "EAWF015"

_REQUIREMENT_LANGUAGE = re.compile(
    r"\b(shall|should|must|needs?\s+to|required|requirement)\b",
    re.IGNORECASE,
)
_EARS_SHAPE = re.compile(
    r"^\s*(?:[-*]\s+)?"
    r"(?:(?:when|while|where)\b.+,\s*)?"
    r"(?:(?:if)\b.+,\s*then\s*)?"
    r"(?:the\s+)?[A-Za-z][A-Za-z0-9 _/-]{0,80}\s+shall\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EarsAdvisory:
    """One EAWF015 advisory finding."""

    lineno: int
    col_offset: int
    snippet: str
    reason: str

    @property
    def code(self) -> str:
        """Return the rule code."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``line:col: CODE reason`` style one-liner body."""
        return f"{self.lineno}:{self.col_offset}: {RULE_CODE} {self.reason}: {self.snippet!r}"


def check_source(source: str) -> list[EarsAdvisory]:
    """Return non-blocking EAWF015 advisories for Markdown requirements.

    Args:
        source: Markdown text to inspect.

    Returns:
        Advisory findings in source order. Fenced code blocks are ignored.
    """
    advisories: list[EarsAdvisory] = []
    in_fence = False
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _REQUIREMENT_LANGUAGE.search(line)
        if match is None:
            continue
        if _EARS_SHAPE.match(line):
            continue
        advisories.append(
            EarsAdvisory(
                lineno=lineno,
                col_offset=match.start(),
                snippet=stripped[:100],
                reason="requirement-like prose is not in an EARS 'the system shall' shape",
            )
        )
    return advisories
