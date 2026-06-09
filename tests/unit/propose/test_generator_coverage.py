"""Tests for the anti-drift generator: extraction + deterministic diff."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import (
    DeferredDeliverable,
    SourceUnit,
    grandfather_criterion,
)
from eawf.workflow.propose.coverage import coverage_gaps, significant_tokens
from eawf.workflow.propose.generator import coverage_diff, extract_units

_FIXTURE = Path(__file__).parent / "fixtures" / "tui_drift_brief.md"

# Index of the rich digit-map span in the TUI-drift fixture (first line).
_DIGIT_MAP_SPAN = "U-000"


def _defer(span_id: str, *, target: str = "B999") -> DeferredDeliverable:
    """Build a valid deferral row whose reason clears the 20-char floor."""
    return DeferredDeliverable(
        span_id=span_id,
        reason="deferred to a later phase with a tracked backlog target",
        target=target,
    )


def test_extract_units_two_sentence_brief_returns_two_units() -> None:
    """A two-sentence brief yields two units with stable ids and offsets."""
    text = "First clause here. Second clause here."
    units = extract_units(text)
    assert [u.span_id for u in units] == ["U-000", "U-001"]
    assert units[0].quote == "First clause here."
    assert units[1].quote == "Second clause here."
    assert units[0].char_offset == 0
    assert units[1].char_offset == text.index("Second")


def test_extract_units_is_deterministic_over_identical_input() -> None:
    """Repeated extraction over the same input yields identical spans."""
    text = "Alpha sentence one. Beta sentence two; gamma clause three.\n"
    first = extract_units(text)
    second = extract_units(text)
    assert [u.model_dump() for u in first] == [u.model_dump() for u in second]


def test_extract_units_empty_brief_returns_empty() -> None:
    """An empty / whitespace-only brief extracts no spans."""
    assert extract_units("") == []
    assert extract_units("   \n\t  \n") == []


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


def test_coverage_diff_tui_fixture_flags_collapsed_digit_map() -> None:
    """The TUI-drift fixture flags the dropped digit-map span (CR-3 calibration)."""
    units = extract_units(_FIXTURE.read_text(encoding="utf-8"))
    # The first span is the rich six-mode digit map.
    digit_map = units[0]
    assert digit_map.span_id == _DIGIT_MAP_SPAN
    assert "six-mode digit map" in digit_map.quote
    # Emit criteria that cover every OTHER span but omit the digit-map detail,
    # exactly as the incident's generator pass did, with no deferral filed.
    covered = {u.span_id for u in units[1:]}
    report = coverage_diff(units, covered, [])
    assert _DIGIT_MAP_SPAN in report.uncovered
    assert _DIGIT_MAP_SPAN not in report.covered


# ---- coverage_gaps: criteria vs planned-steps shared helper ------------------


def test_significant_tokens_drops_short_connectives() -> None:
    """Tokens under the 4-char floor are dropped; topical runs are kept."""
    tokens = significant_tokens("wire the parser to a new exporter")
    assert "parser" in tokens
    assert "exporter" in tokens
    assert "the" not in tokens
    assert "to" not in tokens


def test_coverage_gaps_flags_uncovered_planned_step() -> None:
    """A planned step no criterion topically covers is an EAWF022 finding."""
    criteria = [grandfather_criterion("implement parser tokeniser module", index=1)]
    steps = ["implement parser tokeniser module", "wire telemetry dashboard exporter"]
    findings = coverage_gaps(criteria, planned_steps=steps)
    assert len(findings) == 1
    assert findings[0].code == "EAWF022"


def test_coverage_gaps_clean_when_every_step_covered() -> None:
    """Every planned step covered by a criterion yields no finding."""
    criteria = [
        grandfather_criterion("implement parser tokeniser module", index=1),
        grandfather_criterion("wire telemetry dashboard exporter", index=2),
    ]
    steps = ["implement parser tokeniser module", "wire telemetry dashboard exporter"]
    assert coverage_gaps(criteria, planned_steps=steps) == []


def test_coverage_gaps_no_planned_steps_is_noop() -> None:
    """A wave with no planned steps has nothing to cover (clean no-op)."""
    criteria = [grandfather_criterion("anything at all", index=1)]
    assert coverage_gaps(criteria, planned_steps=[]) == []
