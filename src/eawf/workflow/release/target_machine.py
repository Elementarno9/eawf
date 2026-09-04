"""The per-target publication status machine and its named denials.

:mod:`eawf.workflow.release.lifecycle` walks the whole release; this
module walks one *leg* of it. The two are separate machines because they
answer different questions: the release machine asks "may this version
move on", the target machine asks "what is true about this one
registry". Collapsing them would make a single unlucky adapter dictate
the release status, which is exactly the coupling recovery exists to
break.

Three rules shape the table and are worth stating before reading it:

* **Only a deadline admits ``unknown``.** ``in_flight -> unknown`` is
  guarded on the attempt's own ``deadline_at``, which is
  ``started_at + timeout_seconds`` of the target's configuration. An
  operator cannot declare a leg unknown early to unlock recovery.
* **A retry is a re-queue, and it is budgeted.** The moves back into
  ``queued`` from ``reported_failure`` and ``unknown`` are the only
  retries, and both are guarded on the target's ``retry_limit``, so the
  number of external effect attempts against one registry is bounded by
  configuration rather than by operator patience.
* **Only observation reaches ``observed_success``.** An adapter's own
  ``reported_success`` never advances by itself; the move is guarded on
  a read-back that matched, and the mismatch edge is unguarded because
  contradicting evidence is always admissible.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from eawf.kernel.spec.publication import (
    SETTLED_TARGET_STATUSES,
    OperationAttempt,
    PublicationOperation,
    assert_append_only,
    attempt_count,
    latest_attempt,
)
from eawf.kernel.spec.release import ReleaseTargetStatus
from eawf.kernel.spec.release_config import ReleaseTargetConfig

logger = logging.getLogger(__name__)


class TargetGuardName(StrEnum):
    """Named predicates a guarded per-target edge can attach.

    Values:
        NONE: No predicate; the source status alone permits the move.
        DEADLINE_ELAPSED: The attempt's ``deadline_at`` has passed.
        RETRY_BUDGET: The target has attempts left under its
            ``retry_limit``.
        OBSERVATION_MATCHED: An independent read-back matched the frozen
            digests.
    """

    NONE = "none"
    DEADLINE_ELAPSED = "deadline_elapsed"
    RETRY_BUDGET = "retry_budget"
    OBSERVATION_MATCHED = "observation_matched"


class TargetDenialCode(StrEnum):
    """Named error a denied per-target transition raises.

    Values:
        ILLEGAL_TARGET_TRANSITION: The edge is absent from the table.
        TARGET_DEADLINE_NOT_ELAPSED: A leg was declared unknown before
            its own deadline.
        RETRY_LIMIT_EXHAUSTED: A re-queue past the target's
            ``retry_limit``.
        OBSERVATION_MISMATCHED: A read-back that did not match was
            claimed as an observed success.
    """

    ILLEGAL_TARGET_TRANSITION = "illegal_target_transition"
    TARGET_DEADLINE_NOT_ELAPSED = "target_deadline_not_elapsed"
    RETRY_LIMIT_EXHAUSTED = "retry_limit_exhausted"
    OBSERVATION_MISMATCHED = "observation_mismatched"


class TargetTransitionError(Exception):
    """A per-target status move was denied.

    Attributes:
        code: The named denial for this edge, or
            :attr:`TargetDenialCode.ILLEGAL_TARGET_TRANSITION` when the
            edge does not exist at all.
        target_id: The leg whose move was denied.
        frm: Status the leg is in.
        to: Status the caller intended to move it to.
    """

    def __init__(
        self,
        code: TargetDenialCode,
        target_id: str,
        frm: ReleaseTargetStatus,
        to: ReleaseTargetStatus,
        message: str,
    ) -> None:
        """Store the typed denial *code* alongside the edge it denied."""
        super().__init__(message)
        self.code = code
        self.target_id = target_id
        self.frm = frm
        self.to = to


@dataclass(frozen=True, slots=True)
class TargetGuardContext:
    """Predicate inputs the named per-target guards evaluate.

    Attributes:
        deadline_elapsed: Backs :attr:`TargetGuardName.DEADLINE_ELAPSED`.
        retry_budget_remaining: Backs :attr:`TargetGuardName.RETRY_BUDGET`.
        observation_matched: Backs
            :attr:`TargetGuardName.OBSERVATION_MATCHED`.
    """

    deadline_elapsed: bool = True
    retry_budget_remaining: bool = True
    observation_matched: bool = True


#: The eight-state per-target machine. Each edge is a
#: ``(target, TargetGuardName)`` pair; the two observed states are
#: terminal because a leg that has been independently read back has
#: nothing left to say -- a contradiction is handled by release-level
#: recovery, not by re-opening the leg.
TARGET_TRANSITIONS: Final[
    dict[ReleaseTargetStatus, frozenset[tuple[ReleaseTargetStatus, TargetGuardName]]]
] = {
    ReleaseTargetStatus.NOT_STARTED: frozenset(
        {(ReleaseTargetStatus.QUEUED, TargetGuardName.NONE)}
    ),
    ReleaseTargetStatus.QUEUED: frozenset({(ReleaseTargetStatus.IN_FLIGHT, TargetGuardName.NONE)}),
    ReleaseTargetStatus.IN_FLIGHT: frozenset(
        {
            (ReleaseTargetStatus.REPORTED_SUCCESS, TargetGuardName.NONE),
            (ReleaseTargetStatus.REPORTED_FAILURE, TargetGuardName.NONE),
            (ReleaseTargetStatus.UNKNOWN, TargetGuardName.DEADLINE_ELAPSED),
        }
    ),
    ReleaseTargetStatus.REPORTED_SUCCESS: frozenset(
        {
            (ReleaseTargetStatus.OBSERVED_SUCCESS, TargetGuardName.OBSERVATION_MATCHED),
            (ReleaseTargetStatus.OBSERVED_MISMATCH, TargetGuardName.NONE),
        }
    ),
    ReleaseTargetStatus.REPORTED_FAILURE: frozenset(
        {(ReleaseTargetStatus.QUEUED, TargetGuardName.RETRY_BUDGET)}
    ),
    ReleaseTargetStatus.UNKNOWN: frozenset(
        {
            (ReleaseTargetStatus.QUEUED, TargetGuardName.RETRY_BUDGET),
            (ReleaseTargetStatus.OBSERVED_SUCCESS, TargetGuardName.OBSERVATION_MATCHED),
            (ReleaseTargetStatus.OBSERVED_MISMATCH, TargetGuardName.NONE),
        }
    ),
    ReleaseTargetStatus.OBSERVED_SUCCESS: frozenset(),
    ReleaseTargetStatus.OBSERVED_MISMATCH: frozenset(),
}


#: The named error each guarded per-target edge raises when its guard is
#: unmet. Unguarded edges do not appear, because they cannot be denied.
TARGET_DENIALS: Final[
    Mapping[tuple[ReleaseTargetStatus, ReleaseTargetStatus], TargetDenialCode]
] = {
    (ReleaseTargetStatus.IN_FLIGHT, ReleaseTargetStatus.UNKNOWN): (
        TargetDenialCode.TARGET_DEADLINE_NOT_ELAPSED
    ),
    (ReleaseTargetStatus.REPORTED_FAILURE, ReleaseTargetStatus.QUEUED): (
        TargetDenialCode.RETRY_LIMIT_EXHAUSTED
    ),
    (ReleaseTargetStatus.UNKNOWN, ReleaseTargetStatus.QUEUED): (
        TargetDenialCode.RETRY_LIMIT_EXHAUSTED
    ),
    (ReleaseTargetStatus.REPORTED_SUCCESS, ReleaseTargetStatus.OBSERVED_SUCCESS): (
        TargetDenialCode.OBSERVATION_MISMATCHED
    ),
    (ReleaseTargetStatus.UNKNOWN, ReleaseTargetStatus.OBSERVED_SUCCESS): (
        TargetDenialCode.OBSERVATION_MISMATCHED
    ),
}


#: Per-target statuses with no out-edges.
TERMINAL_TARGET_STATUSES: Final[frozenset[ReleaseTargetStatus]] = frozenset(
    status for status, edges in TARGET_TRANSITIONS.items() if not edges
)


def _guard_satisfied(guard: TargetGuardName, ctx: TargetGuardContext) -> bool:
    """Return whether *guard* holds against *ctx*.

    Args:
        guard: The named predicate attached to the edge.
        ctx: Predicate inputs derived from the ledger and the target
            configuration.

    Returns:
        ``True`` when the predicate holds.
    """
    return {
        TargetGuardName.NONE: True,
        TargetGuardName.DEADLINE_ELAPSED: ctx.deadline_elapsed,
        TargetGuardName.RETRY_BUDGET: ctx.retry_budget_remaining,
        TargetGuardName.OBSERVATION_MATCHED: ctx.observation_matched,
    }[guard]


def next_target_statuses(status: ReleaseTargetStatus) -> frozenset[ReleaseTargetStatus]:
    """Return the statuses reachable from *status* in one transition.

    Args:
        status: Source per-target status.

    Returns:
        The set of legal targets, ignoring guards. Empty for a terminal
        status.
    """
    return frozenset(target for target, _guard in TARGET_TRANSITIONS[status])


def validate_target_transition(
    target_id: str,
    frm: ReleaseTargetStatus,
    to: ReleaseTargetStatus,
    ctx: TargetGuardContext | None = None,
) -> None:
    """Guard one per-target status move against :data:`TARGET_TRANSITIONS`.

    Args:
        target_id: The leg being moved, for the error message.
        frm: Status the leg is in.
        to: Status the caller intends to move it to.
        ctx: Predicate inputs. ``None`` is an all-satisfied context.

    Raises:
        TargetTransitionError: When the edge is absent from the table
            (code :attr:`TargetDenialCode.ILLEGAL_TARGET_TRANSITION`) or
            a guard on it is unmet (the code :data:`TARGET_DENIALS`
            declares for that edge).
    """
    context = ctx if ctx is not None else TargetGuardContext()
    guards = sorted(
        (guard for target, guard in TARGET_TRANSITIONS[frm] if target == to),
        key=lambda guard: guard.value,
    )
    if not guards:
        raise TargetTransitionError(
            TargetDenialCode.ILLEGAL_TARGET_TRANSITION,
            target_id,
            frm,
            to,
            f"illegal target transition {frm.value!r} -> {to.value!r} on target "
            f"{target_id!r}; legal targets: "
            f"{sorted(s.value for s in next_target_statuses(frm))}",
        )
    for guard in guards:
        if _guard_satisfied(guard, context):
            continue
        code = TARGET_DENIALS[(frm, to)]
        raise TargetTransitionError(
            code,
            target_id,
            frm,
            to,
            f"{code.value}: target {target_id!r} transition {frm.value!r} -> "
            f"{to.value!r} blocked by guard {guard.value!r}",
        )


def current_target_status(
    operation: PublicationOperation,
    target_id: str,
) -> ReleaseTargetStatus:
    """Return where *target_id* currently stands in the machine.

    Args:
        operation: Operation whose ledger is read.
        target_id: Publication target to place.

    Returns:
        The latest attempt's status, or
        :attr:`~eawf.kernel.spec.release.ReleaseTargetStatus.NOT_STARTED`
        when the operation has never attempted that target.
    """
    row = latest_attempt(operation, target_id)
    return ReleaseTargetStatus.NOT_STARTED if row is None else row.status


def retry_budget_remaining(
    operation: PublicationOperation,
    target: ReleaseTargetConfig,
) -> bool:
    """Return whether *target* may be queued again under its retry limit.

    Attempt 1 is the initial try, so a target configured with
    ``retry_limit`` admits ``1 + retry_limit`` attempts in total. A
    target at ``retry_limit=0`` therefore gets exactly one attempt and
    no re-queue, which is the point of allowing zero.

    Args:
        operation: Operation whose ledger is counted.
        target: The target's configuration, carrying ``retry_limit``.

    Returns:
        ``True`` when another attempt may be opened.
    """
    return attempt_count(operation, target.target_id) <= target.retry_limit


def deadline_elapsed(
    operation: PublicationOperation,
    target_id: str,
    now: datetime,
) -> bool:
    """Return whether the latest attempt of *target_id* is past its deadline.

    Args:
        operation: Operation whose ledger is read.
        target_id: Publication target to check.
        now: Timezone-aware UTC instant to compare against.

    Returns:
        ``True`` when *now* is at or after the attempt's ``deadline_at``.
        The instant of the deadline itself counts as elapsed: the budget
        is the interval before it, not up to and including it.

    Raises:
        KeyError: When the operation has never attempted that target.
        ValueError: When *now* is naive.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    row = latest_attempt(operation, target_id)
    if row is None:
        raise KeyError(f"operation {operation.operation_id} has no attempt for {target_id!r}")
    return now >= row.deadline_at


def open_target_attempt(
    operation: PublicationOperation,
    *,
    target: ReleaseTargetConfig,
    now: datetime,
    request_digest: str,
) -> PublicationOperation:
    """Return the successor that queues a new attempt for *target*.

    This is the only way a leg enters ``queued``: either its first
    attempt (from ``not_started``, unguarded) or a retry after a
    ``reported_failure`` or ``unknown`` result, which is guarded on the
    target's ``retry_limit``.

    Args:
        operation: Operation to extend.
        target: The target's configuration; supplies ``timeout_seconds``
            (the new attempt's deadline) and ``retry_limit``.
        now: Timezone-aware UTC dispatch instant.
        request_digest: Digest of the exact request payload.

    Returns:
        The successor operation carrying one more attempt row.

    Raises:
        TargetTransitionError: When the leg cannot be queued from where
            it stands, or its retry budget is spent
            (:attr:`TargetDenialCode.RETRY_LIMIT_EXHAUSTED`).
        ValueError: When *now* is naive.
        ValidationError: When the successor violates a record invariant.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    frm = current_target_status(operation, target.target_id)
    validate_target_transition(
        target.target_id,
        frm,
        ReleaseTargetStatus.QUEUED,
        TargetGuardContext(retry_budget_remaining=retry_budget_remaining(operation, target)),
    )
    row = OperationAttempt(
        target_id=target.target_id,
        attempt=attempt_count(operation, target.target_id) + 1,
        status=ReleaseTargetStatus.QUEUED,
        request_digest=request_digest,
        started_at=now,
        deadline_at=now + timedelta(seconds=target.timeout_seconds),
    )
    successor = operation.model_copy(
        update={
            "publication_receipts": (*operation.publication_receipts, row),
            "revision": operation.revision + 1,
        }
    )
    validated = PublicationOperation.model_validate(successor.model_dump(mode="json"))
    assert_append_only(operation, validated)
    logger.info(
        f"open_target_attempt operation_id={operation.operation_id} "
        f"target={target.target_id!r} attempt={row.attempt} frm={frm.value!r}"
    )
    return validated


def advance_target_attempt(
    operation: PublicationOperation,
    *,
    target: ReleaseTargetConfig,
    to: ReleaseTargetStatus,
    now: datetime,
    effect_receipt_ref: str | None = None,
    observation_receipt_ref: str | None = None,
    observation_matched: bool = True,
) -> PublicationOperation:
    """Return the successor that moves *target*'s latest attempt to *to*.

    Every move that stays inside one attempt goes through here: the
    dispatch, the adapter's report, the timeout, and the read-back. The
    guard context is derived from the ledger and the configuration
    rather than accepted from the caller, so an operator cannot declare
    a leg unknown before its deadline by passing a flag.

    Args:
        operation: Operation to advance.
        target: The target's configuration.
        to: Status the latest attempt moves to.
        now: Timezone-aware UTC instant; stamps ``settled_at`` when *to*
            is a settled status.
        effect_receipt_ref: The adapter's receipt, carried onto the row.
            Defaults to whatever the row already had.
        observation_receipt_ref: The read-back receipt, required by the
            ``observed_*`` statuses.
        observation_matched: Whether the read-back matched the frozen
            digests; backs the guard on ``-> observed_success``.

    Returns:
        The successor operation with that one row advanced.

    Raises:
        TargetTransitionError: When the edge is illegal or its guard is
            unmet.
        KeyError: When the operation has never attempted that target.
        ValueError: When *now* is naive.
        ValidationError: When the advanced row violates a record
            invariant, e.g. a reported status with no effect receipt.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    rows = list(operation.publication_receipts)
    index = _latest_index(rows, target.target_id, operation)
    row = rows[index]
    validate_target_transition(
        target.target_id,
        row.status,
        to,
        TargetGuardContext(
            deadline_elapsed=now >= row.deadline_at,
            retry_budget_remaining=retry_budget_remaining(operation, target),
            observation_matched=observation_matched,
        ),
    )
    rows[index] = row.model_copy(
        update={
            "status": to,
            "effect_receipt_ref": effect_receipt_ref or row.effect_receipt_ref,
            "observation_receipt_ref": observation_receipt_ref,
            "settled_at": now if to in SETTLED_TARGET_STATUSES else None,
        }
    )
    successor = operation.model_copy(
        update={"publication_receipts": tuple(rows), "revision": operation.revision + 1}
    )
    validated = PublicationOperation.model_validate(successor.model_dump(mode="json"))
    assert_append_only(operation, validated)
    logger.info(
        f"advance_target_attempt operation_id={operation.operation_id} "
        f"target={target.target_id!r} attempt={row.attempt} "
        f"frm={row.status.value!r} to={to.value!r}"
    )
    return validated


def _latest_index(
    rows: list[OperationAttempt],
    target_id: str,
    operation: PublicationOperation,
) -> int:
    """Return the index of the highest-numbered row for *target_id*.

    Args:
        rows: The operation's receipt rows, in persisted order.
        target_id: Publication target to locate.
        operation: The operation, for the error message.

    Returns:
        The index into *rows*.

    Raises:
        KeyError: When no row addresses that target.
    """
    candidates = [index for index, row in enumerate(rows) if row.target_id == target_id]
    if not candidates:
        raise KeyError(
            f"operation {operation.operation_id} has no attempt for target {target_id!r}; "
            f"attempted targets: {list(operation.target_ids)}"
        )
    return max(candidates, key=lambda index: rows[index].attempt)


__all__ = [
    "TARGET_DENIALS",
    "TARGET_TRANSITIONS",
    "TERMINAL_TARGET_STATUSES",
    "TargetDenialCode",
    "TargetGuardContext",
    "TargetGuardName",
    "TargetTransitionError",
    "advance_target_attempt",
    "current_target_status",
    "deadline_elapsed",
    "next_target_statuses",
    "open_target_attempt",
    "retry_budget_remaining",
    "validate_target_transition",
]
