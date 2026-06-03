"""EAWF017 — move reference soup out of prose into a numbered table.

Layer 2 of the doc-clarity enforcement stack (see
``.ea/local/research/2026-05-29-doc-clarity.md``). Rendered artifacts cite
sources with dense ``[N]`` markers backed by a ``## References`` table; the
``file:line`` targets and the URLs live in that table, one row each, not
mid-sentence. Prose that inlines a bare URL or chains several ``file:line``
clauses is hard to read and hard to audit — the same "semicolon-soup" failure
the brief's audit-rewrite worked example fixes.

This module is the deterministic backstop. It flags two prose shapes:

- **inline bare URL** — any ``http://`` / ``https://`` URL that appears in
  running prose (not inside an inline-code span, not inside the
  ``## References`` table, not on a reference row). A single bare URL is a
  finding; URLs belong in a reference row keyed by a ``[N]`` marker.
- **reference soup** — more than two ``path:line`` references inside one
  prose block (a run of non-blank lines). Two reads fine; the third signals
  the references should move into a numbered ``## References`` table.

EAWF017 composes with EAWF013 (which positions the ``[N]`` markers it does
not itself emit) and the chassis citation-resolution validator (which checks
those markers resolve to rows): EAWF013 owns marker *position*, the resolution
validator owns marker *resolution*, and EAWF017 owns whether the raw
``file:line`` / URL targets were tabulated at all. No rule double-checks
another's surface.

The scan is block-aware: it skips fenced code blocks, the whole
``## References`` section, inline-code spans (so a ``path:line`` inside
backticks is exempt), and reference rows themselves, mirroring the EAWF013
fence/reference handling so the citation table is never linted as prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RULE_CODE = "EAWF017"

# A bare URL token. Trailing sentence punctuation is trimmed by the caller so
# ``see https://example.org/x.`` reports the URL without the period.
_URL = re.compile(r"https?://[^\s)>\]]+")

# A ``path:line`` reference: a slash-bearing path-ish token followed by ``:``
# and a line number, e.g. ``src/eawf/x.py:142``. The leading slash requirement
# keeps a bare ``word:12`` clock-like token from matching; a real source path
# in this codebase always carries a directory separator.
_PATH_LINE = re.compile(r"(?<![\w/])[\w./-]*/[\w.-]+\.[A-Za-z0-9_]+:\d+")

# An inline-code span (`...`). Spans are stripped before the prose scan so a
# ``file:line`` or URL inside backticks is exempt (it is already tabulated /
# fenced text, not running prose).
_INLINE_CODE = re.compile(r"`[^`]*`")

# A reference-table row: ``[N] ...`` / ``- [N] ...`` / a markdown table row
# whose first cell is a ``[N]`` marker. Mirrors EAWF013's reference-row guard
# plus the table-row form the brief's worked example uses (``| [a] | ... |``).
_REFERENCE_ROW = re.compile(r"^\s*(?:[-*]\s+)?\[[0-9A-Za-z]+\](?::|\s+`|\s+https?://|\s+\.)")
_TABLE_REFERENCE_ROW = re.compile(r"^\s*\|\s*\[[0-9A-Za-z]+\]\s*\|")

# The maximum inline ``path:line`` references tolerated inside one prose block.
# The third reference is the first violation: two reads fine, three is soup.
MAX_INLINE_PATH_REFS = 2


@dataclass(frozen=True)
class InlineReferenceViolation:
    """One EAWF017 finding against a prose line or block.

    Attributes:
        lineno: 1-based line number the finding is anchored to. For a bare
            URL it is the line the URL is on; for reference soup it is the
            line carrying the over-limit reference (where the count tipped).
        col_offset: 0-based column of the matched token for a URL finding;
            ``0`` for a block-level reference-soup finding.
        snippet: The offending token (the URL) or a short block descriptor
            (the ``path:line`` reference list) surfaced so the author can
            locate and tabulate it.
        reason: Lowercase-led, period-free explanation of the rule tripped.
    """

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


def _strip_inline_code(line: str) -> str:
    """Return *line* with inline-code spans blanked to equal-length spaces.

    Replacing each span with spaces (rather than deleting it) preserves the
    column offsets of the surrounding prose so a URL finding still reports its
    true column.
    """
    return _INLINE_CODE.sub(lambda m: " " * len(m.group(0)), line)


def _is_reference_row(line: str) -> bool:
    """Return ``True`` when *line* is a numbered reference row (any form)."""
    return bool(_REFERENCE_ROW.match(line) or _TABLE_REFERENCE_ROW.match(line))


def _iter_prose_lines(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, prose-text)`` pairs, with blank-line separators kept.

    Skips fenced code blocks (``` / ~~~), the whole ``## References`` section
    (until the next ``## `` heading or end of file), reference rows, and
    heading lines. A blank line inside prose is preserved (yielded as an empty
    string) so the soup scan can use it as a block separator. Each yielded
    prose line has its inline-code spans blanked so a ``path:line`` or URL
    inside backticks is not treated as prose. Mirrors the EAWF013 fence walk.
    """
    rows: list[tuple[int, str]] = []
    in_fence = False
    in_references = False
    for lineno, raw in enumerate(source.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            # A fence boundary ends any open prose block.
            rows.append((lineno, ""))
            continue
        if in_fence:
            continue
        if stripped.startswith("## "):
            in_references = stripped[3:].strip().casefold() == "references"
            rows.append((lineno, ""))
            continue
        if in_references:
            continue
        if _is_reference_row(raw):
            rows.append((lineno, ""))
            continue
        if not stripped:
            rows.append((lineno, ""))
            continue
        rows.append((lineno, _strip_inline_code(raw)))
    return rows


def check_source(source: str) -> list[InlineReferenceViolation]:
    """Return EAWF017 violations for inline URLs and reference soup.

    Args:
        source: Markdown text to inspect.

    Returns:
        Violations in source order. Fenced code blocks, the ``## References``
        section, reference rows, and inline-code spans are ignored. Each bare
        inline URL is one finding; a prose block carrying more than
        :data:`MAX_INLINE_PATH_REFS` inline ``path:line`` references yields one
        block-level finding anchored to the line of the over-limit reference.
    """
    violations: list[InlineReferenceViolation] = []
    prose = _iter_prose_lines(source)

    for lineno, line in prose:
        if not line:
            continue
        for match in _URL.finditer(line):
            url = match.group(0).rstrip(".,;:!?)")
            violations.append(
                InlineReferenceViolation(
                    lineno=lineno,
                    col_offset=match.start(),
                    snippet=url,
                    reason="inline bare URL; cite it from a numbered ## References row",
                )
            )

    violations.extend(_check_reference_soup(prose))
    violations.sort(key=lambda v: (v.lineno, v.col_offset))
    return violations


def _check_reference_soup(
    prose: list[tuple[int, str]],
) -> list[InlineReferenceViolation]:
    """Flag prose blocks with more than two inline ``path:line`` references.

    A *block* is a run of consecutive non-blank prose lines; an empty line ends
    it. The reference count is per block, so two references in one paragraph
    and two in the next are both clean, but three in a single paragraph trip
    the rule. The finding is anchored to the line carrying the over-limit
    (third) reference.
    """
    violations: list[InlineReferenceViolation] = []
    block_refs: list[tuple[int, str]] = []

    def flush() -> None:
        if len(block_refs) > MAX_INLINE_PATH_REFS:
            trip_lineno, _ = block_refs[MAX_INLINE_PATH_REFS]
            joined = ", ".join(ref for _, ref in block_refs)
            violations.append(
                InlineReferenceViolation(
                    lineno=trip_lineno,
                    col_offset=0,
                    snippet=joined,
                    reason=(
                        f"more than {MAX_INLINE_PATH_REFS} inline path:line refs in one "
                        "block; move them into a numbered ## References table"
                    ),
                )
            )
        block_refs.clear()

    for _lineno, line in prose:
        if not line.strip():
            flush()
            continue
        for match in _PATH_LINE.finditer(line):
            block_refs.append((_lineno, match.group(0)))
    flush()
    return violations


def assert_inline_references(text: str, *, surface: str) -> None:
    """Raise when *text* trips the EAWF017 inline-reference rules.

    The text-surface gate: the in-skill loop / text-surface validator calls
    this with a commit-body, PR-body, or ``state.json`` description string so a
    reference-soupy draft is rejected before it is committed. A clean text is a
    no-op.

    Args:
        text: The prose surface to inspect (Markdown).
        surface: Human label for the surface (``"commit body"`` / ``"PR
            body"`` / ``"description"``), interpolated into the error.

    Raises:
        ValueError: when *text* trips one or more inline-reference rules. The
            message names the surface and every finding.
    """
    violations = check_source(text)
    if not violations:
        return
    reasons = "; ".join(v.render() for v in violations)
    raise ValueError(f"{surface} fails inline-reference tabulation: {reasons}")


__all__ = [
    "MAX_INLINE_PATH_REFS",
    "RULE_CODE",
    "InlineReferenceViolation",
    "assert_inline_references",
    "check_source",
]
