"""Pure reducers over the epoch-2 guarded transition registry.

A reducer here takes a record and a target status and returns either the
successor record or a typed denial. It never writes anything and never
reaches for a clock, a store or a host: the caller supplies the time, the
guard answers and the observed facts, so the same call is replayable and
gives the same answer in a test, in the CLI and in the daemon.

Denials are returned rather than raised because a denied transition is an
ordinary answer to an ordinary question -- "may this move happen yet?" --
and the surfaces that ask it need the code and the remediation, not a
stack trace. Every denial carries the record unchanged by construction:
the successor is built only after the last check passes.

Four refusals come before any guard runs. A record projected from the
previous epoch is immutable to this lifecycle; a terminal record has no
moves left; an unregistered edge does not exist; and a move whose target
status makes a field a fact is refused when the caller did not supply it,
rather than being handed to a validator that would reject it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, cast

from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.state.epoch2.domain_events import DomainEvent, domain_event_name
from eawf.kernel.state.epoch2.milestone import Milestone
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.track import Track
from eawf.kernel.state.epoch2.transitions import (
    DENIAL_REMEDIATION,
    GUARD_DENIALS,
    OBSERVED_GUARD_FACTS,
    DenialCode,
    LifecycleEntity,
    LifecycleStatus,
    ObservedFact,
    TransitionGuard,
    TransitionRow,
    is_terminal,
    row_for,
    rows_from,
)
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)

#: The records this reducer drives. A Release is registered in the same
#: table but is not an epoch-2 record: it carries no origin and no URN,
#: and its own module owns its successor construction, so only its edges
#: are evaluated here through :func:`evaluate_edge`.
LifecycleRecord = Track | Milestone | DeliveryBatch | Task | Run


#: Which entity's machine governs each record type.
ENTITY_OF_RECORD: Final[Mapping[type[LifecycleRecord], LifecycleEntity]] = {
    Track: LifecycleEntity.TRACK,
    Milestone: LifecycleEntity.MILESTONE,
    DeliveryBatch: LifecycleEntity.DELIVERY_BATCH,
    Task: LifecycleEntity.TASK,
    Run: LifecycleEntity.RUN,
}


@dataclass(frozen=True, slots=True)
class GuardContext:
    """What the caller found when it evaluated the named predicates.

    The two fields are deliberately asymmetric. A predicate the caller
    can compute from state it already holds defaults to satisfied and is
    listed only when it fails, which keeps a call site that exercises one
    guard from having to spell the other twenty-seven. A predicate that
    only the outside world can answer defaults to *unknown*, so an edge
    behind an observation stays shut until the fact is presented.

    Attributes:
        unmet: The guards the caller evaluated and found false.
        observations: The facts an adapter read back from the world.
    """

    unmet: frozenset[TransitionGuard] = field(default_factory=frozenset)
    observations: frozenset[ObservedFact] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class TransitionDenied:
    """A move that will not happen, with the code and the way forward.

    Attributes:
        entity: The entity whose machine refused the move.
        frm: Status the record is in; it still is.
        to: Status the caller asked for.
        code: The stable denial code a consumer groups by.
        remediation: One sentence saying what to do about it.
        guard: The guard that failed, or ``None`` for a structural
            refusal that no predicate was reached for.
        message: Human-readable detail naming the edge.
    """

    entity: LifecycleEntity
    frm: LifecycleStatus
    to: LifecycleStatus
    code: DenialCode
    remediation: str
    guard: TransitionGuard | None
    message: str


@dataclass(frozen=True, slots=True)
class TransitionAccepted[RecordT: LifecycleRecord]:
    """A move that happened, as a successor record plus its event.

    Attributes:
        record: The successor. The input record is untouched.
        row: The registry row the move was authorised by.
        event: The domain event the move emits, already named from the
            closed vocabulary.
    """

    record: RecordT
    row: TransitionRow
    event: DomainEvent


def guard_satisfied(guard: TransitionGuard, ctx: GuardContext) -> bool:
    """Return whether *guard* holds against *ctx*.

    Args:
        guard: The named predicate attached to an edge.
        ctx: The caller's answers and observations.

    Returns:
        For an observation-backed guard, whether the fact it needs was
        presented. For every other guard, whether the caller left it out
        of :attr:`GuardContext.unmet`.
    """
    fact = OBSERVED_GUARD_FACTS.get(guard)
    if fact is not None:
        return fact in ctx.observations
    return guard not in ctx.unmet


def _deny(
    *,
    entity: LifecycleEntity,
    frm: LifecycleStatus,
    to: LifecycleStatus,
    code: DenialCode,
    guard: TransitionGuard | None,
    message: str,
) -> TransitionDenied:
    """Build a denial, attaching the remediation the code declares."""
    return TransitionDenied(
        entity=entity,
        frm=frm,
        to=to,
        code=code,
        remediation=DENIAL_REMEDIATION[code],
        guard=guard,
        message=message,
    )


def evaluate_edge(
    entity: LifecycleEntity,
    frm: LifecycleStatus,
    to: LifecycleStatus,
    ctx: GuardContext | None = None,
) -> TransitionRow | TransitionDenied:
    """Decide ``frm -> to`` for *entity* without touching a record.

    This is the record-free half of :func:`apply_transition`, so a
    Release -- whose successor construction lives in its own module --
    and a plan-time preview of an edge both go through the same table.

    Args:
        entity: The entity whose machine is consulted.
        frm: Status the record is in.
        to: Status the caller intends to move to.
        ctx: The caller's guard answers. ``None`` is an all-satisfied
            context with nothing observed, so unguarded edges pass and
            observation-backed ones do not.

    Returns:
        The authorising row, or the denial that stops it.
    """
    context = ctx if ctx is not None else GuardContext()
    if is_terminal(entity, frm):
        return _deny(
            entity=entity,
            frm=frm,
            to=to,
            code=DenialCode.TERMINAL_STATE,
            guard=None,
            message=f"{entity.value} status {frm!s} is terminal and has no outgoing transition",
        )
    row = row_for(entity, frm, to)
    if row is None:
        legal = sorted(str(candidate.to) for candidate in rows_from(entity, frm))
        return _deny(
            entity=entity,
            frm=frm,
            to=to,
            code=DenialCode.ILLEGAL_TRANSITION,
            guard=None,
            message=f"{entity.value} {frm!s} -> {to!s} is not a registered edge; legal: {legal}",
        )
    for guard in row.guards:
        if guard_satisfied(guard, context):
            continue
        code = GUARD_DENIALS[guard]
        return _deny(
            entity=entity,
            frm=frm,
            to=to,
            code=code,
            guard=guard,
            message=f"{entity.value} {frm!s} -> {to!s} blocked by guard {guard.value!r}",
        )
    return row


def _entity_of(record: LifecycleRecord) -> LifecycleEntity:
    """Return the entity whose machine governs *record*.

    Args:
        record: The record to classify.

    Returns:
        The governing entity.

    Raises:
        TypeError: *record* is not one of the driven epoch-2 records.
    """
    entity = ENTITY_OF_RECORD.get(type(record))
    if entity is None:
        raise TypeError(
            f"{type(record).__name__} is not a driven epoch-2 record; expected one of "
            f"{sorted(cls.__name__ for cls in ENTITY_OF_RECORD)}"
        )
    return entity


def apply_transition[RecordT: LifecycleRecord](
    record: RecordT,
    *,
    to: LifecycleStatus,
    at: UtcDatetime,
    ctx: GuardContext | None = None,
    updates: Mapping[str, object] | None = None,
) -> TransitionAccepted[RecordT] | TransitionDenied:
    """Return the successor of *record* at *to*, or the denial that stops it.

    Args:
        record: The record to advance. Never mutated.
        to: Target status.
        at: When the transition happened. Supplied by the caller so the
            reducer stays pure and a replay reproduces the same record.
        ctx: The caller's guard answers and observations.
        updates: Field values the target status makes facts, such as the
            head binding a Batch merged at.

    Returns:
        The successor record with its event, or a typed denial.

    Raises:
        TypeError: *record* is not a driven epoch-2 record.
        ValidationError: A supplied update is not a legal value for its
            field. A caller passing a value the model refuses is a defect
            in the caller, not a lifecycle decision, so it is raised
            rather than returned as a denial.
    """
    entity = _entity_of(record)
    supplied = dict(updates) if updates is not None else {}
    if record.origin.kind == "legacy":
        return _deny(
            entity=entity,
            frm=record.status,
            to=to,
            code=DenialCode.LEGACY_ORIGIN_IMMUTABLE,
            guard=None,
            message=f"{record.key} was projected from epoch 1 and refuses native transitions",
        )
    outcome = evaluate_edge(entity, record.status, to, ctx)
    if isinstance(outcome, TransitionDenied):
        return outcome
    missing = [name for name in outcome.required_updates if name not in supplied]
    if missing:
        return _deny(
            entity=entity,
            frm=record.status,
            to=to,
            code=DenialCode.MISSING_TRANSITION_FIELDS,
            guard=None,
            message=f"{entity.value} {record.status!s} -> {to!s} requires {sorted(missing)}",
        )
    successor = _successor(record, to=to, at=at, supplied=supplied)
    event = DomainEvent(
        name=domain_event_name(entity, outcome.verb),
        subject=record.urn,
        occurred_at=at,
        revision=successor.revision,
    )
    logger.info(
        f"apply_transition entity={entity.value} key={record.key!r} "
        f"frm={record.status!s} to={to!s} verb={outcome.verb.value}"
    )
    return TransitionAccepted(record=successor, row=outcome, event=event)


def _successor[RecordT: LifecycleRecord](
    record: RecordT,
    *,
    to: LifecycleStatus,
    at: UtcDatetime,
    supplied: Mapping[str, object],
) -> RecordT:
    """Return a fully re-validated successor of *record* at *to*.

    The successor is rebuilt from a dump rather than copied in place so
    every cross-field rule of the model runs again: a status change is
    exactly what makes those rules say something different.

    Args:
        record: The record to advance.
        to: Target status.
        at: The transition time, which becomes the successor's stamp.
        supplied: The caller's field updates.

    Returns:
        The successor record.

    Raises:
        ValidationError: The successor breaks a model invariant.
    """
    payload: dict[str, object] = record.model_dump(mode="json")
    payload.update(supplied)
    payload["status"] = str(to)
    payload["revision"] = record.revision + 1
    payload["updated_at"] = at
    return cast("RecordT", type(record).model_validate(payload))


__all__ = [
    "ENTITY_OF_RECORD",
    "GuardContext",
    "LifecycleRecord",
    "TransitionAccepted",
    "TransitionDenied",
    "apply_transition",
    "evaluate_edge",
    "guard_satisfied",
]
