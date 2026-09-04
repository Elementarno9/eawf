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
from eawf.kernel.spec.intent import IntentBrief, source_brief_units
from eawf.workflow.propose.coverage import (
    coverage_gaps,
    significant_tokens,
    source_brief_coverage_gaps,
)
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


# ---- source_brief_units: extraction over referenced brief documents ----------

_BRIEF_BODY = "Implement the parser tokeniser module.\nWire the telemetry dashboard exporter.\n"


def _write_brief(tmp_path: Path, *, name: str = "brief.md") -> Path:
    """Write the two-deliverable brief document and return its path."""
    brief = tmp_path / name
    brief.write_text(_BRIEF_BODY, encoding="utf-8")
    return brief


def test_source_brief_units_extracts_units_from_referenced_document(tmp_path: Path) -> None:
    """Each deliverable line of a referenced brief becomes one source unit."""
    brief = _write_brief(tmp_path)
    intent = IntentBrief(
        problem="parser is missing",
        desired_outcome="parser ships",
        source_brief_ids=[str(brief)],
    )
    units = source_brief_units(intent, repo_root=tmp_path)
    assert [u.span_id for u in units] == ["U-000", "U-001"]
    assert "parser tokeniser module" in units[0].quote
    assert "telemetry dashboard exporter" in units[1].quote


def test_source_brief_units_skips_unresolvable_ref(tmp_path: Path) -> None:
    """A source-brief id with no on-disk document contributes no units."""
    intent = IntentBrief(
        problem="parser is missing",
        desired_outcome="parser ships",
        source_brief_ids=["RES-no-such-brief", "docs/missing.md"],
    )
    assert source_brief_units(intent, repo_root=tmp_path) == []


def test_source_brief_units_remints_monotonic_ids_across_documents(tmp_path: Path) -> None:
    """Two briefs never collide on a span id (re-minted monotonic sequence)."""
    first = _write_brief(tmp_path, name="first.md")
    second = _write_brief(tmp_path, name="second.md")
    intent = IntentBrief(
        problem="parser is missing",
        desired_outcome="parser ships",
        source_brief_ids=[str(first), str(second)],
    )
    units = source_brief_units(intent, repo_root=tmp_path)
    assert [u.span_id for u in units] == ["U-000", "U-001", "U-002", "U-003"]


# ---- source_brief_coverage_gaps: brief-document coverage diff ----------------


def test_source_brief_coverage_gaps_flags_dropped_deliverable(tmp_path: Path) -> None:
    """A source-brief deliverable no criterion covers is exactly one finding."""
    brief = _write_brief(tmp_path)
    intent = IntentBrief(
        problem="parser is missing",
        desired_outcome="parser ships",
        source_brief_ids=[str(brief)],
    )
    # Cover only the first deliverable; the exporter line is silently dropped.
    criteria = [grandfather_criterion("implement parser tokeniser module", index=1)]
    findings = source_brief_coverage_gaps(criteria, intent=intent, repo_root=tmp_path)
    assert len(findings) == 1
    assert findings[0].code == "EAWF022"
    assert findings[0].snippet == "U-001"


def test_source_brief_coverage_gaps_required_intent_empty_planned_steps(tmp_path: Path) -> None:
    """A required-intent wave with empty planned_steps still diffs the brief."""
    brief = _write_brief(tmp_path)
    intent = IntentBrief(
        problem="parser is missing",
        desired_outcome="parser ships",
        planned_steps=[],
        source_brief_ids=[str(brief)],
    )
    # No criterion covers either deliverable; both lines are findings even with
    # zero planned steps -- the no-op short-circuit no longer hides the drift.
    criteria = [grandfather_criterion("unrelated bookkeeping change", index=1)]
    findings = source_brief_coverage_gaps(criteria, intent=intent, repo_root=tmp_path)
    assert {f.snippet for f in findings} == {"U-000", "U-001"}


def test_source_brief_coverage_gaps_deferred_unit_is_clean(tmp_path: Path) -> None:
    """The dropped deliverable marked deferred yields zero findings."""
    brief = _write_brief(tmp_path)
    intent = IntentBrief(
        problem="parser is missing",
        desired_outcome="parser ships",
        source_brief_ids=[str(brief)],
    )
    criteria = [grandfather_criterion("implement parser tokeniser module", index=1)]
    deferrals = [
        DeferredDeliverable(
            span_id="U-001",
            reason="exporter is filed to the next phase backlog item",
            target="B999",
        )
    ]
    assert (
        source_brief_coverage_gaps(criteria, intent=intent, repo_root=tmp_path, deferrals=deferrals)
        == []
    )


def test_source_brief_coverage_gaps_fully_covered_is_clean(tmp_path: Path) -> None:
    """A brief whose every deliverable is covered yields zero findings."""
    brief = _write_brief(tmp_path)
    intent = IntentBrief(
        problem="parser is missing",
        desired_outcome="parser ships",
        source_brief_ids=[str(brief)],
    )
    criteria = [
        grandfather_criterion("implement parser tokeniser module", index=1),
        grandfather_criterion("wire telemetry dashboard exporter", index=2),
    ]
    assert source_brief_coverage_gaps(criteria, intent=intent, repo_root=tmp_path) == []


def test_source_brief_coverage_gaps_not_required_intent_is_noop(tmp_path: Path) -> None:
    """A brief with no source_brief_ids has no document to diff (clean no-op)."""
    intent = IntentBrief(
        problem="parser is missing",
        desired_outcome="parser ships",
        planned_steps=["implement parser tokeniser module"],
    )
    assert intent.is_required_intent is False
    criteria = [grandfather_criterion("unrelated change", index=1)]
    assert source_brief_coverage_gaps(criteria, intent=intent, repo_root=tmp_path) == []
