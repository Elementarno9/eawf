"""EAWF014 — reject manually wrapped rendered Markdown paragraphs."""

from __future__ import annotations

from dataclasses import dataclass

RULE_CODE = "EAWF014"

_BLOCK_PREFIXES = (
    "#",
    "- ",
    "* ",
    "+ ",
    ">",
    "|",
    "<!--",
    "::",
)


@dataclass(frozen=True)
class ManualWrapViolation:
    """One EAWF014 finding."""

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


def _is_plain_prose(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(_BLOCK_PREFIXES):
        return False
    if len(stripped) >= 2 and stripped[0].isdigit() and stripped[1] in ".):":
        return False
    if stripped.startswith(("```", "~~~")):
        return False
    return not (stripped.startswith("**") and stripped.endswith("**"))


def _line_joins_forward(line: str) -> bool:
    stripped = line.rstrip()
    if stripped.endswith(("  ", "\\", ">", "|")):
        return False
    return not stripped.endswith((".", "!", "?", ":", ";"))


def check_source(source: str) -> list[ManualWrapViolation]:
    """Return EAWF014 violations for likely hard-wrapped paragraphs.

    Args:
        source: Markdown text to inspect.

    Returns:
        Violations in source order. Fenced code, lists, blockquotes,
        headings, tables, directives, and reference rows are ignored.
    """
    violations: list[ManualWrapViolation] = []
    in_fence = False
    previous_plain: tuple[int, str] | None = None
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            previous_plain = None
            continue
        if in_fence or not _is_plain_prose(line):
            previous_plain = None
            continue
        if previous_plain is not None:
            prev_lineno, prev_line = previous_plain
            if _line_joins_forward(prev_line):
                violations.append(
                    ManualWrapViolation(
                        lineno=lineno,
                        col_offset=len(line) - len(line.lstrip()),
                        snippet=stripped[:80],
                        reason=f"paragraph appears manually wrapped after line {prev_lineno}",
                    )
                )
        previous_plain = (lineno, line)
    return violations
