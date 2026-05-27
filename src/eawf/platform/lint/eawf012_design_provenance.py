"""EAWF012 — reject provenance breadcrumbs in source comments and docstrings.

Source comments should explain why code behaves the way it does. They
should not carry implementation provenance such as audit IDs, operator
decision references, or tool-attribution notes. That provenance belongs
in state, commits, and durable artifacts, where it remains searchable
without turning code comments or docstrings into a change log.
"""

from __future__ import annotations

import ast
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


def _docstring_nodes(source: str) -> list[ast.Constant]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    nodes: list[ast.Constant] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            nodes.append(value)
    return nodes


def _find_line_col(
    text: str,
    *,
    start_line: int,
    start_col: int,
    match_start: int,
) -> tuple[int, int]:
    prefix = text[:match_start]
    line_offset = prefix.count("\n")
    if line_offset == 0:
        return start_line, start_col + match_start
    return start_line + line_offset, len(prefix.rsplit("\n", 1)[-1])


def _append_violation(
    violations: list[DesignProvenanceViolation],
    *,
    text: str,
    start_line: int,
    start_col: int,
    context: str,
) -> None:
    for pattern, reason in _PROVENANCE_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        lineno, col_offset = _find_line_col(
            text,
            start_line=start_line,
            start_col=start_col,
            match_start=match.start(),
        )
        violations.append(
            DesignProvenanceViolation(
                lineno=lineno,
                col_offset=col_offset,
                snippet=match.group(0),
                reason=f"source {context} carries {reason}",
            )
        )
        break


def check_source(source: str) -> list[DesignProvenanceViolation]:
    """Return EAWF012 violations for Python source comments and docstrings.

    Args:
        source: Python source text to inspect.

    Returns:
        Violations in source order. Ordinary string literals are not
        scanned because this rule is scoped to comments and docstrings.
    """
    violations: list[DesignProvenanceViolation] = []
    for tok in _comment_tokens(source):
        _append_violation(
            violations,
            text=tok.string,
            start_line=tok.start[0],
            start_col=tok.start[1],
            context="comment",
        )
    for node in _docstring_nodes(source):
        _append_violation(
            violations,
            text=node.value,
            start_line=node.lineno,
            start_col=node.col_offset,
            context="docstring",
        )
    return sorted(violations, key=lambda violation: (violation.lineno, violation.col_offset))
