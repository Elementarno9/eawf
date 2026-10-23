"""Tests for the anti-drift generator's deterministic coverage diff."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import (
    DeferredDeliverable,
    SourceUnit,
)
from eawf.workflow.propose.generator import coverage_diff


def _defer(span_id: str, *, target: str = "B999") -> DeferredDeliverable:
    """Build a valid deferral row whose reason clears the 20-char floor."""
    return DeferredDeliverable(
        span_id=span_id,
        reason="deferred to a later phase with a tracked backlog target",
        target=target,
    )


def test_coverage_diff_full_coverage_yields_empty_uncovered() -> None:
    """Every span mapped to a criterion leaves uncovered empty (boundary)."""
    units = [
        SourceUnit(span_id="U-000", quote="span zero.", char_offset=0),
        SourceUnit(span_id="U-001", quote="span one.", char_offset=11),
    ]
    report = coverage_diff(units, {"U-000", "U-001"}, [])
    assert report.uncovered == []
    assert report.covered == ["U-000", "U-001"]
    assert report.deferred == []


def test_coverage_diff_deferred_span_is_suppressed() -> None:
    """A deferred span lands in deferred, not uncovered (boundary)."""
    units = [
        SourceUnit(span_id="U-000", quote="covered span.", char_offset=0),
        SourceUnit(span_id="U-001", quote="deferred span.", char_offset=14),
    ]
    report = coverage_diff(units, {"U-000"}, [_defer("U-001")])
    assert report.uncovered == []
    assert report.covered == ["U-000"]
    assert [d.span_id for d in report.deferred] == ["U-001"]


def test_coverage_diff_unmapped_span_is_a_hard_finding() -> None:
    """A span with neither a criterion nor a deferral is uncovered (error-path)."""
    units = [
        SourceUnit(span_id="U-000", quote="covered span.", char_offset=0),
        SourceUnit(span_id="U-001", quote="dropped span.", char_offset=14),
    ]
    report = coverage_diff(units, {"U-000"}, [])
    assert report.uncovered == ["U-001"]
    assert report.covered == ["U-000"]


def test_coverage_diff_covered_wins_over_deferred() -> None:
    """A span that is both covered and deferred counts as covered."""
    units = [SourceUnit(span_id="U-000", quote="overlap span.", char_offset=0)]
    report = coverage_diff(units, {"U-000"}, [_defer("U-000")])
    assert report.covered == ["U-000"]
    assert report.uncovered == []


def test_coverage_diff_preserves_source_order_in_uncovered() -> None:
    """Uncovered span ids retain the order the units were extracted in."""
    units = [
        SourceUnit(span_id="U-002", quote="third.", char_offset=20),
        SourceUnit(span_id="U-000", quote="first.", char_offset=0),
        SourceUnit(span_id="U-001", quote="second.", char_offset=10),
    ]
    report = coverage_diff(units, set(), [])
    assert report.uncovered == ["U-002", "U-000", "U-001"]


def test_deferred_deliverable_short_reason_raises_validation_error() -> None:
    """A DeferredDeliverable with a sub-20-char reason fails validation (error-path)."""
    with pytest.raises(ValidationError):
        DeferredDeliverable(span_id="U-001", reason="too short", target="B999")
