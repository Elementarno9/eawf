"""Integration attempt state machine and the selected generation ledger."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.integration import (
    INTEGRATION_ATTEMPT_EDGES,
    INTEGRATION_TERMINAL_STATUSES,
    IntegrationAttempt,
    IntegrationAttemptStatus,
    IntegrationFailureKind,
    IntegrationGeneration,
    IntegrationGenerationLedger,
)
from eawf.kernel.delivery.receipts import RevisionRefKind, canonical_digest
from eawf.runtime.integration.generations import (
    IllegalIntegrationTransitionError,
    select_generation,
    transition_attempt,
)

REPOSITORY = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0001"
OTHER_BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0002"
TASK = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0004"
FOREIGN_TASK = "eawf://WSP-MAIN/PRJ-EAWF/REP-OTHER/task/EAWF-0004"
EVIDENCE = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"
AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LATER = AT + timedelta(minutes=5)

Status = IntegrationAttemptStatus

#: The edges of the integration state diagram, spelled out so a new edge
#: in the table has to be added here on purpose.
DECLARED_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        ("QUEUED", "PREPARING"),
        ("QUEUED", "TIMED_OUT"),
        ("QUEUED", "CANCELLED"),
        ("PREPARING", "APPLYING"),
        ("PREPARING", "STALE"),
        ("PREPARING", "FAILED"),
        ("PREPARING", "TIMED_OUT"),
        ("PREPARING", "CANCELLED"),
        ("APPLYING", "VERIFYING_TREE"),
        ("APPLYING", "STALE"),
        ("APPLYING", "BLOCKED"),
        ("APPLYING", "FAILED"),
        ("APPLYING", "TIMED_OUT"),
        ("VERIFYING_TREE", "SUCCEEDED"),
        ("VERIFYING_TREE", "STALE"),
        ("VERIFYING_TREE", "FAILED"),
    }
)

UNDECLARED_EDGES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (source, target)
        for source, target in itertools.product([status.value for status in Status], repeat=2)
        if (source, target) not in DECLARED_EDGES
    )
)

#: A failure kind and diagnostic each failing status accepts.
OUTCOMES: dict[Status, tuple[IntegrationFailureKind, str | None]] = {
    Status.STALE: (IntegrationFailureKind.BASE_MOVED, None),
    Status.BLOCKED: (IntegrationFailureKind.CONFLICT, EVIDENCE),
    Status.FAILED: (IntegrationFailureKind.APPLY_ERROR, None),
}


def _binding(
    generation: int,
    *,
    ref_kind: RevisionRefKind = RevisionRefKind.INTEGRATION,
    batch: str = BATCH,
) -> dict[str, Any]:
    return {
        "repository_ref": REPOSITORY,
        "ref_kind": ref_kind,
        "head_sha": f"{generation % 10}" * 40,
        "tree_sha": "c" * 40,
        "parent_sha": None,
        "batch_ref": batch,
        "integration_generation": generation,
        "manifest_digest": canonical_digest("manifest"),
        "criteria_digest": canonical_digest("criteria"),
        "policy_digest": canonical_digest("policy"),
        "environment_digest": None,
        "bound_at": AT,
    }


def _attempt_payload(status: Status = Status.QUEUED, **overrides: Any) -> dict[str, Any]:
    failure_kind, diagnostic_ref = OUTCOMES.get(status, (None, None))
    terminal = status in INTEGRATION_TERMINAL_STATUSES
    payload: dict[str, Any] = {
        "id": "INA-000004",
        "operation_attempt_id": "OPR-INT-000004",
        "batch_ref": BATCH,
        "task_ref": TASK,
        "candidate_bundle_id": "CB-00000013",
        "generation": 4,
        "source_base": _binding(3, ref_kind=RevisionRefKind.CANDIDATE),
        "selected_batch_base": _binding(3),
        "candidate_patch_digest": canonical_digest("patch"),
        "candidate_tree_digest": canonical_digest("tree"),
        "changed_paths": ["src/pkg/loader.py"],
        "affected_criterion_ids": ["CR-01"],
        "idempotency_key": "BAT-0001-EAWF-0004-CB-E04",
        "status": status,
        "failure_kind": failure_kind,
        "diagnostic_ref": diagnostic_ref,
        "requested_at": AT,
        "updated_at": AT,
        "terminal_at": AT if terminal else None,
    }
    return payload | overrides


def _attempt(status: Status = Status.QUEUED, **overrides: Any) -> IntegrationAttempt:
    return IntegrationAttempt.model_validate(_attempt_payload(status, **overrides))


def _generation(
    ordinal: int,
    *,
    parent: str | None,
    target: int,
    selected: bool = True,
    created_at: datetime = AT,
    **overrides: Any,
) -> IntegrationGeneration:
    payload: dict[str, Any] = {
        "id": f"ING-{ordinal:06d}",
        "batch_ref": BATCH,
        "generation": ordinal,
        "parent_generation_id": parent,
        "source_candidate_bundle_id": f"CB-{ordinal:08d}",
        "source_base": _binding(target, ref_kind=RevisionRefKind.CANDIDATE),
        "target_base": _binding(target),
        "integrated_revision": _binding(ordinal),
        "patch_digest": canonical_digest(f"patch-{ordinal}"),
        "diff_digest": canonical_digest(f"diff-{ordinal}"),
        "tree_digest": canonical_digest(f"tree-{ordinal}"),
        "changed_paths": ["src/pkg/loader.py"],
        "affected_task_refs": [TASK],
        "affected_criterion_ids": ["CR-01"],
        "integration_policy_digest": canonical_digest("integration-policy"),
        "selected": selected,
        "created_at": created_at,
    }
    return IntegrationGeneration.model_validate(payload | overrides)


def _ledger(*generations: IntegrationGeneration) -> IntegrationGenerationLedger:
    return IntegrationGenerationLedger.model_validate(
        {"batch_ref": BATCH, "generations": [item.model_dump() for item in generations]}
    )


# ---- declared edges -----------------------------------------------------------


def test_integration_attempt_edges_are_exactly_the_declared_diagram() -> None:
    table = {
        (source.value, target.value)
        for source, targets in INTEGRATION_ATTEMPT_EDGES.items()
        for target in targets
    }

    assert table == DECLARED_EDGES
    assert set(INTEGRATION_ATTEMPT_EDGES) == set(Status)


def test_integration_terminal_statuses_are_the_six_outcomes() -> None:
    assert {status.value for status in INTEGRATION_TERMINAL_STATUSES} == {
        "SUCCEEDED",
        "STALE",
        "BLOCKED",
        "FAILED",
        "TIMED_OUT",
        "CANCELLED",
    }


@pytest.mark.parametrize(("source", "target"), sorted(DECLARED_EDGES))
def test_transition_attempt_admits_each_declared_edge(source: str, target: str) -> None:
    to = Status(target)
    failure_kind, diagnostic_ref = OUTCOMES.get(to, (None, None))

    moved = transition_attempt(
        _attempt(Status(source)),
        to=to,
        at=LATER,
        failure_kind=failure_kind,
        diagnostic_ref=diagnostic_ref,
    )

    assert moved.status is to
    assert moved.failure_kind is failure_kind
    assert moved.updated_at == LATER
    assert moved.terminal_at == (LATER if to in INTEGRATION_TERMINAL_STATUSES else None)


@pytest.mark.parametrize(("source", "target"), UNDECLARED_EDGES)
def test_transition_attempt_refuses_each_undeclared_edge(source: str, target: str) -> None:
    to = Status(target)
    failure_kind, diagnostic_ref = OUTCOMES.get(to, (None, None))

    with pytest.raises(IllegalIntegrationTransitionError) as caught:
        transition_attempt(
            _attempt(Status(source)),
            to=to,
            at=LATER,
            failure_kind=failure_kind,
            diagnostic_ref=diagnostic_ref,
        )

    assert (caught.value.current, caught.value.requested) == (Status(source), to)
    assert caught.value.attempt_id == "INA-000004"


def test_transition_attempt_never_turns_a_timeout_into_a_cancellation() -> None:
    with pytest.raises(IllegalIntegrationTransitionError, match="TIMED_OUT -> CANCELLED"):
        transition_attempt(_attempt(Status.TIMED_OUT), to=Status.CANCELLED, at=LATER)


def test_transition_attempt_leaves_the_original_attempt_unchanged() -> None:
    original = _attempt(Status.QUEUED)

    transition_attempt(original, to=Status.PREPARING, at=LATER)

    assert original.status is Status.QUEUED
    assert original.updated_at == AT


def test_transition_attempt_accepts_a_move_at_the_last_update_instant() -> None:
    """Boundary: the same instant as the last update is not going back."""
    moved = transition_attempt(_attempt(Status.QUEUED), to=Status.PREPARING, at=AT)

    assert moved.updated_at == AT


def test_transition_attempt_refuses_to_move_back_in_time() -> None:
    attempt = _attempt(Status.PREPARING, updated_at=LATER)

    with pytest.raises(ValueError, match="back in time"):
        transition_attempt(attempt, to=Status.APPLYING, at=AT)


# ---- outcome fields -----------------------------------------------------------


@pytest.mark.parametrize("target", [Status.STALE, Status.BLOCKED, Status.FAILED])
def test_transition_attempt_requires_a_failure_kind_for_a_failing_outcome(target: Status) -> None:
    _, diagnostic_ref = OUTCOMES[target]

    with pytest.raises(ValidationError, match="requires a failure_kind"):
        transition_attempt(
            _attempt(Status.APPLYING), to=target, at=LATER, diagnostic_ref=diagnostic_ref
        )


@pytest.mark.parametrize("target", [Status.TIMED_OUT, Status.CANCELLED, Status.PREPARING])
def test_transition_attempt_refuses_a_failure_kind_where_none_is_admitted(
    target: Status,
) -> None:
    with pytest.raises(ValidationError, match="is not admitted"):
        transition_attempt(
            _attempt(Status.QUEUED),
            to=target,
            at=LATER,
            failure_kind=IntegrationFailureKind.APPLY_ERROR,
        )


def test_transition_attempt_refuses_a_failure_kind_foreign_to_the_status() -> None:
    with pytest.raises(ValidationError, match="base_moved is not admitted in BLOCKED"):
        transition_attempt(
            _attempt(Status.APPLYING),
            to=Status.BLOCKED,
            at=LATER,
            failure_kind=IntegrationFailureKind.BASE_MOVED,
            diagnostic_ref=EVIDENCE,
        )


def test_transition_attempt_requires_a_diagnostic_for_a_blocked_attempt() -> None:
    with pytest.raises(ValidationError, match="requires the diagnostic_ref"):
        transition_attempt(
            _attempt(Status.APPLYING),
            to=Status.BLOCKED,
            at=LATER,
            failure_kind=IntegrationFailureKind.CONFLICT,
        )


def test_transition_attempt_refuses_a_diagnostic_before_the_outcome() -> None:
    with pytest.raises(ValidationError, match="cannot carry a diagnostic_ref"):
        transition_attempt(
            _attempt(Status.QUEUED), to=Status.PREPARING, at=LATER, diagnostic_ref=EVIDENCE
        )


def test_integration_attempt_rejects_terminal_at_on_a_running_attempt() -> None:
    with pytest.raises(ValidationError, match="terminal_at must be set exactly"):
        _attempt(Status.APPLYING, terminal_at=AT)


def test_integration_attempt_rejects_a_terminal_attempt_without_terminal_at() -> None:
    with pytest.raises(ValidationError, match="terminal_at must be set exactly"):
        _attempt(Status.SUCCEEDED, terminal_at=None)


def test_integration_attempt_rejects_an_update_before_the_request() -> None:
    with pytest.raises(ValidationError, match="updated_at cannot precede"):
        _attempt(Status.QUEUED, requested_at=LATER)


# ---- attempt subject ----------------------------------------------------------


def test_integration_attempt_accepts_the_next_ordinal_after_the_base() -> None:
    """Boundary: the smallest admitted target is one past the selected base."""
    assert _attempt(generation=4).generation == 4


def test_integration_attempt_rejects_a_target_at_the_selected_base() -> None:
    with pytest.raises(ValidationError, match="must follow the selected base generation 3"):
        _attempt(generation=3)


def test_integration_attempt_rejects_a_base_taken_on_another_batch() -> None:
    with pytest.raises(ValidationError, match="selected_batch_base binds"):
        _attempt(selected_batch_base=_binding(3, batch=OTHER_BATCH))


def test_integration_attempt_rejects_a_task_in_another_repository() -> None:
    with pytest.raises(ValidationError, match="is not in the repository"):
        _attempt(task_ref=FOREIGN_TASK)


def test_integration_attempt_rejects_an_empty_change_set() -> None:
    with pytest.raises(ValidationError, match="changed_paths"):
        _attempt(changed_paths=[])


def test_integration_attempt_rejects_a_repeated_changed_path() -> None:
    with pytest.raises(ValidationError, match="changed_paths repeats"):
        _attempt(changed_paths=["src/pkg/loader.py", "src/pkg/loader.py"])


def test_integration_attempt_rejects_self_supersession() -> None:
    with pytest.raises(ValidationError, match="cannot supersede itself"):
        _attempt(supersedes_id="INA-000004")


def test_integration_attempt_rejects_an_unknown_status() -> None:
    with pytest.raises(ValidationError, match="status"):
        IntegrationAttempt.model_validate(_attempt_payload() | {"status": "MERGED"})


# ---- generations and the selected head ------------------------------------------


def test_integration_generation_ledger_starts_without_a_head() -> None:
    assert _ledger().head is None


def test_select_generation_makes_the_first_generation_the_head() -> None:
    first = _generation(2, parent=None, target=1)

    ledger = select_generation(_ledger(), first)

    assert ledger.head == first
    assert [item.selected for item in ledger.generations] == [True]


def test_select_generation_releases_the_previous_head() -> None:
    first = _generation(2, parent=None, target=1)
    second = _generation(3, parent="ING-000002", target=2, created_at=LATER)

    ledger = select_generation(select_generation(_ledger(), first), second)

    assert ledger.head == second
    assert [item.generation for item in ledger.generations] == [2, 3]
    assert [item.selected for item in ledger.generations] == [False, True]


def test_select_generation_keeps_one_selected_head_across_many_selections() -> None:
    ledger = _ledger()
    parent: str | None = None
    for ordinal in range(2, 7):
        ledger = select_generation(ledger, _generation(ordinal, parent=parent, target=ordinal - 1))
        parent = f"ING-{ordinal:06d}"

    assert [item.generation for item in ledger.generations] == [2, 3, 4, 5, 6]
    assert sum(item.selected for item in ledger.generations) == 1
    assert ledger.head is not None and ledger.head.generation == 6


def test_select_generation_leaves_the_input_ledger_unchanged() -> None:
    before = _ledger(_generation(2, parent=None, target=1))

    select_generation(before, _generation(3, parent="ING-000002", target=2))

    assert [item.selected for item in before.generations] == [True]


def test_select_generation_refuses_an_unselected_generation() -> None:
    with pytest.raises(ValueError, match="must be selected"):
        select_generation(_ledger(), _generation(2, parent=None, target=1, selected=False))


def test_select_generation_refuses_a_repeated_ordinal() -> None:
    ledger = _ledger(_generation(3, parent=None, target=2))
    repeat = _generation(3, parent="ING-000003", target=2, id="ING-000009")

    with pytest.raises(ValidationError, match="ordinal 3 does not follow 3"):
        select_generation(ledger, repeat)


def test_select_generation_refuses_a_decreasing_ordinal() -> None:
    ledger = _ledger(_generation(4, parent=None, target=3))

    with pytest.raises(ValidationError, match="ordinal 2 does not follow 4"):
        select_generation(ledger, _generation(2, parent="ING-000004", target=1))


def test_select_generation_refuses_a_descendant_of_a_superseded_generation() -> None:
    ledger = _ledger(
        _generation(2, parent=None, target=1, selected=False),
        _generation(3, parent="ING-000002", target=2),
    )
    stale = _generation(4, parent="ING-000003", target=2)

    with pytest.raises(ValidationError, match="applied on generation 2, not the head 3"):
        select_generation(ledger, stale)


def test_select_generation_refuses_a_generation_that_skips_its_parent() -> None:
    ledger = _ledger(_generation(2, parent=None, target=1))

    with pytest.raises(ValidationError, match="expected 'ING-000002'"):
        select_generation(ledger, _generation(3, parent=None, target=2))


def test_select_generation_refuses_another_batch() -> None:
    foreign = _generation(
        2,
        parent=None,
        target=1,
        batch_ref=OTHER_BATCH,
        source_base=_binding(1, ref_kind=RevisionRefKind.CANDIDATE, batch=OTHER_BATCH),
        target_base=_binding(1, batch=OTHER_BATCH),
        integrated_revision=_binding(2, batch=OTHER_BATCH),
    )

    with pytest.raises(ValidationError, match="belongs to"):
        select_generation(_ledger(), foreign)


def test_integration_generation_ledger_rejects_two_selected_generations() -> None:
    with pytest.raises(ValidationError, match="exactly the newest generation must be selected"):
        _ledger(
            _generation(2, parent=None, target=1),
            _generation(3, parent="ING-000002", target=2),
        )


def test_integration_generation_ledger_rejects_a_history_without_a_head() -> None:
    with pytest.raises(ValidationError, match="found none"):
        _ledger(_generation(2, parent=None, target=1, selected=False))


def test_integration_generation_ledger_rejects_a_selected_older_generation() -> None:
    with pytest.raises(ValidationError, match="exactly the newest generation must be selected"):
        _ledger(
            _generation(2, parent=None, target=1),
            _generation(3, parent="ING-000002", target=2, selected=False),
        )


def test_integration_generation_ledger_rejects_a_generation_created_earlier() -> None:
    with pytest.raises(ValidationError, match="predates"):
        _ledger(
            _generation(2, parent=None, target=1, selected=False, created_at=LATER),
            _generation(3, parent="ING-000002", target=2, created_at=AT),
        )


def test_integration_generation_ledger_rejects_a_repeated_id() -> None:
    with pytest.raises(ValidationError, match="generations repeats ING-000002"):
        _ledger(
            _generation(2, parent=None, target=1, selected=False),
            _generation(3, parent=None, target=2, id="ING-000002"),
        )


def test_integration_generation_rejects_an_integrated_revision_of_another_ordinal() -> None:
    with pytest.raises(ValidationError, match="binds generation 5, not 2"):
        _generation(2, parent=None, target=1, integrated_revision=_binding(5))


def test_integration_generation_rejects_an_integrated_revision_off_the_integration_ref() -> None:
    candidate = _binding(2, ref_kind=RevisionRefKind.CANDIDATE)

    with pytest.raises(ValidationError, match="bound on the integration ref"):
        _generation(2, parent=None, target=1, integrated_revision=candidate)


def test_integration_generation_rejects_a_target_base_at_its_own_ordinal() -> None:
    with pytest.raises(ValidationError, match="target_base generation 2 must precede"):
        _generation(2, parent=None, target=2)


def test_integration_generation_rejects_an_affected_task_in_another_repository() -> None:
    with pytest.raises(ValidationError, match="is not in the repository"):
        _generation(2, parent=None, target=1, affected_task_refs=[FOREIGN_TASK])


def test_integration_generation_rejects_parenting_itself() -> None:
    with pytest.raises(ValidationError, match="cannot parent itself"):
        _generation(2, parent="ING-000002", target=1)


def test_integration_generation_is_immutable() -> None:
    generation = _generation(2, parent=None, target=1)

    with pytest.raises(ValidationError, match="frozen"):
        generation.selected = False
