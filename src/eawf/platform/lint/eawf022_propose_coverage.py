"""EAWF022 -- surface dropped brief detail at ``/roadmap propose`` render time.

Layer 2 of the doc-clarity enforcement stack and the Fidelity-Spine L1
decomposition-coverage layer: the TUI brief-criteria-drift incident
(``.ea/artifacts/incidents/2026-06-02-tui-brief-criteria-drift.md``) lost a
rich six-mode digit-map spec when a JSON generator collapsed each candidate
detail into a single one-line success criterion with no dropped-detail log
and no deferral target. The FS11 anti-drift generator
(:func:`eawf.workflow.propose.generator.coverage_diff`) already partitions a
brief's extracted spans into covered / deferred / uncovered; this rule wires
that diff into the propose render so each silently-dropped (``uncovered``)
span becomes a finding in the skill envelope.

The module exposes two surfaces:

- :func:`check_coverage` -- the typed entry the ``/roadmap propose`` render
  calls with the brief units, the emitted-criteria cover set, and the
  explicit deferral list. It runs :func:`coverage_diff` and turns each
  ``uncovered`` span id into a :class:`CoverageGapViolation`.
- :func:`check_source` -- the prose-string adapter the
  :func:`~eawf.platform.lint.validate_prose.validate_prose` chokepoint
  composes. Coverage is a diff over typed inputs, not a property of a raw
  prose surface, so the composed leg is a no-op (returns ``[]``); the real
  surface is :func:`check_coverage`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from eawf.workflow.propose.generator import coverage_diff

if TYPE_CHECKING:
    from eawf.kernel.spec.common import DeferredDeliverable, SourceUnit

logger = logging.getLogger(__name__)

RULE_CODE = "EAWF022"


@dataclass(frozen=True)
class CoverageGapViolation:
    """One EAWF022 finding: a brief span covered by neither a criterion nor a deferral.

    Attributes:
        lineno: 1-based ordinal of the uncovered span within the
            :func:`coverage_diff` ``uncovered`` list (a stable position, not a
            source line; the brief char offset lives in ``snippet``).
        col_offset: Always ``0`` -- the finding is span-level, not column-level.
        snippet: The uncovered span id (``U-007``).
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
        """Return a ``line:col: CODE reason: snippet`` style one-liner body."""
        return f"{self.lineno}:{self.col_offset}: {RULE_CODE} {self.reason}: {self.snippet!r}"


def check_coverage(
    units: list[SourceUnit],
    covered_span_ids: set[str],
    deferrals: list[DeferredDeliverable],
) -> list[CoverageGapViolation]:
    """Return one EAWF022 finding per silently-dropped brief span.

    Runs the FS11 :func:`coverage_diff` over the extracted brief spans, the
    emitted-criteria cover set, and the explicit deferral list, then surfaces
    each ``uncovered`` span id as a finding. A span covered by a criterion or
    named by a :class:`DeferredDeliverable` is not a finding; a span in
    neither set is a hard finding because the brief detail was dropped with no
    criterion and no deferral target.

    Args:
        units: The extracted source spans (from
            :func:`eawf.workflow.propose.generator.extract_units`).
        covered_span_ids: Span ids that at least one emitted criterion maps to.
        deferrals: Explicit deferral rows; each names a span id, a rationale,
            and a filing target.

    Returns:
        One :class:`CoverageGapViolation` per ``uncovered`` span id, in the
        ``coverage_diff`` (source) order. An empty list when every span is
        covered or deferred.
    """
    report = coverage_diff(units, covered_span_ids, deferrals)
    violations = [
        CoverageGapViolation(
            lineno=index,
            col_offset=0,
            snippet=span_id,
            reason="brief span dropped with no covering criterion and no deferral",
        )
        for index, span_id in enumerate(report.uncovered, start=1)
    ]
    logger.debug(f"check_coverage uncovered={len(violations)} units={len(units)}")
    return violations


def check_source(source: str) -> list[CoverageGapViolation]:
    """Return EAWF022 findings over a Markdown prose surface (always empty).

    The prose-string adapter the :func:`~eawf.platform.lint.validate_prose`
    chokepoint composes. Coverage is a diff over typed inputs (brief spans,
    cover set, deferrals), not a property of a raw prose surface, so this leg
    is a no-op. The real surface is :func:`check_coverage`, called by the
    ``/roadmap propose`` render with the typed inputs.

    Args:
        source: Markdown text (ignored).

    Returns:
        An empty list.
    """
    return []


__all__ = [
    "RULE_CODE",
    "CoverageGapViolation",
    "check_coverage",
    "check_source",
]
