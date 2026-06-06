"""Spec entity lifecycle DAG helpers (C01-IMPL W03 placeholder).

C01-IMPL reserves the Spec status vocabulary + canonical transition map
so C02 daemon writes and C03 spec CLI verbs share one source of truth.
The full PhaseSpec / IterSpec / WaveSpec Pydantic schemas + filesystem
storage layout land in C03-IMPL; this module exposes the typed DAG
helpers callers need before then.

Per c01-foundations §5.4.15 the lifecycle is strictly forward:

    DRAFT -> READY -> IMPLEMENTED -> ARCHIVED

No backward edges, no idempotent self-loops. ARCHIVED is terminal —
the daemon ``git rm``s the spec file and the operator restores from
``git log`` rather than transitioning the entity back.

Library-private. No CLI surface in v0.3; C05 spec CLI verbs land in
C05-IMPL and dispatch through C02 daemon RPC.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from eawf.kernel.state.enums import (
    IterStatus,
    PhaseStatus,
    SpecStatus,
    WaveStatus,
)
from eawf.workflow.lifecycle._errors import LifecycleError

logger = logging.getLogger(__name__)


SPEC_TRANSITIONS: dict[SpecStatus, frozenset[SpecStatus]] = {
    SpecStatus.DRAFT: frozenset({SpecStatus.READY}),
    SpecStatus.READY: frozenset({SpecStatus.IMPLEMENTED}),
    SpecStatus.IMPLEMENTED: frozenset({SpecStatus.ARCHIVED}),
    SpecStatus.ARCHIVED: frozenset(),
}


class GuardName(StrEnum):
    """Named predicates a guarded transition-table edge can attach.

    A guarded transition table edge carries a ``(target_status, GuardName)``
    pair: the status move is structurally legal only when the named guard's
    predicate is satisfied against the caller-supplied :class:`GuardContext`.
    :attr:`NONE` is the always-allowed sentinel for edges whose only
    precondition is the source status (e.g. ``claimed -> in_progress``).

    Values:
        NONE: No predicate; the edge is allowed whenever the source status
            matches (the bare status-machine edge).
        DEPS_CLOSED: Every dependency wave is :data:`WaveStatus.CLOSED`.
        SIBLING_ORDERED: No lower-numbered ``W##`` sibling is still PENDING
            with its own deps satisfied -- the monotonic claim-order gate.
            Satisfied either when ordering holds or when
            :attr:`OUT_OF_ORDER_OVERRIDE` is set on the context.
        OUT_OF_ORDER_OVERRIDE: The operator-blessed ``--out-of-order`` escape
            hatch that satisfies :attr:`SIBLING_ORDERED` for parallel-worktree
            dispatch of same-frontier siblings.
        NOT_PAUSED: Dispatch is not paused (``state.dispatch_paused`` is
            ``False``); an unconditional cooperative-stop gate.
    """

    NONE = "none"
    DEPS_CLOSED = "deps_closed"
    SIBLING_ORDERED = "sibling_ordered"
    OUT_OF_ORDER_OVERRIDE = "out_of_order_override"
    NOT_PAUSED = "not_paused"


@dataclass(frozen=True, slots=True)
class GuardContext:
    """Booleans + pre-formatted failure messages the named guards evaluate.

    The caller computes the predicate inputs (it owns the wave id, the sorted
    unmet-dep list, etc.) and passes them here so :func:`validate_transition`
    can stay a thin status-machine evaluator. ``messages`` carries the exact
    :class:`LifecycleError` text to raise when a guard fails, keyed by the
    guard that owns it -- this is what preserves the existing public error
    contract (the dep / sibling / pause message substrings tests assert).

    Attributes:
        deps_closed: Backs :attr:`GuardName.DEPS_CLOSED`.
        sibling_ordered: Backs :attr:`GuardName.SIBLING_ORDERED` (OR'd with
            :attr:`out_of_order`).
        out_of_order: Backs :attr:`GuardName.OUT_OF_ORDER_OVERRIDE`; when
            ``True`` it satisfies the sibling-ordering guard.
        not_paused: Backs :attr:`GuardName.NOT_PAUSED`.
        messages: Per-guard failure text raised on an unmet predicate.
    """

    deps_closed: bool = True
    sibling_ordered: bool = True
    out_of_order: bool = False
    not_paused: bool = True
    messages: dict[GuardName, str] | None = None


#: Evaluation order for guards on a single edge so the surfaced failure
#: message is deterministic and matches the legacy ``claim_wave`` order
#: (deps, then sibling-ordering, then pause).
_GUARD_ORDER: dict[GuardName, int] = {
    GuardName.NONE: 0,
    GuardName.DEPS_CLOSED: 1,
    GuardName.SIBLING_ORDERED: 2,
    GuardName.OUT_OF_ORDER_OVERRIDE: 3,
    GuardName.NOT_PAUSED: 4,
}


#: Wave status-machine, guarded. Each edge is a ``(target, GuardName)`` pair;
#: an edge with multiple guards appears once per guard. Terminal statuses
#: (CLOSED, FAILED, ABANDONED) carry no out-edges -- they are reached only
#: through these forward edges and the archive cascade, never left here.
#: Derived from the inline guards in :mod:`eawf.workflow.lifecycle.wave`:
#: ``claim_wave`` (pending -> claimed, guarded), ``start_wave``
#: (claimed -> in_progress), ``close_wave`` (claimed/in_progress -> closed),
#: ``fail_wave`` (pending/claimed/in_progress -> failed), and ``release_wave``
#: (claimed/in_progress -> pending).
WAVE_TRANSITIONS: dict[WaveStatus, frozenset[tuple[WaveStatus, GuardName]]] = {
    WaveStatus.PENDING: frozenset(
        {
            (WaveStatus.CLAIMED, GuardName.DEPS_CLOSED),
            (WaveStatus.CLAIMED, GuardName.SIBLING_ORDERED),
            (WaveStatus.CLAIMED, GuardName.NOT_PAUSED),
            (WaveStatus.FAILED, GuardName.NONE),
        }
    ),
    WaveStatus.CLAIMED: frozenset(
        {
            (WaveStatus.IN_PROGRESS, GuardName.NONE),
            (WaveStatus.CLOSED, GuardName.NONE),
            (WaveStatus.FAILED, GuardName.NONE),
            (WaveStatus.PENDING, GuardName.NONE),
        }
    ),
    WaveStatus.IN_PROGRESS: frozenset(
        {
            (WaveStatus.CLOSED, GuardName.NONE),
            (WaveStatus.FAILED, GuardName.NONE),
            (WaveStatus.PENDING, GuardName.NONE),
        }
    ),
    WaveStatus.CLOSED: frozenset(),
    WaveStatus.FAILED: frozenset(),
    WaveStatus.ABANDONED: frozenset(),
}


#: Phase status-machine. Edges carry no named guard (the rich domain gates --
#: phase-dep-closed + has-waves on activate, the close-readiness projection on
#: close -- stay inline because they are not part of the named-guard set). The
#: table is the source of truth for the legal status moves themselves.
#: Derived from :mod:`eawf.workflow.lifecycle.phase`: ``activate_phase``
#: (planned -> active), ``close_phase`` (planned/active -> closed; the
#: phase-status close-readiness criterion accepts both PLANNED and ACTIVE
#: sources), ``archive_phase`` (planned -> archived), ``reopen_phase``
#: (closed -> active). Note the ACTIVE target is shared by ``activate_phase``
#: (planned source) and ``reopen_phase`` (closed source), so those two keep
#: their inline source-specific guards; the table is consulted by
#: ``archive_phase`` whose PLANNED -> ARCHIVED target is unambiguous.
PHASE_TRANSITIONS: dict[PhaseStatus, frozenset[tuple[PhaseStatus, GuardName]]] = {
    PhaseStatus.PLANNED: frozenset(
        {
            (PhaseStatus.ACTIVE, GuardName.NONE),
            (PhaseStatus.CLOSED, GuardName.NONE),
            (PhaseStatus.ARCHIVED, GuardName.NONE),
        }
    ),
    PhaseStatus.ACTIVE: frozenset({(PhaseStatus.CLOSED, GuardName.NONE)}),
    PhaseStatus.CLOSED: frozenset({(PhaseStatus.ACTIVE, GuardName.NONE)}),
    PhaseStatus.ARCHIVED: frozenset(),
}


#: Iter status-machine. Like the phase table the edges carry no named guard;
#: the open-child-wave gate on close stays inline. Derived from
#: :mod:`eawf.workflow.lifecycle.iter_`: ``activate_iter`` (planned -> active),
#: ``close_iter`` (planned/active -> closed), plus the archive cascade
#: (planned/active -> abandoned) driven from ``archive_phase``.
ITER_TRANSITIONS: dict[IterStatus, frozenset[tuple[IterStatus, GuardName]]] = {
    IterStatus.PLANNED: frozenset(
        {
            (IterStatus.ACTIVE, GuardName.NONE),
            (IterStatus.CLOSED, GuardName.NONE),
            (IterStatus.ABANDONED, GuardName.NONE),
        }
    ),
    IterStatus.ACTIVE: frozenset(
        {
            (IterStatus.CLOSED, GuardName.NONE),
            (IterStatus.ABANDONED, GuardName.NONE),
        }
    ),
    IterStatus.CLOSED: frozenset(),
    IterStatus.ABANDONED: frozenset(),
}


def _guard_satisfied(guard: GuardName, ctx: GuardContext) -> bool:
    """Return whether *guard* holds against *ctx*.

    :attr:`GuardName.NONE` is always satisfied. The sibling-ordering guard
    folds the out-of-order override in: it holds when ordering holds OR the
    override is set. The override member itself is satisfied by its own flag.
    """
    if guard is GuardName.NONE:
        return True
    if guard is GuardName.DEPS_CLOSED:
        return ctx.deps_closed
    if guard is GuardName.SIBLING_ORDERED:
        return ctx.sibling_ordered or ctx.out_of_order
    if guard is GuardName.OUT_OF_ORDER_OVERRIDE:
        return ctx.out_of_order
    return ctx.not_paused


def validate_transition[StatusT: StrEnum](
    table: dict[StatusT, frozenset[tuple[StatusT, GuardName]]],
    frm: StatusT,
    to: StatusT,
    guard_ctx: GuardContext | None = None,
    *,
    illegal_message: str | None = None,
) -> None:
    """Guard a single status move against a guarded transition *table*.

    The shared status-machine evaluator for the wave / phase / iter tables.
    Rejects when ``frm -> to`` has no edge in the table at all, then evaluates
    every named guard attached to the edge against *guard_ctx*. A guard whose
    predicate is unmet raises with the caller-supplied message for that guard
    (so the existing dep / sibling / pause error contract is preserved); a
    guard with no supplied message falls back to a generic phrasing.

    Args:
        table: A guarded transition table (e.g. :data:`WAVE_TRANSITIONS`).
        frm: The source status the entity is in.
        to: The target status the caller intends to move to.
        guard_ctx: Predicate inputs + per-guard failure messages. ``None``
            (the default) is treated as an all-satisfied context, which is
            correct for the unguarded phase / iter edges.
        illegal_message: The :class:`LifecycleError` text to raise when
            ``frm -> to`` is not in *table*. Callers pass the legacy
            status-guard wording here so the existing public message contract
            is preserved; ``None`` falls back to a generic phrasing.

    Raises:
        LifecycleError: when ``frm -> to`` is absent from *table*, or when a
            named guard on the edge is unmet.
    """
    ctx = guard_ctx if guard_ctx is not None else GuardContext()
    guards = sorted(
        (guard for target, guard in table.get(frm, frozenset()) if target == to),
        key=_GUARD_ORDER.__getitem__,
    )
    if not guards:
        raise LifecycleError(
            illegal_message
            if illegal_message is not None
            else f"illegal transition {frm.value!r} -> {to.value!r} not in table"
        )
    messages = ctx.messages or {}
    for guard in guards:
        if not _guard_satisfied(guard, ctx):
            message = messages.get(guard)
            if message is None:
                message = (
                    f"transition {frm.value!r} -> {to.value!r} blocked by guard {guard.value!r}"
                )
            raise LifecycleError(message)


class SpecTransitionError(Exception):
    """Raised when a Spec status transition violates the canonical DAG.

    C03 spec CLI verbs catch this and emit ``VALIDATION_FAILED`` exit
    codes; C02 daemon writes treat it as an internal-state-machine
    invariant violation.
    """


def validate_spec_transition(current: SpecStatus, target: SpecStatus) -> None:
    """Guard: raise :class:`SpecTransitionError` when ``current -> target`` is illegal.

    Args:
        current: Spec status before the transition.
        target: Spec status the caller intends to move to.

    Raises:
        SpecTransitionError: When the transition is not in
            :data:`SPEC_TRANSITIONS`.
    """
    allowed = SPEC_TRANSITIONS[current]
    if target not in allowed:
        raise SpecTransitionError(
            f"spec transition {current.value!r} -> {target.value!r} not allowed; "
            f"valid: {sorted(s.value for s in allowed)}"
        )
    logger.debug(f"validate_spec_transition current={current.value} target={target.value}")


def next_spec_statuses(current: SpecStatus) -> frozenset[SpecStatus]:
    """Return the set of statuses reachable from ``current`` in one transition."""
    return SPEC_TRANSITIONS[current]


def is_terminal_spec_status(status: SpecStatus) -> bool:
    """Return ``True`` when ``status`` has no outgoing transitions."""
    return not SPEC_TRANSITIONS[status]
