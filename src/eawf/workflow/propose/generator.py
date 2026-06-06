"""Anti-drift generator: deterministic source extraction + coverage diff.

The TUI brief-criteria-drift incident (``.ea/artifacts/incidents/
2026-06-02-tui-brief-criteria-drift.md``) lost a rich six-mode digit-map
spec when a JSON generator collapsed each candidate detail into a single
one-line success criterion with no dropped-detail log and no deferral
target. This module is the structural fix: a brief is first split into
:class:`~eawf.kernel.spec.common.SourceUnit` rows, then a coverage diff
checks every span id against the set the emitted criteria actually cover
plus the explicit deferral list. The diff is computed over span ids, so
the generator never trusts an LLM's claim that it dropped nothing.

Stage-1 (``extract_units``) and the coverage diff (``coverage_diff``) are
the deterministic deliverable here. The Stage-2 mapper that turns
:class:`SourceUnit` rows into the emit-target
:class:`~eawf.kernel.spec.common.CriterionSpec` list is an LLM call that
lives behind the ``/roadmap propose`` skill, not in this module.
"""

from __future__ import annotations

import logging
import re

from eawf.kernel.spec.common import (
    CoverageReport,
    CriterionSpec,
    DeferredDeliverable,
    SourceUnit,
)

logger = logging.getLogger(__name__)

#: Split point between atomic spans. A span boundary is a sentence
#: terminator (``.``, ``!``, ``?``) or a clause separator (``;``)
#: followed by whitespace, OR a hard newline. Capturing the boundary
#: lets the splitter preserve each span's absolute ``char_offset``.
_SPAN_BOUNDARY = re.compile(r"(?<=[.!?;])\s+|\n+")


def extract_units(brief_text: str) -> list[SourceUnit]:
    """Split a brief into atomic source spans with stable ids and offsets.

    This is EXTRACTION, not summary: every non-empty atomic span (a
    sentence or a clause delimited by a terminator / separator / hard
    newline) becomes one :class:`SourceUnit`. The split is deterministic
    over identical input -- the same brief always yields the same span
    ids and offsets -- because the generator's whole purpose is a
    reproducible diff, not a paraphrase.

    Each unit carries ``span_id`` ``f"U-{i:03d}"`` (0-based, zero-padded
    to three digits) and the 0-based ``char_offset`` of the span's first
    character in *brief_text*, so a coverage finding traces back to the
    exact source location.

    Args:
        brief_text: The raw source brief text to extract spans from.

    Returns:
        One :class:`SourceUnit` per atomic span, in source order. An empty
        or whitespace-only brief yields an empty list.
    """
    units: list[SourceUnit] = []
    cursor = 0
    index = 0
    for match in _SPAN_BOUNDARY.finditer(brief_text):
        raw = brief_text[cursor : match.start()]
        unit = _build_unit(raw, cursor, index)
        if unit is not None:
            units.append(unit)
            index += 1
        cursor = match.end()
    tail = brief_text[cursor:]
    tail_unit = _build_unit(tail, cursor, index)
    if tail_unit is not None:
        units.append(tail_unit)
    logger.debug(f"extract_units spans={len(units)} chars={len(brief_text)}")
    return units


def _build_unit(raw: str, span_start: int, index: int) -> SourceUnit | None:
    """Build one :class:`SourceUnit` from a raw span slice, or skip it.

    The ``char_offset`` is anchored to the first non-whitespace
    character so leading whitespace inside a slice does not shift the
    reported source location. A slice that is empty or all whitespace
    yields ``None`` (no span).

    Args:
        raw: The raw text slice between two span boundaries.
        span_start: The absolute offset of *raw* in the original brief.
        index: The 0-based ordinal used to mint the span id.

    Returns:
        A :class:`SourceUnit`, or ``None`` when the slice has no content.
    """
    stripped = raw.strip()
    if not stripped:
        return None
    lead = len(raw) - len(raw.lstrip())
    return SourceUnit(
        span_id=f"U-{index:03d}",
        quote=stripped[:1000],
        char_offset=span_start + lead,
    )


def coverage_diff(
    units: list[SourceUnit],
    covered_span_ids: set[str],
    deferrals: list[DeferredDeliverable],
) -> CoverageReport:
    """Diff extracted spans against the covered set plus explicit deferrals.

    A span id is COVERED when it appears in *covered_span_ids* (the set
    the caller derives from the emitted criteria -- one entry per span
    that at least one :class:`CriterionSpec` maps to). A span id is
    DEFERRED when an explicit :class:`DeferredDeliverable` names it. Any
    span in neither set lands in :attr:`CoverageReport.uncovered` -- a
    hard finding, because the brief detail was silently dropped.

    The signature takes ``covered_span_ids`` as an explicit set rather
    than reading a ``source_span`` field off :class:`CriterionSpec`: the
    persisted criterion schema is left untouched (no store migration, no
    golden regen), and the diff stays a pure function of three plain
    inputs. The caller -- the ``/roadmap propose`` Stage-2 mapper -- owns
    the criterion -> span mapping and passes the resulting cover set in.
    Coverage is decided by span id, never by an LLM's self-report.

    Args:
        units: The extracted source spans (from :func:`extract_units`).
        covered_span_ids: Span ids that at least one emitted criterion
            maps to.
        deferrals: Explicit deferral rows; each names a span id, a
            rationale, and a filing target.

    Returns:
        A :class:`CoverageReport` partitioning the span ids into
        ``covered``, ``deferred``, and ``uncovered``. ``covered`` and
        ``uncovered`` preserve source order; a span both covered and
        deferred counts as covered (coverage wins).
    """
    deferred_ids = {row.span_id for row in deferrals}
    covered: list[str] = []
    uncovered: list[str] = []
    for unit in units:
        if unit.span_id in covered_span_ids:
            covered.append(unit.span_id)
        elif unit.span_id in deferred_ids:
            continue
        else:
            uncovered.append(unit.span_id)
    logger.debug(
        f"coverage_diff units={len(units)} covered={len(covered)} "
        f"deferred={len(deferrals)} uncovered={len(uncovered)}"
    )
    return CoverageReport(
        covered=covered,
        deferred=list(deferrals),
        uncovered=uncovered,
    )


def covered_span_ids(
    criteria: list[CriterionSpec],
    spans_by_criterion_id: dict[str, str],
) -> set[str]:
    """Derive the covered-span set from emitted criteria and their span refs.

    Convenience for the Stage-2 mapper: the mapper authors each
    :class:`CriterionSpec` and records which source span the criterion
    was synthesised from in *spans_by_criterion_id* (criterion id ->
    span id). This collapses that mapping into the ``covered_span_ids``
    set :func:`coverage_diff` consumes, without adding a ``source_span``
    field to the persisted criterion schema.

    Args:
        criteria: The emitted criteria for the wave set.
        spans_by_criterion_id: A mapping from each criterion's id to the
            source span id it was synthesised from. Criteria absent from
            the mapping contribute no coverage.

    Returns:
        The set of span ids covered by at least one criterion.
    """
    return {
        span_id
        for criterion in criteria
        if (span_id := spans_by_criterion_id.get(criterion.id)) is not None
    }
