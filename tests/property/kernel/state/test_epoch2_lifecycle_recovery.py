"""No live state is a dead end, and no unknown outcome is invented.

The first half is a reachability walk: from every nonterminal state of
every entity there is a route to a terminal or recovery state, so nothing
can be parked in a status it can never leave.

The second half is the harder promise. A Batch that is ``MERGING`` and a
Run that has stopped answering both have an outcome nobody knows yet.
The tempting fix is a status that decides for them -- a fabricated
``LOST`` terminal, or a ``MERGING -> COMPLETED`` edge -- and either would
turn "we cannot tell" into a claim the record cannot support. So the
ambiguity stays a fact about the world: both states remain ordinary
nonterminal states, they carry an observability label rather than a
status, every route from them to a successful terminal is behind an
observation, and the only route out that needs no observation is the
operator recovery that records the work as abandoned rather than done.
"""

from __future__ import annotations

from collections import deque

import pytest
from hypothesis import given
from hypothesis import strategies as st

from eawf.kernel.state.epoch2.batch import BatchStatus
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.transitions import (
    AMBIGUOUS_STATES,
    ENTITY_STATUS_ENUM,
    OBSERVED_GUARD_FACTS,
    RECOVERY_STATES,
    SUCCESS_TERMINALS,
    TERMINAL_STATUSES,
    AmbiguityLabel,
    DenialCode,
    LifecycleEntity,
    LifecycleStatus,
    ambiguity_label,
    rows_from,
)
from eawf.workflow.lifecycle.epoch2 import (
    GuardContext,
    TransitionDenied,
    apply_transition,
    evaluate_edge,
)
from tests.unit.kernel.state._epoch2_transition_fixtures import AT, seed_record

pytestmark = pytest.mark.property

NONTERMINAL: list[tuple[LifecycleEntity, LifecycleStatus]] = [
    (entity, status)
    for entity, status_enum in ENTITY_STATUS_ENUM.items()
    for status in status_enum
    if str(status) not in TERMINAL_STATUSES[entity]
]


def _walk(entity: LifecycleEntity, start: LifecycleStatus) -> set[str]:
    """Return every status reachable from *start* by registered edges."""
    status_enum = ENTITY_STATUS_ENUM[entity]
    seen: set[str] = set()
    queue: deque[str] = deque([str(start)])
    while queue:
        current = queue.popleft()
        for row in rows_from(entity, status_enum(current)):
            target = str(row.to)
            if target in seen:
                continue
            seen.add(target)
            queue.append(target)
    return seen


# ---- no live state is a dead end --------------------------------------------


@given(origin=st.sampled_from(NONTERMINAL))
def test_every_nonterminal_state_reaches_a_terminal_or_recovery_state(
    origin: tuple[LifecycleEntity, LifecycleStatus],
) -> None:
    entity, status = origin
    reached = _walk(entity, status)
    resting = TERMINAL_STATUSES[entity] | RECOVERY_STATES[entity]
    assert reached & resting


@given(origin=st.sampled_from(NONTERMINAL))
def test_a_walk_never_leaves_the_entity_status_enum(
    origin: tuple[LifecycleEntity, LifecycleStatus],
) -> None:
    entity, status = origin
    declared = {str(member) for member in ENTITY_STATUS_ENUM[entity]}
    assert _walk(entity, status) <= declared


@given(origin=st.sampled_from(NONTERMINAL))
def test_every_nonterminal_state_has_somewhere_to_go(
    origin: tuple[LifecycleEntity, LifecycleStatus],
) -> None:
    entity, status = origin
    assert rows_from(entity, status)


def test_every_recovery_state_is_terminal() -> None:
    for entity, states in RECOVERY_STATES.items():
        assert states <= TERMINAL_STATUSES[entity]


# ---- the ambiguity stays observable -----------------------------------------


def test_the_labelled_states_are_ordinary_nonterminal_states() -> None:
    for (entity, status), label in AMBIGUOUS_STATES.items():
        status_enum = ENTITY_STATUS_ENUM[entity]
        assert status in {str(member) for member in status_enum}
        assert status not in TERMINAL_STATUSES[entity]
        assert ambiguity_label(entity, status_enum(status)) is label


def test_lost_is_a_label_on_a_running_run_not_a_status() -> None:
    assert AmbiguityLabel.LOST.value.upper() not in {member.value for member in RunStatus}
    assert AMBIGUOUS_STATES[(LifecycleEntity.RUN, str(RunStatus.RUNNING))] is AmbiguityLabel.LOST


def test_merging_is_labelled_on_the_stored_merging_status() -> None:
    assert (
        AMBIGUOUS_STATES[(LifecycleEntity.DELIVERY_BATCH, str(BatchStatus.MERGING))]
        is AmbiguityLabel.MERGING
    )
    assert ambiguity_label(LifecycleEntity.DELIVERY_BATCH, BatchStatus.MERGING) is not None


def test_an_unambiguous_state_carries_no_label() -> None:
    assert ambiguity_label(LifecycleEntity.RUN, RunStatus.QUEUED) is None
    assert ambiguity_label(LifecycleEntity.DELIVERY_BATCH, BatchStatus.ACTIVE) is None


def test_no_route_from_an_ambiguous_state_to_success_skips_an_observation() -> None:
    for (entity, status), _label in AMBIGUOUS_STATES.items():
        status_enum = ENTITY_STATUS_ENUM[entity]
        for row in rows_from(entity, status_enum(status)):
            if str(row.to) not in SUCCESS_TERMINALS[entity]:
                continue
            assert any(guard in OBSERVED_GUARD_FACTS for guard in row.guards)


def test_an_unobserved_exit_from_an_ambiguous_state_never_decides_the_outcome() -> None:
    # An exit that needs no observation may keep the record live -- a
    # running Run may still suspend -- but it may not finish it, unless
    # what it records is the operator abandoning the work.
    for (entity, status), _label in AMBIGUOUS_STATES.items():
        status_enum = ENTITY_STATUS_ENUM[entity]
        for row in rows_from(entity, status_enum(status)):
            if any(guard in OBSERVED_GUARD_FACTS for guard in row.guards):
                continue
            if str(row.to) not in TERMINAL_STATUSES[entity]:
                continue
            assert str(row.to) in RECOVERY_STATES[entity]


def test_merging_has_no_direct_edge_to_completed() -> None:
    outcome = evaluate_edge(
        LifecycleEntity.DELIVERY_BATCH, BatchStatus.MERGING, BatchStatus.COMPLETED
    )
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code is DenialCode.ILLEGAL_TRANSITION


def test_a_merging_batch_with_nothing_observed_makes_no_progress() -> None:
    record = seed_record(LifecycleEntity.DELIVERY_BATCH, BatchStatus.MERGING)
    before = record.model_dump(mode="json")
    for target, code in (
        (BatchStatus.MERGED_PENDING_RECONCILIATION, DenialCode.HOST_MERGE_UNOBSERVED),
        (BatchStatus.READY_TO_MERGE, DenialCode.HOST_MERGE_NOT_REFUSED),
        (BatchStatus.COMPLETED, DenialCode.ILLEGAL_TRANSITION),
    ):
        outcome = apply_transition(record, to=target, at=AT, ctx=GuardContext())
        assert isinstance(outcome, TransitionDenied)
        assert outcome.code is code
    assert record.model_dump(mode="json") == before
    assert ambiguity_label(LifecycleEntity.DELIVERY_BATCH, record.status) is AmbiguityLabel.MERGING


def test_a_run_that_stopped_answering_can_neither_complete_nor_fail() -> None:
    record = seed_record(LifecycleEntity.RUN, RunStatus.RUNNING)
    before = record.model_dump(mode="json")
    for target in (RunStatus.COMPLETED, RunStatus.FAILED):
        outcome = apply_transition(
            record,
            to=target,
            at=AT,
            ctx=GuardContext(),
            updates={"ended_at": "2026-09-08T02:00:00Z", "failure": None},
        )
        assert isinstance(outcome, TransitionDenied)
        assert outcome.code is DenialCode.RUN_REPORT_UNBOUND
    assert record.model_dump(mode="json") == before
    assert ambiguity_label(LifecycleEntity.RUN, record.status) is AmbiguityLabel.LOST


def test_a_lost_run_is_still_recoverable_by_the_operator() -> None:
    record = seed_record(LifecycleEntity.RUN, RunStatus.RUNNING)
    outcome = apply_transition(
        record,
        to=RunStatus.CANCELLED,
        at=AT,
        ctx=GuardContext(),
        updates={"ended_at": "2026-09-08T02:00:00Z"},
    )
    assert not isinstance(outcome, TransitionDenied)
    assert str(outcome.record.status) in RECOVERY_STATES[LifecycleEntity.RUN]


def test_a_merging_batch_is_still_recoverable_by_the_operator() -> None:
    record = seed_record(LifecycleEntity.DELIVERY_BATCH, BatchStatus.MERGING)
    outcome = apply_transition(
        record,
        to=BatchStatus.FAILED,
        at=AT,
        ctx=GuardContext(),
        updates={
            "failure": {
                "code": "host-outcome-unknown",
                "message": "The host stopped answering before the merge resolved.",
                "evidence_refs": [],
            }
        },
    )
    assert not isinstance(outcome, TransitionDenied)
    assert str(outcome.record.status) in RECOVERY_STATES[LifecycleEntity.DELIVERY_BATCH]
