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

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from eawf.kernel.migration.epoch2.lifecycle import (
    BATCH_HEAD_BINDING_FIELD,
    MILESTONE_ACCEPTANCE_FIELDS,
    DeferralReason,
    map_phase_row,
)
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan, LifecycleImportPlan
from eawf.kernel.migration.epoch2.runs import (
    ATTEMPT_RUN_ID_SEPARATOR,
    FAILED_RUN_STATUS,
    SUCCEEDED_RUN_STATUS,
    UNCLASSIFIED_RUN_STATUS,
    BindingRefusal,
    MintedRun,
    ReportBindingIndex,
    RunBinding,
    RunSource,
    attempt_entries,
    classify_attempt_status,
    mint_attempt_runs,
    mint_claim_run,
    report_ledger_rows,
)
from eawf.kernel.migration.epoch2.status_map import SourceLifecycle
from tests.property.kernel.migration.conftest import (
    DANGLING_SESSION_ID,
    attempt_map_plan,
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


# --- The attempt map: one Run per attempt, and no outcome without a report ---
#
# The live epoch-1 corpus binds no role report to any provider session id,
# so its attempts can only ever land unclassified. The attempt-map corpus
# is the fixture where the other arms fire, and it is written rather than
# harvested for exactly that reason: making the SUCCEEDED arm fire on the
# real corpus would mean inventing a report nobody filed.

#: Which wave carries which shape of attempt.
BOUND_ZERO_WAVE = "P01-I01-W01"
NO_ATTEMPT_WAVE = "P01-I01-W02"
BOUND_NONZERO_WAVE = "P01-I01-W03"
SHARED_ATTEMPT_WAVES = ("P01-I01-W04", "P01-I01-W05")
EMPTY_ATTEMPT_TABLE_WAVE = "P01-I01-W06"
BOUND_NO_EXIT_WAVE = "P01-I01-W07"
SHARED_SESSION_WAVE = "P01-I01-W08"
TWO_REPORT_WAVE = "P01-I01-W09"
NAMELESS_ATTEMPT_WAVE = "P01-I01-W10"

#: The provider session ids the corpus names.
BOUND_ZERO_SESSION = "PS-BOUND-ZERO"
UNBOUND_ZERO_SESSION = "PS-UNBOUND-ZERO"
BOUND_NONZERO_SESSION = "PS-BOUND-NONZERO"
SHARED_SESSION = "PS-SHARED"

#: How many dispatch attempts the attempt-map document records, counted
#: from the fixture rather than from the plan the assertion is about.
ATTEMPT_ENTRY_COUNT = 9

#: A binding standing in for "the source really reported on this episode",
#: used where the test drives the classifier directly.
_BINDING = RunBinding(
    provider_session_id="PS-A",
    session_id="S001",
    report_id="AR-executor-01",
    report_role="executor",
)


def _attempt_runs_by_wave(plan: CorpusImportPlan) -> dict[str, tuple[MintedRun, ...]]:
    """Group the attempt-minted Runs of ``plan`` by the wave they belong to."""
    grouped: dict[str, list[MintedRun]] = {}
    for run in plan.lifecycle.minted_runs():
        if run.run_source is RunSource.WAVE_ATTEMPT:
            grouped.setdefault(run.wave_id, []).append(run)
    return {wave_id: tuple(runs) for wave_id, runs in grouped.items()}


def _run_for(plan: CorpusImportPlan, wave_id: str) -> MintedRun:
    """Return the single attempt-minted Run of ``wave_id``."""
    return _attempt_runs_by_wave(plan)[wave_id][0]


def test_attempt_map_mints_exactly_one_run_per_attempt_entry(
    attempt_plan: CorpusImportPlan,
) -> None:
    """The attempt table is the population and the Run set is a bijection on it."""
    runs = _attempt_runs_by_wave(attempt_plan)

    assert sum(len(group) for group in runs.values()) == ATTEMPT_ENTRY_COUNT
    assert len(runs[BOUND_ZERO_WAVE]) == 2
    assert [run.attempt_key for run in runs[BOUND_ZERO_WAVE]] == ["1", "2"]
    for wave_id in (
        BOUND_NONZERO_WAVE,
        BOUND_NO_EXIT_WAVE,
        SHARED_SESSION_WAVE,
        TWO_REPORT_WAVE,
        NAMELESS_ATTEMPT_WAVE,
        *SHARED_ATTEMPT_WAVES,
    ):
        assert len(runs[wave_id]) == 1


def test_attempt_map_wave_without_an_attempt_entry_mints_no_run(
    attempt_plan: CorpusImportPlan,
) -> None:
    """An absent attempt table and an empty one both mint nothing."""
    runs = _attempt_runs_by_wave(attempt_plan)

    assert NO_ATTEMPT_WAVE not in runs
    assert EMPTY_ATTEMPT_TABLE_WAVE not in runs
    assert attempt_plan.lifecycle.record_for(NO_ATTEMPT_WAVE).minted_runs == ()
    assert attempt_plan.lifecycle.record_for(EMPTY_ATTEMPT_TABLE_WAVE).minted_runs == ()


def test_attempt_map_zero_exit_status_alone_never_yields_succeeded(
    attempt_plan: CorpusImportPlan,
) -> None:
    """An attempt that exited zero with no report bound stays unclassified."""
    unbound = next(
        run for run in attempt_plan.lifecycle.minted_runs() if run.source_id == UNBOUND_ZERO_SESSION
    )

    assert unbound.exit_status == 0
    assert unbound.binding is None
    assert unbound.status == UNCLASSIFIED_RUN_STATUS
    assert unbound.status != SUCCEEDED_RUN_STATUS


def test_attempt_map_bound_report_maps_exit_zero_to_succeeded(
    attempt_plan: CorpusImportPlan,
) -> None:
    bound = next(
        run for run in attempt_plan.lifecycle.minted_runs() if run.source_id == BOUND_ZERO_SESSION
    )

    assert bound.exit_status == 0
    assert bound.binding is not None
    assert bound.binding.report_role == "executor"
    assert bound.status == SUCCEEDED_RUN_STATUS


def test_attempt_map_bound_report_maps_nonzero_exit_to_failed(
    attempt_plan: CorpusImportPlan,
) -> None:
    """The binding is read from every role-report ledger, not only the executor's."""
    bound = next(
        run
        for run in attempt_plan.lifecycle.minted_runs()
        if run.source_id == BOUND_NONZERO_SESSION
    )

    assert bound.exit_status == 1
    assert bound.binding is not None
    assert bound.binding.report_role == "auditor"
    assert bound.status == FAILED_RUN_STATUS


def test_attempt_map_bound_report_without_an_exit_status_stays_unclassified(
    attempt_plan: CorpusImportPlan,
) -> None:
    """Half the evidence classifies nothing: the report alone is not an outcome."""
    run = _run_for(attempt_plan, BOUND_NO_EXIT_WAVE)

    assert run.exit_status is None
    assert run.binding is not None
    assert run.status == UNCLASSIFIED_RUN_STATUS


def test_attempt_map_a_provider_session_shared_by_two_attempts_binds_nothing(
    attempt_plan: CorpusImportPlan,
) -> None:
    """One report cannot say which of two episodes it reports on."""
    bindings = attempt_plan.lifecycle.source_index.report_bindings
    runs = [run for run in attempt_plan.lifecycle.minted_runs() if run.source_id == SHARED_SESSION]

    assert len(runs) == 2
    assert {run.wave_id for run in runs} == set(SHARED_ATTEMPT_WAVES)
    assert bindings.binding_for(SHARED_SESSION) is None
    assert bindings.refusal_for(SHARED_SESSION) is BindingRefusal.SHARED_ACROSS_ATTEMPTS
    for run in runs:
        assert run.exit_status == 0
        assert run.status == UNCLASSIFIED_RUN_STATUS


def test_attempt_map_a_provider_session_declared_by_two_sessions_binds_nothing(
    attempt_plan: CorpusImportPlan,
) -> None:
    bindings = attempt_plan.lifecycle.source_index.report_bindings
    run = _run_for(attempt_plan, SHARED_SESSION_WAVE)

    assert bindings.refusal_for(run.source_id) is BindingRefusal.SHARED_ACROSS_SESSIONS
    assert run.status == UNCLASSIFIED_RUN_STATUS


def test_attempt_map_two_reports_on_one_session_bind_nothing(
    attempt_plan: CorpusImportPlan,
) -> None:
    """Picking one of two reports would assert an attribution the source lacks."""
    bindings = attempt_plan.lifecycle.source_index.report_bindings
    run = _run_for(attempt_plan, TWO_REPORT_WAVE)

    assert bindings.refusal_for(run.source_id) is BindingRefusal.SEVERAL_REPORTS_NAME_THE_SESSION
    assert run.status == UNCLASSIFIED_RUN_STATUS


def test_attempt_map_an_attempt_without_a_provider_id_is_addressed_synthetically(
    attempt_plan: CorpusImportPlan,
) -> None:
    """A nameless attempt is still one attempt, and still binds nothing."""
    run = _run_for(attempt_plan, NAMELESS_ATTEMPT_WAVE)

    assert run.source_id == f"{NAMELESS_ATTEMPT_WAVE}{ATTEMPT_RUN_ID_SEPARATOR}1"
    assert run.binding is None
    assert run.status == UNCLASSIFIED_RUN_STATUS


def test_attempt_map_import_is_idempotent() -> None:
    assert attempt_map_plan() == attempt_map_plan()


def test_attempt_map_every_run_names_the_wave_and_attempt_it_came_from(
    attempt_plan: CorpusImportPlan,
) -> None:
    for run in attempt_plan.lifecycle.minted_runs():
        assert run.wave_id
        if run.run_source is RunSource.WAVE_ATTEMPT:
            assert run.attempt_key is not None
        else:
            assert run.attempt_key is None


@given(exit_status=st.integers(min_value=-2_147_483_648, max_value=2_147_483_647))
def test_attempt_map_classification_without_a_binding_is_always_unclassified(
    exit_status: int,
) -> None:
    """No exit status, zero or otherwise, classifies an unbound attempt."""
    assert classify_attempt_status(exit_status=exit_status, binding=None) == UNCLASSIFIED_RUN_STATUS


@given(exit_status=st.integers(min_value=-2_147_483_648, max_value=2_147_483_647))
def test_attempt_map_classification_with_a_binding_splits_on_zero(exit_status: int) -> None:
    """With a report bound, and only then, the exit status decides the outcome."""
    status = classify_attempt_status(exit_status=exit_status, binding=_BINDING)

    assert status == (SUCCEEDED_RUN_STATUS if exit_status == 0 else FAILED_RUN_STATUS)


def test_attempt_map_classification_of_an_absent_exit_status_is_unclassified() -> None:
    assert classify_attempt_status(exit_status=None, binding=_BINDING) == UNCLASSIFIED_RUN_STATUS
    assert classify_attempt_status(exit_status=None, binding=None) == UNCLASSIFIED_RUN_STATUS


def test_attempt_map_attempt_entries_reads_an_absent_or_mis_shaped_table() -> None:
    """Absent, null and non-object attempt tables all read as no attempts."""
    assert attempt_entries({}) == ()
    assert attempt_entries({"sessions": None}) == ()
    assert attempt_entries({"sessions": []}) == ()
    assert attempt_entries({"sessions": {"1": "not a row"}}) == ()


def test_attempt_map_attempt_entries_orders_a_single_and_a_multi_entry_table() -> None:
    single = attempt_entries({"sessions": {"1": {"session_id": "PS-A"}}})
    multi = attempt_entries(
        {"sessions": {"2": {"session_id": "PS-B"}, "1": {"session_id": "PS-A"}}}
    )

    assert [key for key, _ in single] == ["1"]
    assert [key for key, _ in multi] == ["1", "2"]


def test_attempt_map_boolean_exit_status_is_read_as_no_exit_status() -> None:
    """A boolean in the exit-status slot is a mis-shaped row, not exit 0 or 1."""
    runs = mint_attempt_runs(
        wave_id="P01-I01-W01",
        row={"sessions": {"1": {"session_id": "PS-A", "exit_status": False}}},
        bindings=ReportBindingIndex(bindings={}, refusals={}),
    )

    assert runs[0].exit_status is None
    assert runs[0].status == UNCLASSIFIED_RUN_STATUS


def test_attempt_map_mint_claim_run_rejects_an_empty_wave_id() -> None:
    with pytest.raises(ValidationError):
        mint_claim_run(wave_id="", provider_session_id="PS-A")


def test_attempt_map_mint_claim_run_rejects_an_empty_provider_session_id() -> None:
    with pytest.raises(ValidationError):
        mint_claim_run(wave_id="P01-I01-W01", provider_session_id="")


def test_attempt_map_report_ledger_rows_selects_only_the_report_ledgers() -> None:
    rows = report_ledger_rows(
        {
            "audit": ({"id": "A001"},),
            "executor_report": ({"id": "R2"},),
            "auditor_report": ({"id": "R1"},),
        }
    )

    assert [row["id"] for row in rows] == ["R1", "R2"]


def test_attempt_map_report_ledger_rows_of_a_store_with_no_reports_is_empty() -> None:
    assert report_ledger_rows({}) == ()
    assert report_ledger_rows({"audit": ({"id": "A001"},)}) == ()


def test_attempt_map_binding_index_ignores_a_report_with_no_usable_header() -> None:
    """A report the importer cannot read binds nothing rather than binding wrongly."""
    index = ReportBindingIndex.build(
        document={"agent_sessions": {"S001": {"runtime_session_id": "PS-A"}}},
        report_rows=(
            {"id": "R1"},
            {"id": "R2", "payload": "not an object"},
            {"id": "R3", "payload": {"header": {"session_id": "S001"}}},
        ),
    )

    assert index.binding_for("PS-A") is None
    assert index.refusal_for("PS-A") is BindingRefusal.NO_REPORT_NAMES_THE_SESSION


def test_attempt_map_binding_index_of_an_empty_document_binds_nothing() -> None:
    index = ReportBindingIndex.build(document={}, report_rows=())

    assert index.bindings == {}
    assert index.refusals == {}
    assert index.binding_for("PS-A") is None
    assert index.refusal_for("PS-A") is None
