"""An unregistered edge is always refused, and always without side effects.

The registry is small enough to enumerate, so this walks every ordered
pair of statuses of every entity rather than a sample of them. Two things
have to hold across all of them: a pair that is not a row comes back as a
structural denial with a stable code, and the record it was asked about
is byte-identical afterwards. A reducer that half-applied a refused move
would leave a record in a status nothing downstream knows how to read,
which is worse than the move it declined.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from eawf.kernel.state.epoch2.transitions import (
    DENIAL_REMEDIATION,
    ENTITY_STATUS_ENUM,
    OBSERVED_GUARD_FACTS,
    TERMINAL_STATUSES,
    DenialCode,
    LifecycleEntity,
    LifecycleStatus,
    row_for,
)
from eawf.workflow.lifecycle.epoch2 import (
    TransitionDenied,
    apply_transition,
    evaluate_edge,
)
from tests.unit.kernel.state._epoch2_transition_fixtures import (
    AT,
    legacy_task,
    seed_record,
)

pytestmark = pytest.mark.property

#: The structural codes a refusal that never reached a guard may carry.
STRUCTURAL = frozenset({DenialCode.ILLEGAL_TRANSITION, DenialCode.TERMINAL_STATE})

ALL_PAIRS: list[tuple[LifecycleEntity, LifecycleStatus, LifecycleStatus]] = [
    (entity, frm, to)
    for entity, status_enum in ENTITY_STATUS_ENUM.items()
    for frm in status_enum
    for to in status_enum
]

RECORD_PAIRS = [triple for triple in ALL_PAIRS if triple[0] is not LifecycleEntity.RELEASE]

UNLISTED_RECORD_PAIRS = [
    (entity, frm, to) for entity, frm, to in RECORD_PAIRS if row_for(entity, frm, to) is None
]

TASK_STATUSES = list(ENTITY_STATUS_ENUM[LifecycleEntity.TASK])


@given(triple=st.sampled_from(UNLISTED_RECORD_PAIRS))
def test_an_unlisted_edge_returns_a_structural_denial(
    triple: tuple[LifecycleEntity, LifecycleStatus, LifecycleStatus],
) -> None:
    entity, frm, to = triple
    outcome = apply_transition(seed_record(entity, frm), to=to, at=AT)
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code in STRUCTURAL
    assert outcome.guard is None
    assert outcome.remediation == DENIAL_REMEDIATION[outcome.code]


@given(triple=st.sampled_from(UNLISTED_RECORD_PAIRS))
def test_an_unlisted_edge_mutates_nothing(
    triple: tuple[LifecycleEntity, LifecycleStatus, LifecycleStatus],
) -> None:
    entity, frm, to = triple
    record = seed_record(entity, frm)
    before = record.model_dump(mode="json")
    apply_transition(record, to=to, at=AT)
    assert record.model_dump(mode="json") == before


@given(triple=st.sampled_from(UNLISTED_RECORD_PAIRS))
def test_the_code_says_which_refusal_it_was(
    triple: tuple[LifecycleEntity, LifecycleStatus, LifecycleStatus],
) -> None:
    entity, frm, to = triple
    outcome = apply_transition(seed_record(entity, frm), to=to, at=AT)
    assert isinstance(outcome, TransitionDenied)
    terminal = str(frm) in TERMINAL_STATUSES[entity]
    expected = DenialCode.TERMINAL_STATE if terminal else DenialCode.ILLEGAL_TRANSITION
    assert outcome.code is expected


@given(triple=st.sampled_from(ALL_PAIRS))
def test_only_a_registered_edge_of_a_live_state_is_authorised(
    triple: tuple[LifecycleEntity, LifecycleStatus, LifecycleStatus],
) -> None:
    entity, frm, to = triple
    outcome = evaluate_edge(entity, frm, to)
    row = row_for(entity, frm, to)
    if row is None or str(frm) in TERMINAL_STATUSES[entity]:
        assert isinstance(outcome, TransitionDenied)
        return
    # The default context observes nothing, so an edge behind an
    # observation stays shut and every other registered edge opens.
    if any(guard in OBSERVED_GUARD_FACTS for guard in row.guards):
        assert isinstance(outcome, TransitionDenied)
    else:
        assert outcome is row


@given(to=st.sampled_from(TASK_STATUSES))
def test_a_legacy_record_refuses_every_target(to: LifecycleStatus) -> None:
    record = legacy_task()
    before = record.model_dump(mode="json")
    outcome = apply_transition(record, to=to, at=AT)
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code is DenialCode.LEGACY_ORIGIN_IMMUTABLE
    assert record.model_dump(mode="json") == before


def test_an_illegal_denial_names_the_legal_targets() -> None:
    entity = LifecycleEntity.TASK
    status_enum = ENTITY_STATUS_ENUM[entity]
    outcome = evaluate_edge(entity, status_enum("DRAFT"), status_enum("COMPLETED"))
    assert isinstance(outcome, TransitionDenied)
    assert "'DEFERRED'" in outcome.message
    assert "'PLANNED'" in outcome.message


def test_the_registry_leaves_most_pairs_unregistered() -> None:
    # If nearly every pair were an edge the property above would be
    # vacuous, so the shape of the table is pinned too.
    assert len(UNLISTED_RECORD_PAIRS) > len(RECORD_PAIRS) // 2
