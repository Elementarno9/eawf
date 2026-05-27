"""EAWF013 — keep numeric citation brackets attached to claims.

Rendered artifacts use dense numeric citations. A citation marker belongs
immediately before sentence punctuation on the same physical line as the
claim it supports, for example ``claim [1].``. Detached markers such as
``claim. [1]`` or a standalone ``[1]`` line are hard to audit and often
come from manual wrapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RULE_CODE = "EAWF013"

_CITATION = r"\[(?:\d+)(?:,\s*\d+)*\]"
_AFTER_PUNCTUATION = re.compile(rf"[.!?;:]\s+{_CITATION}")
_ORPHANED = re.compile(rf"^\s*{_CITATION}(?:\s|$)")
_SPACED_FROM_PUNCTUATION = re.compile(rf"{_CITATION}\s+[.!?;:]")
_REFERENCE_ROW = re.compile(r"^\s*(?:[-*]\s+)?\[\d+\](?::|\s+`|\s+https?://|\s+\.)")


@dataclass(frozen=True)
class BracketPositionViolation:
    """One EAWF013 finding."""

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


def _iter_markdown_content(source: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        rows.append((lineno, line))
    return rows


def _is_reference_row(line: str) -> bool:
    return bool(_REFERENCE_ROW.match(line))


def check_source(source: str) -> list[BracketPositionViolation]:
    """Return EAWF013 violations for Markdown citation markers.

    Args:
        source: Markdown text to inspect.

    Returns:
        Violations in source order. Reference rows and fenced code blocks
        are ignored.
    """
    violations: list[BracketPositionViolation] = []
    for lineno, line in _iter_markdown_content(source):
        if _is_reference_row(line):
            continue
        checks = (
            (_ORPHANED, "citation marker is detached from its claim"),
            (_AFTER_PUNCTUATION, "citation marker must precede sentence punctuation"),
            (_SPACED_FROM_PUNCTUATION, "citation marker must touch sentence punctuation"),
        )
        for pattern, reason in checks:
            match = pattern.search(line)
            if match is None:
                continue
            violations.append(
                BracketPositionViolation(
                    lineno=lineno,
                    col_offset=match.start(),
                    snippet=match.group(0),
                    reason=reason,
                )
            )
            break
    return violations
