"""EAWF018 — advisory structure-smell lint (block-bloat heuristics).

Catches the block-structure blind spot a prose linter (Vale) misses:
over-long prose blocks, run-on bullet lists, over-long single bullets in
Markdown, and over-long leading-description paragraphs in Python
docstrings. All four heuristics are *advisory* — the hook wires this
module via ``blocking=False`` so a finding emits a warning and exits 0,
never failing the commit.

The thresholds below are the spike-calibrated defaults (a low
single-digit per-block flag-rate over the real corpus): an over-long
prose block at 600 chars, a run-on bullet list at 12 items, an
over-long single bullet at 500 chars, and an over-long docstring
description paragraph at 600 chars. The pyproject ``[tool.eawf.lint]``
dispatcher resolves the live caps from ``[tool.eawf.lint.eawf018]`` and
clamps any local override to be no looser than these defaults
(tighten-only).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

RULE_CODE = "EAWF018"

# Spike-calibrated defaults (see
# .ea/local/research/2026-06-03-eawf018-threshold-calibration.md). Mirrored
# as module constants so a missing pyproject sub-table still yields the
# canonical caps without importing the config loader at check time, and so
# the dispatcher can clamp a local override no looser than these (the
# tighten-only authority guard).
DEFAULT_MAX_PROSE_CHARS = 600
DEFAULT_MAX_BULLET_RUN = 12
DEFAULT_MAX_BULLET_CHARS = 500
DEFAULT_MAX_DOCSTRING_PARA_CHARS = 600

_BULLET_PREFIXES = ("- ", "* ", "+ ")
# Google-style docstring section headers that terminate the leading
# description paragraph. Matched case-sensitively on the stripped line.
_DOCSTRING_SECTION_HEADERS = (
    "Args:",
    "Arguments:",
    "Returns:",
    "Return:",
    "Yields:",
    "Yield:",
    "Raises:",
    "Attributes:",
    "Note:",
    "Notes:",
    "Example:",
    "Examples:",
    "Warning:",
    "Warnings:",
    "References:",
)


@dataclass(frozen=True)
class StructureSmell:
    """One EAWF018 advisory finding."""

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


def _is_bullet(stripped: str) -> bool:
    """Return ``True`` when ``stripped`` opens an unordered list item."""
    return stripped.startswith(_BULLET_PREFIXES)


def _is_skippable_prose(stripped: str) -> bool:
    """Return ``True`` for blank / structural lines exempt from the H1 cap."""
    return not stripped or stripped.startswith(("#", ">", "|", "<!--", "::"))


def _over_long_bullet(line: str, stripped: str, lineno: int, cap: int) -> StructureSmell | None:
    """Return an H3 finding when a single bullet's text exceeds ``cap``."""
    if len(stripped) <= cap:
        return None
    return StructureSmell(
        lineno=lineno,
        col_offset=len(line) - len(line.lstrip()),
        snippet=stripped[:100],
        reason=f"over-long bullet ({len(stripped)} chars) — tighten or split",
    )


def _over_long_prose(line: str, stripped: str, lineno: int, cap: int) -> StructureSmell | None:
    """Return an H1 finding when a plain-prose block exceeds ``cap``."""
    if _is_skippable_prose(stripped) or len(stripped) <= cap:
        return None
    return StructureSmell(
        lineno=lineno,
        col_offset=len(line) - len(line.lstrip()),
        snippet=stripped[:100],
        reason=f"over-long prose block ({len(stripped)} chars) — split into paragraphs",
    )


@dataclass
class _BulletRun:
    """Accumulator for the H2 maximal-consecutive-bullet-run heuristic."""

    max_run: int
    length: int = 0
    start_lineno: int = 0
    start_snippet: str = ""

    def add(self, stripped: str, lineno: int) -> None:
        """Extend the current run with a bullet item at ``lineno``."""
        if self.length == 0:
            self.start_lineno = lineno
            self.start_snippet = stripped[:100]
        self.length += 1

    def flush(self) -> StructureSmell | None:
        """Return an H2 finding if the just-ended run exceeded the cap, then reset."""
        finding: StructureSmell | None = None
        if self.length > self.max_run:
            finding = StructureSmell(
                lineno=self.start_lineno,
                col_offset=0,
                snippet=self.start_snippet,
                reason=f"run-on bullet list ({self.length} items) — split into sub-sections",
            )
        self.length = 0
        return finding


def check_markdown(
    source: str,
    *,
    max_prose_chars: int = DEFAULT_MAX_PROSE_CHARS,
    max_bullet_run: int = DEFAULT_MAX_BULLET_RUN,
    max_bullet_chars: int = DEFAULT_MAX_BULLET_CHARS,
) -> list[StructureSmell]:
    """Return non-blocking EAWF018 advisories for a Markdown block-bloat sweep.

    Runs three heuristics. H1 flags a plain-prose block (one physical
    line, since EAWF014 forbids hard-wrap) longer than ``max_prose_chars``.
    H2 flags a maximal run of consecutive bullet items longer than
    ``max_bullet_run`` (reported at the run's first item). H3 flags a
    single bullet item whose text exceeds ``max_bullet_chars``. Fenced code
    blocks are skipped.

    Args:
        source: Markdown text to inspect.
        max_prose_chars: H1 per-block character cap.
        max_bullet_run: H2 maximum consecutive-bullet-item count.
        max_bullet_chars: H3 per-bullet character cap.

    Returns:
        Advisory findings in source order. Fenced code blocks are ignored.
    """
    advisories: list[StructureSmell] = []
    run = _BulletRun(max_run=max_bullet_run)
    in_fence = False

    def _end_run() -> None:
        finding = run.flush()
        if finding is not None:
            advisories.append(finding)

    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            if not in_fence:
                _end_run()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _is_bullet(stripped):
            run.add(stripped, lineno)
            bullet_finding = _over_long_bullet(line, stripped, lineno, max_bullet_chars)
            if bullet_finding is not None:
                advisories.append(bullet_finding)
            continue
        # Non-bullet line ends any open run.
        _end_run()
        prose_finding = _over_long_prose(line, stripped, lineno, max_prose_chars)
        if prose_finding is not None:
            advisories.append(prose_finding)
    _end_run()
    return advisories


def _description_paragraph(docstring: str) -> str:
    """Return the leading description paragraph of ``docstring``, lines joined.

    Joins the wrapped physical lines of the leading description into one
    paragraph (ruff wraps docstrings at ~88 cols, so the smell only shows
    once lines are re-joined). Stops at the first blank line *after* some
    text has accumulated, or at the first Google section header
    (``Args:`` / ``Returns:`` / ``Raises:`` / ...). Leading blank lines are
    skipped so a docstring whose summary starts on line 2 still joins.
    """
    parts: list[str] = []
    for raw in docstring.splitlines():
        stripped = raw.strip()
        if not stripped:
            if parts:
                break
            continue
        if stripped in _DOCSTRING_SECTION_HEADERS:
            break
        parts.append(stripped)
    return " ".join(parts)


def check_docstrings(
    source: str,
    *,
    max_para_chars: int = DEFAULT_MAX_DOCSTRING_PARA_CHARS,
) -> list[StructureSmell]:
    """Return non-blocking EAWF018 advisories for over-long docstring paragraphs.

    Parses ``source`` and inspects every module / class / function
    docstring. Each docstring's leading description paragraph is rebuilt by
    joining its wrapped physical lines (stopping at the first blank line or
    Google section header); a joined paragraph longer than
    ``max_para_chars`` is flagged. A raw-physical-line heuristic finds
    nothing here because ruff wraps docstrings at ~88 cols, so the join is
    load-bearing.

    Args:
        source: Python source text to inspect.
        max_para_chars: Per-paragraph character cap.

    Returns:
        Advisory findings in source order (by docstring line number).

    Raises:
        SyntaxError: if ``source`` is not parseable Python (the caller
            skips such files — a parse failure is surfaced by ruff
            elsewhere).
    """
    tree = ast.parse(source)
    advisories: list[StructureSmell] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        docstring = ast.get_docstring(node, clean=True)
        if docstring is None:
            continue
        paragraph = _description_paragraph(docstring)
        if len(paragraph) <= max_para_chars:
            continue
        # The docstring expr is the first statement of the body; its lineno
        # locates the finding. Modules with a docstring put the expr first too.
        body = getattr(node, "body", [])
        anchor = body[0] if body else node
        advisories.append(
            StructureSmell(
                lineno=getattr(anchor, "lineno", 1),
                col_offset=getattr(anchor, "col_offset", 0),
                snippet=paragraph[:100],
                reason=(
                    f"over-long docstring paragraph ({len(paragraph)} chars) — split the summary"
                ),
            )
        )
    advisories.sort(key=lambda finding: finding.lineno)
    return advisories
