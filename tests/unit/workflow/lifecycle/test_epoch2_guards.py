"""Every registered edge passes with its guard and denies without it.

The sweep runs both directions over the whole registry rather than over a
sample. Forwards: each edge, given the guard answers and the observed
facts it asks for, produces a successor whose status, revision and event
are what the row promised. Backwards: each guard on each edge, withheld
one at a time, produces that guard's own stable code -- and leaves the
input record byte-identical, because a denial that half-applied a move
would be worse than the move it refused.

Three refusals come before any guard. A record projected from epoch 1
refuses every native move, a terminal record has none left, and a move
whose target status makes a field a fact is refused when the caller did
not supply it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.domain_events import domain_event_name
from eawf.kernel.state.epoch2.transitions import (
    DENIAL_REMEDIATION,
    ENTITY_STATUS_ENUM,
    GUARD_DENIALS,
    OBSERVED_GUARD_FACTS,
    TERMINAL_STATUSES,
    TRANSITION_ROWS,
    DenialCode,
    LifecycleEntity,
    ObservedFact,
    TransitionGuard,
    TransitionRow,
)
from eawf.workflow.lifecycle.epoch2 import (
    ENTITY_OF_RECORD,
    GuardContext,
    TransitionAccepted,
    TransitionDenied,
    apply_transition,
    evaluate_edge,
    guard_satisfied,
)
from tests.unit.kernel.state._epoch2_transition_fixtures import (
    AT,
    legacy_task,
    observations_for,
    seed_record,
    updates_for,
)

pytestmark = pytest.mark.unit

#: The rows this module can drive against a real record. A Release is
#: registered in the same table but is not an epoch-2 record, so its edges
#: are exercised through the record-free evaluator.
RECORD_ROWS = [row for row in TRANSITION_ROWS if row.entity is not LifecycleEntity.RELEASE]


def _row_id(row: TransitionRow) -> str:
    """Return a readable parametrisation id for *row*."""
    return f"{row.entity.value}:{row.frm!s}->{row.to!s}"


def _satisfying_context(row: TransitionRow) -> GuardContext:
    """Return the context under which every guard of *row* holds."""
    return GuardContext(observations=observations_for(row))


# ---- every legal edge passes with its guard ---------------------------------


@pytest.mark.parametrize("row", RECORD_ROWS, ids=_row_id)
def test_a_legal_edge_produces_the_successor_the_row_promises(row: TransitionRow) -> None:
    record = seed_record(row.entity, row.frm)
    outcome = apply_transition(
        record,
        to=row.to,
        at=AT,
        ctx=_satisfying_context(row),
        updates=updates_for(row),
    )
    assert isinstance(outcome, TransitionAccepted)
    assert str(outcome.record.status) == str(row.to)
    assert outcome.record.revision == record.revision + 1
    assert outcome.record.updated_at == AT
    assert outcome.row is row


@pytest.mark.parametrize("row", RECORD_ROWS, ids=_row_id)
def test_a_legal_edge_emits_the_registered_event(row: TransitionRow) -> None:
    record = seed_record(row.entity, row.frm)
    outcome = apply_transition(
        record,
        to=row.to,
        at=AT,
        ctx=_satisfying_context(row),
        updates=updates_for(row),
    )
    assert isinstance(outcome, TransitionAccepted)
    assert outcome.event.name == domain_event_name(row.entity, row.verb)
    assert outcome.event.subject == record.urn
    assert outcome.event.occurred_at == AT
    assert outcome.event.revision == outcome.record.revision


@pytest.mark.parametrize("row", RECORD_ROWS, ids=_row_id)
def test_a_legal_edge_leaves_the_input_record_untouched(row: TransitionRow) -> None:
    record = seed_record(row.entity, row.frm)
    before = record.model_dump(mode="json")
    apply_transition(
        record,
        to=row.to,
        at=AT,
        ctx=_satisfying_context(row),
        updates=updates_for(row),
    )
    assert record.model_dump(mode="json") == before


@pytest.mark.parametrize("row", TRANSITION_ROWS, ids=_row_id)
def test_every_registered_edge_is_authorised_by_the_evaluator(row: TransitionRow) -> None:
    outcome = evaluate_edge(row.entity, row.frm, row.to, _satisfying_context(row))
    assert outcome is row


# ---- every guard denies with its own stable code ----------------------------


def _guarded_cases() -> list[tuple[TransitionRow, TransitionGuard]]:
    """Return one case per (edge, guard) pair in the registry."""
    return [(row, guard) for row in TRANSITION_ROWS for guard in row.guards]


@pytest.mark.parametrize(
    ("row", "guard"),
    _guarded_cases(),
    ids=lambda item: item.value if isinstance(item, TransitionGuard) else _row_id(item),
)
def test_a_withheld_guard_denies_with_its_own_code(
    row: TransitionRow, guard: TransitionGuard
) -> None:
    observations = observations_for(row)
    fact = OBSERVED_GUARD_FACTS.get(guard)
    ctx = GuardContext(
        unmet=frozenset({guard}) if fact is None else frozenset(),
        observations=observations - {fact} if fact is not None else observations,
    )
    outcome = evaluate_edge(row.entity, row.frm, row.to, ctx)
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code is GUARD_DENIALS[guard]
    assert outcome.remediation == DENIAL_REMEDIATION[GUARD_DENIALS[guard]]


@pytest.mark.parametrize(
    ("row", "guard"),
    [(row, guard) for row, guard in _guarded_cases() if row.entity is not LifecycleEntity.RELEASE],
    ids=lambda item: item.value if isinstance(item, TransitionGuard) else _row_id(item),
)
def test_a_withheld_guard_mutates_nothing(row: TransitionRow, guard: TransitionGuard) -> None:
    record = seed_record(row.entity, row.frm)
    before = record.model_dump(mode="json")
    fact = OBSERVED_GUARD_FACTS.get(guard)
    observations = observations_for(row)
    outcome = apply_transition(
        record,
        to=row.to,
        at=AT,
        ctx=GuardContext(
            unmet=frozenset({guard}) if fact is None else frozenset(),
            observations=observations - {fact} if fact is not None else observations,
        ),
        updates=updates_for(row),
    )
    assert isinstance(outcome, TransitionDenied)
    assert record.model_dump(mode="json") == before


def test_the_first_unmet_guard_is_the_one_surfaced() -> None:
    # The batch ready edge carries two guards in declaration order; an
    # operator fixing them one at a time needs the first, not an
    # arbitrary member of the set.
    row = next(
        candidate
        for candidate in TRANSITION_ROWS
        if candidate.entity is LifecycleEntity.DELIVERY_BATCH
        and str(candidate.to) == "READY_TO_MERGE"
    )
    ctx = GuardContext(unmet=frozenset(row.guards))
    outcome = evaluate_edge(row.entity, row.frm, row.to, ctx)
    assert isinstance(outcome, TransitionDenied)
    assert outcome.guard is row.guards[0]


# ---- observation-backed guards need the fact, not a claim -------------------


def test_an_observation_guard_is_unsatisfied_without_the_fact() -> None:
    assert not guard_satisfied(TransitionGuard.HOST_MERGE_OBSERVED, GuardContext())


def test_an_observation_guard_ignores_the_unmet_set() -> None:
    # Listing an observation guard as met is not how it is satisfied: the
    # fact has to be presented, so an empty observation set still denies.
    ctx = GuardContext(unmet=frozenset())
    assert not guard_satisfied(TransitionGuard.RUN_REPORT_BOUND, ctx)
    assert guard_satisfied(
        TransitionGuard.RUN_REPORT_BOUND,
        GuardContext(observations=frozenset({ObservedFact.RUN_REPORT_BOUND})),
    )


def test_a_predicate_guard_defaults_to_satisfied() -> None:
    assert guard_satisfied(TransitionGuard.LEASE_HELD, GuardContext())


def test_a_wrong_observation_does_not_open_the_edge() -> None:
    ctx = GuardContext(observations=frozenset({ObservedFact.HOST_MERGE_REFUSED}))
    assert not guard_satisfied(TransitionGuard.HOST_MERGE_OBSERVED, ctx)


# ---- the structural refusals ------------------------------------------------


def test_a_legacy_origin_record_refuses_a_native_transition() -> None:
    record = legacy_task()
    before = record.model_dump(mode="json")
    outcome = apply_transition(record, to=record.status.__class__("CLAIMED"), at=AT)
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code is DenialCode.LEGACY_ORIGIN_IMMUTABLE
    assert record.model_dump(mode="json") == before


def test_a_legacy_origin_record_is_refused_before_the_edge_is_even_read() -> None:
    record = legacy_task()
    outcome = apply_transition(record, to=record.status.__class__("COMPLETED"), at=AT)
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code is DenialCode.LEGACY_ORIGIN_IMMUTABLE


@pytest.mark.parametrize(
    ("entity", "status"),
    [
        (entity, status)
        for entity, status_enum in ENTITY_STATUS_ENUM.items()
        for status in status_enum
        if str(status) in TERMINAL_STATUSES[entity]
    ],
    ids=lambda item: str(item),
)
def test_a_terminal_record_refuses_every_move(entity: LifecycleEntity, status: Any) -> None:
    for target in ENTITY_STATUS_ENUM[entity]:
        outcome = evaluate_edge(entity, status, target)
        assert isinstance(outcome, TransitionDenied)
        assert outcome.code is DenialCode.TERMINAL_STATE


def test_a_terminal_record_mutates_nothing() -> None:
    record = seed_record(
        LifecycleEntity.TASK, ENTITY_STATUS_ENUM[LifecycleEntity.TASK]("COMPLETED")
    )
    before = record.model_dump(mode="json")
    outcome = apply_transition(
        record, to=ENTITY_STATUS_ENUM[LifecycleEntity.TASK]("PLANNED"), at=AT
    )
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code is DenialCode.TERMINAL_STATE
    assert record.model_dump(mode="json") == before


def test_a_missing_required_field_is_refused_rather_than_attempted() -> None:
    row = next(row for row in RECORD_ROWS if row.required_updates)
    record = seed_record(row.entity, row.frm)
    outcome = apply_transition(record, to=row.to, at=AT, ctx=_satisfying_context(row))
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code is DenialCode.MISSING_TRANSITION_FIELDS
    assert sorted(row.required_updates)[0] in outcome.message


def test_an_unlisted_edge_returns_illegal_transition() -> None:
    record = seed_record(LifecycleEntity.TASK, ENTITY_STATUS_ENUM[LifecycleEntity.TASK]("DRAFT"))
    before = record.model_dump(mode="json")
    outcome = apply_transition(
        record, to=ENTITY_STATUS_ENUM[LifecycleEntity.TASK]("COMPLETED"), at=AT
    )
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code is DenialCode.ILLEGAL_TRANSITION
    assert record.model_dump(mode="json") == before


def test_a_self_loop_is_not_a_registered_edge() -> None:
    outcome = evaluate_edge(
        LifecycleEntity.TASK,
        ENTITY_STATUS_ENUM[LifecycleEntity.TASK]("PLANNED"),
        ENTITY_STATUS_ENUM[LifecycleEntity.TASK]("PLANNED"),
    )
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code is DenialCode.ILLEGAL_TRANSITION


# ---- error paths ------------------------------------------------------------


def test_a_foreign_record_type_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="not a driven epoch-2 record"):
        apply_transition(
            object(),  # type: ignore[arg-type]
            to=ENTITY_STATUS_ENUM[LifecycleEntity.TASK]("PLANNED"),
            at=AT,
        )


def test_an_illegal_update_value_raises_rather_than_denying() -> None:
    row = next(
        candidate
        for candidate in RECORD_ROWS
        if candidate.entity is LifecycleEntity.DELIVERY_BATCH and str(candidate.to) == "ACTIVE"
    )
    record = seed_record(row.entity, row.frm)
    with pytest.raises(ValidationError):
        apply_transition(
            record,
            to=row.to,
            at=AT,
            ctx=_satisfying_context(row),
            updates={"target_branch": "a branch with a ? in it"},
        )


def test_an_unknown_status_of_the_wrong_entity_is_illegal_not_a_crash() -> None:
    outcome = evaluate_edge(
        LifecycleEntity.TRACK,
        ENTITY_STATUS_ENUM[LifecycleEntity.TRACK]("ACTIVE"),
        ENTITY_STATUS_ENUM[LifecycleEntity.TASK]("CLAIMED"),
    )
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code is DenialCode.ILLEGAL_TRANSITION


def test_rows_from_refuses_a_status_of_another_entity() -> None:
    with pytest.raises(KeyError):
        evaluate_edge(
            LifecycleEntity.TRACK,
            ENTITY_STATUS_ENUM[LifecycleEntity.TASK]("CLAIMED"),
            ENTITY_STATUS_ENUM[LifecycleEntity.TRACK]("RETIRED"),
        )


def test_every_driven_record_type_is_registered() -> None:
    assert set(ENTITY_OF_RECORD.values()) == set(LifecycleEntity) - {LifecycleEntity.RELEASE}
