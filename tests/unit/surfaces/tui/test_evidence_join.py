"""Unit tests for the evidence->criterion join.

The pure :func:`~eawf.surfaces.tui.modes.evidence.join_evidence_to_criteria`
maps each :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` to the
in-scope criterion id it references and groups every record that joins no
in-scope criterion under the :data:`~eawf.surfaces.tui.modes.evidence.ORPHAN_SECTION`
bucket -- so an evidence row whose criterion was dropped (or never existed)
stays visible rather than silently vanishing.

These tests need no Textual mount: the join is a pure function over typed
fixtures, covering the matched-row path, the orphan-bucket path, the
boundary cases (no records, no criteria), and the multi-criterion record.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.surfaces.tui.modes.evidence import (
    ORPHAN_SECTION,
    EvidenceJoin,
    join_evidence_to_criteria,
)

_NOW = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)


def _record(
    record_id: str,
    *,
    produced_by: str = "tool",
    refs: list[str] | None = None,
    criterion_id: str | None = None,
) -> EvidenceRecord:
    """Build an evidence record referencing *refs* / *criterion_id*."""
    metrics = {"criterion_id": criterion_id} if criterion_id is not None else None
    return EvidenceRecord(
        id=record_id,
        scope_id="P01-I01-W01",
        produced_by=produced_by,  # type: ignore[arg-type]
        evidence_kind="deterministic",
        status="pass",
        summary=f"row {record_id}",
        refs=refs or [],
        metrics=metrics,
        created_at=_NOW,
    )


def test_join_maps_record_to_its_criterion_id() -> None:
    """A record naming an in-scope criterion lands under that criterion id."""
    record = _record("EV-1", criterion_id="CR-01", refs=["G-01", "CR-01"])
    join = join_evidence_to_criteria(["CR-01", "CR-02"], [record])
    assert isinstance(join, EvidenceJoin)
    assert set(join.matched) == {"CR-01"}
    assert join.matched["CR-01"] == (record,)
    assert join.orphans == ()


def test_join_groups_unmatched_evidence_under_orphan() -> None:
    """A record naming no in-scope criterion is bucketed as an orphan."""
    matched_row = _record("EV-1", criterion_id="CR-01")
    orphan_row = _record("EV-2", criterion_id="CR-99", refs=["CR-99"])
    join = join_evidence_to_criteria(["CR-01"], [matched_row, orphan_row])
    assert set(join.matched) == {"CR-01"}
    assert join.orphans == (orphan_row,)


def test_join_orphan_section_name_is_orphan() -> None:
    """The orphan bucket's section heading is the canonical ``orphan`` token."""
    assert ORPHAN_SECTION == "orphan"


def test_join_matches_via_refs_when_no_metric_criterion() -> None:
    """A record with no metrics joins via its refs list."""
    record = _record("EV-1", refs=["CR-02"], criterion_id=None)
    join = join_evidence_to_criteria(["CR-01", "CR-02"], [record])
    assert join.matched["CR-02"] == (record,)
    assert join.orphans == ()


def test_join_record_referencing_multiple_criteria_lands_under_each() -> None:
    """A record naming two in-scope criteria lands under both ids."""
    record = _record("EV-1", criterion_id="CR-01", refs=["CR-01", "CR-02"])
    join = join_evidence_to_criteria(["CR-01", "CR-02"], [record])
    assert join.matched["CR-01"] == (record,)
    assert join.matched["CR-02"] == (record,)
    assert join.orphans == ()


def test_join_multiple_records_per_criterion_preserve_input_order() -> None:
    """Two records joining one criterion group in input order."""
    first = _record("EV-1", criterion_id="CR-01")
    second = _record("EV-2", criterion_id="CR-01")
    join = join_evidence_to_criteria(["CR-01"], [first, second])
    assert join.matched["CR-01"] == (first, second)


def test_join_no_records_is_empty_both_buckets() -> None:
    """No evidence rows yields empty matched + orphan buckets (boundary)."""
    join = join_evidence_to_criteria(["CR-01"], [])
    assert join.matched == {}
    assert join.orphans == ()


def test_join_no_in_scope_criteria_buckets_all_as_orphans() -> None:
    """With no in-scope criteria every record is an orphan (boundary)."""
    rows = [_record("EV-1", criterion_id="CR-01"), _record("EV-2", criterion_id="CR-02")]
    join = join_evidence_to_criteria([], rows)
    assert join.matched == {}
    assert join.orphans == tuple(rows)
