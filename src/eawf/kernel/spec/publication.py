"""Typed publication records: :class:`PublicationOperation` and its attempts.

A *publication operation* is one external-effect episode of a
:class:`~eawf.kernel.spec.release.Release`: the release says *what* is
being published, the operation says *what was actually attempted, to
which target, how many times, and what came back*. The two are separate
records because they have different lifetimes -- a release outlives
every operation opened against it, and a burned release must keep the
attempt history that burned it.

Three properties carry the weight and each is enforced at the model
boundary rather than by a caller:

* an attempt row is *evidence-complete for its own status* -- a row
  claiming :attr:`~eawf.kernel.spec.release.ReleaseTargetStatus.REPORTED_SUCCESS`
  carries the effect receipt that says so, and only an ``observed_*``
  row may carry an observation receipt, so an adapter's own report can
  never be dressed up as an independent read-back;
* ``publication_receipts`` is keyed by ``(target_id, attempt)``, unique,
  and contiguous from attempt 1 per target, so ``attempt_count`` is the
  highest persisted attempt by construction rather than by a separate
  counter that could drift;
* the sequence is **append-only** across revisions
  (:func:`assert_append_only`): a successor may add rows and advance the
  status of a row it already had, but a row that was persisted can never
  vanish or be re-keyed. Losing an attempt row is how a
  double-publication becomes invisible.

Records are frozen. A status advance produces a NEW record with an
incremented :attr:`PublicationOperation.revision`, exactly as
:class:`~eawf.kernel.spec.release.Release` does; the guarded successor
helpers live in :mod:`eawf.workflow.release.publication`.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import (
    OBSERVED_TARGET_STATUSES,
    ReferenceStr,
    ReleaseKeyStr,
    ReleaseTargetStatus,
    Sha256DigestStr,
    TargetIdStr,
)
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)

#: Caller-supplied idempotency key. At least eight characters because a
#: short key is a collision hazard: two different publish requests that
#: collide on the key would have the second silently replayed as the
#: first, which is the one failure mode idempotency exists to prevent.
IdempotencyKeyStr = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")]


class PublicationOperationKind(StrEnum):
    """Which operator verb opened this operation.

    The kind is not cosmetic: it is what distinguishes a first publish
    from a bounded retry of one leg and from a pure read-back, and each
    admits a different attempt shape (a reconcile never opens a new
    attempt, it settles an existing one).

    Values:
        PUBLISH: The first external-effect episode of a release.
        RETRY_TARGET: A bounded re-attempt of one unsettled target.
        RECONCILE: An independent read-back of an already-attempted
            target; opens no new attempt.
    """

    PUBLISH = "publish"
    RETRY_TARGET = "retry_target"
    RECONCILE = "reconcile"


class PublicationOperationStatus(StrEnum):
    """Whether the operation still has work outstanding.

    Values:
        OPEN: At least one leg has no settled result yet.
        SETTLED: Every leg carries a settled result.
        ABANDONED: The operation was superseded by recovery; its rows
            stay for the audit trail but nothing further is attempted.
    """

    OPEN = "open"
    SETTLED = "settled"
    ABANDONED = "abandoned"


#: Target statuses that mean the leg has a final result for this
#: attempt. A settled row cannot change again -- a further move needs a
#: new attempt, which is what makes the retry budget meaningful.
SETTLED_TARGET_STATUSES: Final[frozenset[ReleaseTargetStatus]] = frozenset(
    {
        ReleaseTargetStatus.REPORTED_SUCCESS,
        ReleaseTargetStatus.REPORTED_FAILURE,
        ReleaseTargetStatus.UNKNOWN,
        ReleaseTargetStatus.OBSERVED_SUCCESS,
        ReleaseTargetStatus.OBSERVED_MISMATCH,
    }
)

#: Target statuses that assert the adapter came back with a final word,
#: which it can only have done by producing an effect receipt. UNKNOWN
#: is deliberately absent: not hearing back is exactly the case where no
#: receipt exists.
EFFECT_REPORTED_STATUSES: Final[frozenset[ReleaseTargetStatus]] = frozenset(
    {
        ReleaseTargetStatus.REPORTED_SUCCESS,
        ReleaseTargetStatus.REPORTED_FAILURE,
        ReleaseTargetStatus.OBSERVED_SUCCESS,
        ReleaseTargetStatus.OBSERVED_MISMATCH,
    }
)


class OperationAttempt(_StrictModel):
    """One numbered attempt at publishing one target.

    Attributes:
        schema_version: Record schema tag.
        target_id: Publication target this leg addresses.
        attempt: 1-based attempt number; attempt 1 is the initial try
            and every later number is a retry against the target's
            ``retry_limit``.
        status: Per-target publication state of this attempt.
        request_digest: Digest of the exact request payload dispatched.
            A retry that changes the payload is a different publication,
            not a retry, and this digest is what proves the difference.
        effect_receipt_ref: The adapter's own receipt. Required once the
            adapter reported a final word.
        observation_receipt_ref: The independent read-back receipt.
            Required by, and permitted only on, an ``observed_*`` status.
        started_at: When the attempt was dispatched.
        deadline_at: ``started_at`` plus the target's ``timeout_seconds``;
            crossing it is what admits the ``unknown`` edge.
        settled_at: When the attempt reached a settled status. Present
            exactly when the status is settled.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["operation-attempt/v1"] = "operation-attempt/v1"
    target_id: TargetIdStr
    attempt: Annotated[int, Field(ge=1)]
    status: ReleaseTargetStatus
    request_digest: Sha256DigestStr
    effect_receipt_ref: ReferenceStr | None = None
    observation_receipt_ref: ReferenceStr | None = None
    started_at: UtcDatetime
    deadline_at: UtcDatetime
    settled_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _evidence_matches_status(self) -> OperationAttempt:
        """Reject a row whose receipts contradict its own status.

        Raises:
            ValueError: When the row claims ``not_started`` (a persisted
                attempt was at least queued), when a reported or
                observed status carries no effect receipt, when an
                observation receipt appears on an unobserved row or is
                missing from an observed one, or when the settled stamp
                disagrees with the status.
        """
        if self.status is ReleaseTargetStatus.NOT_STARTED:
            raise ValueError(
                f"attempt {self.attempt} of target {self.target_id!r} cannot be "
                f"'not_started'; a persisted attempt was at least queued"
            )
        if self.status in EFFECT_REPORTED_STATUSES and self.effect_receipt_ref is None:
            raise ValueError(
                f"status {self.status.value!r} requires effect_receipt_ref on "
                f"attempt {self.attempt} of target {self.target_id!r}"
            )
        observed = self.status in OBSERVED_TARGET_STATUSES
        if observed and self.observation_receipt_ref is None:
            raise ValueError(
                f"status {self.status.value!r} requires observation_receipt_ref on "
                f"attempt {self.attempt} of target {self.target_id!r}"
            )
        if not observed and self.observation_receipt_ref is not None:
            raise ValueError(
                f"status {self.status.value!r} must not carry observation_receipt_ref "
                f"on attempt {self.attempt} of target {self.target_id!r}; only an "
                f"independent read-back may"
            )
        return self

    @model_validator(mode="after")
    def _clock_is_coherent(self) -> OperationAttempt:
        """Reject a deadline or settle stamp that cannot have happened.

        Raises:
            ValueError: When the deadline is not after the start, when a
                settled status carries no settle stamp (or an unsettled
                one does), or when the settle stamp precedes the start.
        """
        if self.deadline_at <= self.started_at:
            raise ValueError(
                f"deadline_at must be after started_at on attempt {self.attempt} "
                f"of target {self.target_id!r}"
            )
        settled = self.status in SETTLED_TARGET_STATUSES
        if settled and self.settled_at is None:
            raise ValueError(
                f"settled status {self.status.value!r} requires settled_at on "
                f"attempt {self.attempt} of target {self.target_id!r}"
            )
        if not settled and self.settled_at is not None:
            raise ValueError(
                f"unsettled status {self.status.value!r} must not carry settled_at "
                f"on attempt {self.attempt} of target {self.target_id!r}"
            )
        if self.settled_at is not None and self.settled_at < self.started_at:
            raise ValueError(
                f"settled_at precedes started_at on attempt {self.attempt} of "
                f"target {self.target_id!r}"
            )
        return self

    @property
    def settled(self) -> bool:
        """Return whether this attempt reached a final result."""
        return self.status in SETTLED_TARGET_STATUSES


class PublicationOperation(_StrictModel):
    """One external-effect episode against one release.

    Attributes:
        schema_version: Record schema tag.
        operation_id: Stable identity across revisions of the episode.
        release_ref: ``REL-<version>`` key the episode publishes.
        kind: Which operator verb opened it.
        proof_digest: Digest binding the exact artifact set the episode
            publishes. A retry that carries a different proof digest is
            publishing something else and is refused upstream.
        idempotency_key: Caller-supplied key of the request that opened
            the episode. A repeat of the same key with the same request
            replays; with a different request it conflicts.
        request_fingerprint: Digest of the operator request that
            produced *this* snapshot. The idempotency index compares
            against it, so "same key, different payload" is a decidable
            question rather than a judgement call.
        status: Whether any leg is still outstanding.
        publication_receipts: Attempt rows keyed by ``(target_id,
            attempt)``, unique and contiguous from 1 per target.
        opened_at: When the episode opened.
        revision: Compare-and-swap revision of the operation record.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["publication-operation/v1"] = "publication-operation/v1"
    operation_id: UUID
    release_ref: ReleaseKeyStr
    kind: PublicationOperationKind
    proof_digest: Sha256DigestStr
    idempotency_key: IdempotencyKeyStr
    request_fingerprint: Sha256DigestStr | None = None
    status: PublicationOperationStatus = PublicationOperationStatus.OPEN
    publication_receipts: tuple[OperationAttempt, ...] = ()
    opened_at: UtcDatetime
    revision: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _receipts_are_keyed_and_contiguous(self) -> PublicationOperation:
        """Reject a duplicate, gapped or out-of-order attempt sequence.

        Raises:
            ValueError: When two rows share ``(target_id, attempt)``, or
                a target's attempt numbers are not exactly ``1..N`` in
                ascending row order.
        """
        seen: set[tuple[str, int]] = set()
        highest: dict[str, int] = {}
        for row in self.publication_receipts:
            key = (row.target_id, row.attempt)
            if key in seen:
                raise ValueError(
                    f"duplicate receipt for target {row.target_id!r} attempt {row.attempt}"
                )
            seen.add(key)
            expected = highest.get(row.target_id, 0) + 1
            if row.attempt != expected:
                raise ValueError(
                    f"target {row.target_id!r} attempt {row.attempt} is out of order; "
                    f"attempts must run 1..N ascending (expected {expected})"
                )
            highest[row.target_id] = row.attempt
        return self

    @model_validator(mode="after")
    def _settled_operation_has_no_open_leg(self) -> PublicationOperation:
        """Reject a settled operation that still has an unsettled leg.

        Raises:
            ValueError: When the operation claims
                :attr:`PublicationOperationStatus.SETTLED` while it
                carries no receipts at all, or carries a row that has
                not reached a final result.
        """
        if self.status is not PublicationOperationStatus.SETTLED:
            return self
        if not self.publication_receipts:
            raise ValueError(
                f"operation {self.operation_id} is settled with no publication receipts"
            )
        open_legs = sorted({row.target_id for row in self.publication_receipts if not row.settled})
        if open_legs:
            raise ValueError(f"operation {self.operation_id} is settled with open legs {open_legs}")
        return self

    @property
    def target_ids(self) -> tuple[str, ...]:
        """Return the targets this operation has attempted, in first-seen order."""
        ordered: list[str] = []
        for row in self.publication_receipts:
            if row.target_id not in ordered:
                ordered.append(row.target_id)
        return tuple(ordered)


def attempt_count(operation: PublicationOperation, target_id: str) -> int:
    """Return the highest persisted attempt number for *target_id*.

    Args:
        operation: Operation whose receipts are counted.
        target_id: Publication target to count.

    Returns:
        The highest attempt number persisted for that target, or ``0``
        when the target has no attempt row.
    """
    return max(
        (row.attempt for row in operation.publication_receipts if row.target_id == target_id),
        default=0,
    )


def latest_attempt(operation: PublicationOperation, target_id: str) -> OperationAttempt | None:
    """Return the highest-numbered attempt row for *target_id*.

    Args:
        operation: Operation whose receipts are searched.
        target_id: Publication target to look up.

    Returns:
        The row with the highest attempt number, or ``None`` when the
        target has no attempt row.
    """
    rows = [row for row in operation.publication_receipts if row.target_id == target_id]
    if not rows:
        return None
    return max(rows, key=lambda row: row.attempt)


def require_attempt(operation: PublicationOperation, target_id: str) -> OperationAttempt:
    """Return the latest attempt row for *target_id*, or raise.

    Args:
        operation: Operation whose receipts are searched.
        target_id: Publication target to look up.

    Returns:
        The highest-numbered attempt row for that target.

    Raises:
        KeyError: When the operation has never attempted that target.
    """
    row = latest_attempt(operation, target_id)
    if row is None:
        raise KeyError(
            f"operation {operation.operation_id} has no attempt for target {target_id!r}; "
            f"attempted targets: {list(operation.target_ids)}"
        )
    return row


def assert_append_only(
    previous: PublicationOperation,
    successor: PublicationOperation,
) -> None:
    """Raise unless *successor* only extends *previous*'s receipt sequence.

    The receipt sequence is the audit trail of external effect. A
    successor may append new ``(target_id, attempt)`` rows and may
    advance the status of a row it already carried, but dropping or
    re-keying a persisted row would make a double-publication invisible,
    so both are refused here rather than trusted to callers.

    Args:
        previous: The record the successor was derived from.
        successor: The candidate successor.

    Returns:
        ``None`` when the successor is a legal extension.

    Raises:
        ValueError: When the identity or revision does not follow, or a
            previously persisted attempt key was dropped or reordered.
    """
    if successor.operation_id != previous.operation_id:
        raise ValueError(
            f"successor operation_id {successor.operation_id} does not match "
            f"{previous.operation_id}"
        )
    if successor.revision <= previous.revision:
        raise ValueError(f"successor revision {successor.revision} must exceed {previous.revision}")
    old_keys = [(row.target_id, row.attempt) for row in previous.publication_receipts]
    new_keys = [(row.target_id, row.attempt) for row in successor.publication_receipts]
    if new_keys[: len(old_keys)] != old_keys:
        raise ValueError(
            f"operation {previous.operation_id} receipts are append-only; "
            f"{old_keys} is not a prefix of {new_keys}"
        )
    logger.debug(
        f"assert_append_only operation_id={successor.operation_id} "
        f"revision={successor.revision} receipts={len(new_keys)}"
    )


__all__ = [
    "EFFECT_REPORTED_STATUSES",
    "OBSERVED_TARGET_STATUSES",
    "SETTLED_TARGET_STATUSES",
    "IdempotencyKeyStr",
    "OperationAttempt",
    "PublicationOperation",
    "PublicationOperationKind",
    "PublicationOperationStatus",
    "assert_append_only",
    "attempt_count",
    "latest_attempt",
    "require_attempt",
]
