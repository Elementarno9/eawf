"""The importer preserves source facts and invents none of them.

The sparse-history corpus is history with holes in it: a claim naming a
session that never existed, an iter that was closed and tagged with no
head binding to prove the merge, and two tracks with nothing in the
source saying which of them owns a phase. Each hole is a place where a
plausible value would make a record validate, and each is left open.

What the importer does instead is name the gap. A required epoch-2 field
with no source fact becomes a deferred field carrying the reason and, when
the source offers a choice, the candidates. That is a fact about the
source; a filled-in value would not be.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from eawf.kernel.migration.epoch2.lifecycle import (
    BATCH_HEAD_BINDING_FIELD,
    MILESTONE_ACCEPTANCE_FIELDS,
    UNCLASSIFIED_RUN_STATUS,
    DeferralReason,
    RunSource,
    map_phase_row,
)
from eawf.kernel.migration.epoch2.plan import LifecycleImportPlan
from eawf.kernel.migration.epoch2.status_map import SourceLifecycle
from tests.property.kernel.migration.conftest import (
    DANGLING_SESSION_ID,
    sparse_history_index,
    sparse_history_plan,
)

#: The wave whose claim resolves, and the one whose claim does not.
RESOLVING_WAVE = "P01-I01-W01"
DANGLING_WAVE = "P01-I01-W02"

#: The closed iter that carries merge and tag evidence.
MERGED_BATCH = "P01-I01"

#: The phase with no recorded track and more than one candidate.
UNASSIGNED_MILESTONE = "P01"

_INDEX = sparse_history_index()


def _phase_row(**overrides: object) -> dict[str, object]:
    """One epoch-1 phase row, overridable field by field."""
    row: dict[str, object] = {
        "id": "P09",
        "scope_id": "P09",
        "status": "closed",
        "title": "Deliver P09",
        "opened_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def test_missing_sessions_mint_no_runs(sparse_plan: LifecycleImportPlan) -> None:
    """A claim naming a session the corpus never held mints nothing."""
    dangling = sparse_plan.record_for(DANGLING_WAVE)

    assert dangling.minted_runs == ()
    assert dangling.legacy_session_ref is None
    assert dangling.legacy_refs["claim_session_id"] == DANGLING_SESSION_ID
    assert DANGLING_SESSION_ID not in _INDEX.session_ids


def test_minted_runs_come_only_from_a_resolving_claim_or_a_recorded_attempt(
    sparse_plan: LifecycleImportPlan,
) -> None:
    runs = sparse_plan.minted_runs()

    assert {run.run_source for run in runs} == {RunSource.RESOLVING_CLAIM, RunSource.WAVE_ATTEMPT}
    for run in runs:
        if run.run_source is RunSource.RESOLVING_CLAIM:
            assert run.source_id in _INDEX.session_ids
        else:
            assert run.source_id.startswith(RESOLVING_WAVE)


def test_minted_runs_never_claim_success_on_an_imported_corpus(
    sparse_plan: LifecycleImportPlan,
) -> None:
    """Success needs a bound role report, which no lifecycle row carries."""
    assert sparse_plan.minted_runs()
    for run in sparse_plan.minted_runs():
        assert run.status == UNCLASSIFIED_RUN_STATUS


def test_merge_and_tag_evidence_becomes_fact_without_an_approval(
    sparse_plan: LifecycleImportPlan,
) -> None:
    """The tag and the audit reference survive; the head binding does not appear."""
    batch = sparse_plan.record_for(MERGED_BATCH)

    assert batch.legacy_refs["candidate_tag"] == "v0.1.0"
    assert batch.legacy_refs["audit_id"] == "A001"
    assert batch.record["task_refs"] == [RESOLVING_WAVE, DANGLING_WAVE]
    assert BATCH_HEAD_BINDING_FIELD in batch.deferred_field_names()
    assert BATCH_HEAD_BINDING_FIELD not in batch.record


def test_a_completed_batch_never_yields_an_accepted_milestone(
    sparse_plan: LifecycleImportPlan,
) -> None:
    """Completing every Batch is not acceptance; only an operator accepts."""
    milestone = sparse_plan.record_for(UNASSIGNED_MILESTONE)

    assert milestone.target_status == "COMPLETED"
    for field in MILESTONE_ACCEPTANCE_FIELDS:
        assert field in milestone.deferred_field_names()
        assert field not in milestone.record


def test_an_ambiguous_track_stays_unassigned(sparse_plan: LifecycleImportPlan) -> None:
    """Two candidate tracks and no recorded choice leaves the choice open."""
    milestone = sparse_plan.record_for(UNASSIGNED_MILESTONE)
    deferral = next(
        field for field in milestone.deferred_fields if field.target_field == "primary_track_ref"
    )

    assert "primary_track_ref" not in milestone.record
    assert deferral.reason is DeferralReason.AMBIGUOUS_SOURCE_CANDIDATES
    assert deferral.candidates == _INDEX.track_ids
    assert len(deferral.candidates) > 1
    assert milestone.origin.confidence == "ambiguous"


def test_every_imported_record_names_the_source_row_it_came_from(
    sparse_plan: LifecycleImportPlan,
) -> None:
    """No record exists without a source location, which is what dangling needs."""
    document = {"phases", "iters", "waves", "backlog"}
    for record in sparse_plan.records:
        assert record.origin.kind == "legacy"
        assert record.origin.source_id
        assert record.origin.source_kind in document
        assert record.origin.source_digest is not None


def test_lifecycle_import_plan_build_is_idempotent_over_the_sparse_corpus() -> None:
    assert sparse_history_plan() == sparse_history_plan()


def test_a_source_with_no_tracks_defers_rather_than_inventing_one() -> None:
    """An empty candidate list is still an unanswered question, not an answer."""
    empty = sparse_history_index().model_copy(update={"track_ids": ()})
    record = map_phase_row(source_id="P09", row=_phase_row(), index=empty)
    deferral = next(
        field for field in record.deferred_fields if field.target_field == "primary_track_ref"
    )

    assert deferral.reason is DeferralReason.SOURCE_HAS_NO_FIELD
    assert deferral.candidates == ()


@given(
    track_id=st.text(min_size=1, max_size=24).filter(lambda value: value not in _INDEX.track_ids)
)
def test_a_track_the_source_lacks_is_never_assigned(track_id: str) -> None:
    """No string other than a real track id can end up owning a Milestone."""
    record = map_phase_row(source_id="P09", row=_phase_row(track_id=track_id), index=_INDEX)

    assert "primary_track_ref" not in record.record
    assert record.legacy_refs["track_id"] == track_id


@given(status=st.sampled_from(["planned", "active", "closed", "archived"]))
def test_no_phase_status_produces_an_acceptance_proof(status: str) -> None:
    """Not one arm of the phase status map yields an accepted binding."""
    record = map_phase_row(source_id="P09", row=_phase_row(status=status), index=_INDEX)

    for field in MILESTONE_ACCEPTANCE_FIELDS:
        assert field not in record.record


def test_every_backlog_row_reaches_a_task_without_a_batch(
    sparse_plan: LifecycleImportPlan,
) -> None:
    """A draft head is unplaced by definition, so it fabricates no placement."""
    rows = sparse_plan.for_lifecycle(SourceLifecycle.BACKLOG)

    assert rows
    for record in rows:
        assert record.target_status in {"DRAFT", "DROPPED"}
        assert "batch_ref" not in record.record
        assert record.criteria == ()
