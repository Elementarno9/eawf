"""EAWF012 — reject provenance breadcrumbs in source comments.

Source comments should explain why code behaves the way it does. They
should not carry implementation provenance such as audit IDs, operator
decision references, or "per Codex" style notes. That provenance belongs
in state, commits, and durable artifacts, where it remains searchable
without turning code comments into a change log.
"""

from __future__ import annotations

import io
import re
import tokenize
from dataclasses import dataclass

RULE_CODE = "EAWF012"

_PROVENANCE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bper\s+Q\d+\b", re.IGNORECASE), "operator Q reference"),
    (re.compile(r"\bper\s+audit\s+[A-Z]{1,6}\d+\b", re.IGNORECASE), "audit reference"),
    (re.compile(r"\bper\s+Codex\b", re.IGNORECASE), "Codex provenance"),
    (re.compile(r"\bper\s+Claude\b", re.IGNORECASE), "Claude provenance"),
    (re.compile(r"\bper\s+Decision\b", re.IGNORECASE), "decision provenance"),
    (re.compile(r"\boperator[- ]decision[- ]id\b", re.IGNORECASE), "operator decision id"),
    (re.compile(r"\broundtable\b", re.IGNORECASE), "roundtable provenance"),
)


@dataclass(frozen=True)
class DesignProvenanceViolation:
    """One EAWF012 finding."""

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


def _comment_tokens(source: str) -> list[tokenize.TokenInfo]:
    reader = io.StringIO(source).readline
    try:
        return [tok for tok in tokenize.generate_tokens(reader) if tok.type == tokenize.COMMENT]
    except tokenize.TokenError:
        return []


def check_source(source: str) -> list[DesignProvenanceViolation]:
    """Return EAWF012 violations for Python source comments.

    Args:
        source: Python source text to inspect.

    Returns:
        Violations in source order. Docstrings and string literals are
        not scanned because this rule is about comments only.
    """
    violations: list[DesignProvenanceViolation] = []
    for tok in _comment_tokens(source):
        for pattern, reason in _PROVENANCE_PATTERNS:
            match = pattern.search(tok.string)
            if match is None:
                continue
            violations.append(
                DesignProvenanceViolation(
                    lineno=tok.start[0],
                    col_offset=tok.start[1],
                    snippet=match.group(0),
                    reason=f"source comment carries {reason}",
                )
            )
            break
    return violations
