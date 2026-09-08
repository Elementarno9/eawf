"""REL-019: the publication edges of the Release status machine.

:mod:`eawf.workflow.release.lifecycle` declares *which* release edges
exist and what each denial is called;
:mod:`eawf.workflow.release.target_machine` walks one leg. This module
is where the two meet for the four edges that involve external effect,
and it is the only place that derives a release-level guard from the
per-target ledger:

* ``APPROVED -> PUBLISHING`` -- ``approval_fresh`` is the approval still
  binding the exact manifest digest **and** the chokepoint preflight
  recomputing green. Either half going stale denies ``approval_stale``.
* ``PUBLISHING -> VERIFYING`` -- ``target_results_complete`` is every
  configured leg carrying a success receipt. An adapter-reported
  failure on any leg is a recovery question, not a verification one, so
  it denies ``target_results_incomplete`` rather than quietly verifying
  a partial publication.
* ``RECOVERING -> PUBLISHING`` (and the same move out of
  ``PUBLISH_TIMEOUT``) -- ``idempotent_retry`` is the retry replaying
  the *same* idempotency key and proof digest with retry budget left on
  a retryable leg. Without all three it denies ``unsafe_release_retry``,
  because a "retry" that changes what is published is a second
  publication wearing the first one's name.
* ``RECOVERING -> PARTIALLY_RELEASED`` -- the burn. It takes no field
  updates at all: the burned version's ``source_sha``,
  ``source_tree_sha``, ``manifest_ref`` and ``manifest_digest`` are
  frozen because the signature gives a caller no way to move them, and
  the status is terminal, so the record can never return to ``DRAFT``
  or ``CANCELLED``. A burned version is corrected by a new version
  through ``supersedes_release_ref``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from eawf.kernel.spec.publication import (
    PublicationOperation,
    PublicationOperationKind,
    PublicationOperationStatus,
)
from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseTargetConfig
from eawf.workflow.release.boundaries import PublicationBoundary, durable_boundary
from eawf.workflow.release.lifecycle import (
    ReleaseGuardContext,
    advance_release,
    validate_release_transition,
)
from eawf.workflow.release.observation import configured_target
from eawf.workflow.release.target_machine import (
    advance_target_attempt,
    current_target_status,
    open_target_attempt,
    retry_budget_remaining,
)
from eawf.workflow.verify.release_readiness import ReleaseReadiness

logger = logging.getLogger(__name__)

#: Per-target statuses a retry may re-queue from. A leg that reported
#: success or was independently observed has nothing to retry; a leg
#: that reported failure or timed out into ``unknown`` does.
RETRYABLE_TARGET_STATUSES: frozenset[ReleaseTargetStatus] = frozenset(
    {ReleaseTargetStatus.REPORTED_FAILURE, ReleaseTargetStatus.UNKNOWN}
)

#: The results the publish and reconcile paths may write. Everything an
#: adapter can tell you about its own call, and nothing an independent
#: read-back would have to establish. The complement of this set inside
#: :data:`~eawf.kernel.spec.publication.SETTLED_TARGET_STATUSES` is
#: exactly the two ``observed_*`` statuses, which belong to
#: :mod:`eawf.workflow.release.settlement` alone.
RECONCILABLE_TARGET_STATUSES: Final[frozenset[ReleaseTargetStatus]] = frozenset(
    {
        ReleaseTargetStatus.REPORTED_SUCCESS,
        ReleaseTargetStatus.REPORTED_FAILURE,
        ReleaseTargetStatus.UNKNOWN,
    }
)


class ObserverOnlyStatusError(ValueError):
    """A non-observation path tried to write an ``observed_*`` status.

    Attributes:
        status: The observer-only status that was attempted.
    """

    def __init__(self, status: ReleaseTargetStatus) -> None:
        """Store the refused *status* alongside the operator message."""
        super().__init__(
            f"observer_only_status: {status.value!r} is written only by "
            f"release.observe_target, from an observation receipt; reconciliation "
            f"may write {sorted(s.value for s in RECONCILABLE_TARGET_STATUSES)}"
        )
        self.status = status


def request_digest(config: ReleaseConfig, target: ReleaseTargetConfig, *, proof_digest: str) -> str:
    """Return the digest of the request *config* dispatches to *target*.

    The digest covers exactly what makes two dispatches the same
    publication: the version, the channel, the target, the artifact
    kinds and the proof digest binding the artifact set. A retry that
    reproduces all five reproduces this digest, which is what lets the
    ledger tell a genuine retry from a second, different publication.

    Args:
        config: Loaded checkpoint configuration.
        target: The target leg being dispatched.
        proof_digest: Digest binding the exact artifact set.

    Returns:
        A ``sha256:``-prefixed digest.
    """
    body = json.dumps(
        {
            "version": config.version,
            "channel": config.channel.value,
            "target_id": target.target_id,
            "artifact_kinds": sorted(kind.value for kind in target.artifact_kinds),
            "proof_digest": proof_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def projected_target_statuses(
    config: ReleaseConfig,
    operation: PublicationOperation,
) -> Mapping[str, ReleaseTargetStatus]:
    """Return where every configured target stands in *operation*.

    The release record stores per-target statuses so a reader does not
    have to replay the ledger, so those statuses are *projected* from
    the ledger here rather than tracked in parallel: two counters of the
    same fact can drift, one projection cannot.

    Args:
        config: Loaded checkpoint configuration naming every target.
        operation: The operation whose ledger is projected.

    Returns:
        One entry per configured target.
    """
    return {
        target.target_id: current_target_status(operation, target.target_id)
        for target in config.targets
    }


def target_results_complete(config: ReleaseConfig, operation: PublicationOperation) -> bool:
    """Return whether every configured leg carries a success receipt.

    Args:
        config: Loaded checkpoint configuration naming every target.
        operation: The operation whose ledger is read.

    Returns:
        ``True`` when every configured target's latest attempt reported
        success or was already independently observed as a success.
    """
    complete = {ReleaseTargetStatus.REPORTED_SUCCESS, ReleaseTargetStatus.OBSERVED_SUCCESS}
    return all(
        current_target_status(operation, target.target_id) in complete for target in config.targets
    )


def retryable_targets(
    config: ReleaseConfig,
    operation: PublicationOperation,
) -> tuple[ReleaseTargetConfig, ...]:
    """Return the configured targets whose legs may still be re-queued.

    Args:
        config: Loaded checkpoint configuration naming every target.
        operation: The operation whose ledger is read.

    Returns:
        The targets sitting at a retryable status with budget left.
    """
    return tuple(
        target
        for target in config.targets
        if current_target_status(operation, target.target_id) in RETRYABLE_TARGET_STATUSES
        and retry_budget_remaining(operation, target)
    )


def recovery_exhausted(config: ReleaseConfig, operation: PublicationOperation) -> bool:
    """Return whether no configured leg has a retry left.

    Args:
        config: Loaded checkpoint configuration naming every target.
        operation: The operation whose ledger is read.

    Returns:
        ``True`` when nothing is left to retry, which is the only state
        in which burning the version is honest.
    """
    return not retryable_targets(config, operation)


@durable_boundary(
    PublicationBoundary.OPERATION_OPEN,
    PublicationBoundary.TARGET_DISPATCH,
    PublicationBoundary.TRANSITION_APPLY,
)
def begin_publication(
    release: Release,
    config: ReleaseConfig,
    readiness: ReleaseReadiness,
    *,
    operation_id: UUID,
    approved_manifest_digest: str,
    idempotency_key: str,
    proof_digest: str,
    opened_at: datetime,
) -> tuple[Release, PublicationOperation]:
    """Open the publication operation and move *release* to PUBLISHING.

    The ``approval_fresh`` guard reads both halves of what an approval
    binds: the manifest digest the operator approved must still be the
    one the record carries, and the chokepoint sweep must recompute
    green against the pinned source. Either half going stale denies
    ``approval_stale``.

    Args:
        release: The approved record.
        config: Loaded checkpoint configuration naming every target.
        readiness: The chokepoint sweep recomputed at publish time.
        operation_id: Identity for the new operation.
        approved_manifest_digest: The digest the approval bound. A
            record whose manifest has since been re-pinned no longer
            matches it, which is what makes the approval stale.
        idempotency_key: Caller-supplied key for the episode.
        proof_digest: Digest binding the exact artifact set.
        opened_at: Timezone-aware UTC instant the episode opens.

    Returns:
        The record at PUBLISHING and the operation with one queued
        attempt per configured target.

    Raises:
        ReleaseTransitionError: With
            :attr:`~eawf.workflow.release.lifecycle.ReleaseDenialCode.APPROVAL_STALE`
            when the approval no longer binds, or
            :attr:`~eawf.workflow.release.lifecycle.ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION`
            when the record is not approved.
        ValueError: When *readiness* was computed for a different
            release key, or *opened_at* is naive.
    """
    _assert_same_release(release, readiness)
    if opened_at.tzinfo is None:
        raise ValueError("opened_at must be timezone-aware")
    guards = ReleaseGuardContext(
        approval_fresh=readiness.ready and release.manifest_digest == approved_manifest_digest
    )
    validate_release_transition(release.status, ReleaseStatus.PUBLISHING, guards)
    operation = PublicationOperation(
        operation_id=operation_id,
        release_ref=release.key,
        kind=PublicationOperationKind.PUBLISH,
        proof_digest=proof_digest,
        idempotency_key=idempotency_key,
        opened_at=opened_at,
    )
    for target in config.targets:
        operation = open_target_attempt(
            operation,
            target=target,
            now=opened_at,
            request_digest=request_digest(config, target, proof_digest=proof_digest),
        )
    published = advance_release(
        release,
        ReleaseStatus.PUBLISHING,
        guards,
        publication_operation_ref=operation_reference(operation),
        target_statuses=dict(projected_target_statuses(config, operation)),
    )
    logger.info(
        f"begin_publication key={release.key!r} operation_id={operation_id} "
        f"targets={len(config.targets)} idempotency_key={idempotency_key!r}"
    )
    return published, operation


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def begin_verification(
    release: Release,
    config: ReleaseConfig,
    operation: PublicationOperation,
) -> Release:
    """Move *release* to VERIFYING once every leg reported success.

    Args:
        release: The publishing record.
        config: Loaded checkpoint configuration naming every target.
        operation: The operation whose ledger supplies the guard.

    Returns:
        The record at VERIFYING.

    Raises:
        ReleaseTransitionError: With
            :attr:`~eawf.workflow.release.lifecycle.ReleaseDenialCode.TARGET_RESULTS_INCOMPLETE`
            when a configured leg carries no success receipt.
    """
    return advance_release(
        release,
        ReleaseStatus.VERIFYING,
        ReleaseGuardContext(target_results_complete=target_results_complete(config, operation)),
        target_statuses=dict(projected_target_statuses(config, operation)),
    )


@durable_boundary(PublicationBoundary.TARGET_DISPATCH, PublicationBoundary.TRANSITION_APPLY)
def retry_publication(
    release: Release,
    config: ReleaseConfig,
    operation: PublicationOperation,
    *,
    idempotency_key: str,
    proof_digest: str,
    now: datetime,
    targets: Sequence[str] | None = None,
) -> tuple[Release, PublicationOperation]:
    """Re-queue every retryable leg and move *release* back to PUBLISHING.

    This serves both retry edges -- out of ``RECOVERING`` and out of
    ``PUBLISH_TIMEOUT`` -- under the same guard. The timeout edge is
    guarded deliberately: an unfinished publication is exactly the state
    where a naive retry double-publishes, so it is the last edge that
    should be free.

    Args:
        release: The recovering or timed-out record.
        config: Loaded checkpoint configuration naming every target.
        operation: The operation being retried.
        idempotency_key: Key the caller claims as the idempotency proof;
            must equal the operation's own.
        proof_digest: Artifact-set digest; must equal the operation's.
        now: Timezone-aware UTC instant of the retry.
        targets: Narrow the retry to these target ids. ``None`` retries
            every retryable leg, which is what the recovery sweep wants;
            the single-target verb passes one id.

    Returns:
        The record back at PUBLISHING and the operation carrying one
        fresh attempt per retryable leg.

    Raises:
        ReleaseTransitionError: With
            :attr:`~eawf.workflow.release.lifecycle.ReleaseDenialCode.UNSAFE_RELEASE_RETRY`
            when the key or the proof digest differs, or no leg has
            budget left.
        ValueError: When *now* is naive.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    retryable = tuple(
        target
        for target in retryable_targets(config, operation)
        if targets is None or target.target_id in set(targets)
    )
    guards = ReleaseGuardContext(
        idempotent_retry=(
            idempotency_key == operation.idempotency_key
            and proof_digest == operation.proof_digest
            and bool(retryable)
        )
    )
    validate_release_transition(release.status, ReleaseStatus.PUBLISHING, guards)
    retried = operation.model_copy(update={"kind": PublicationOperationKind.RETRY_TARGET})
    for target in retryable:
        retried = open_target_attempt(
            retried,
            target=target,
            now=now,
            request_digest=request_digest(config, target, proof_digest=proof_digest),
        )
    republished = advance_release(
        release,
        ReleaseStatus.PUBLISHING,
        guards,
        target_statuses=dict(projected_target_statuses(config, retried)),
    )
    logger.info(
        f"retry_publication key={release.key!r} operation_id={operation.operation_id} "
        f"retryable={[target.target_id for target in retryable]}"
    )
    return republished, retried


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def burn_release(
    release: Release,
    config: ReleaseConfig,
    operation: PublicationOperation,
) -> tuple[Release, PublicationOperation]:
    """Burn the version: move *release* to PARTIALLY_RELEASED.

    The signature is the freeze. It accepts no field updates, so the
    burned record keeps the exact ``source_sha``, ``source_tree_sha``,
    ``manifest_ref``, ``manifest_digest`` and ``version`` it carried into
    recovery; and PARTIALLY_RELEASED has no out-edges, so the record can
    never return to DRAFT or CANCELLED. The version is spent. A
    correction is a new version pointing back through
    ``supersedes_release_ref``.

    Args:
        release: The recovering record.
        config: Loaded checkpoint configuration naming every target.
        operation: The operation whose ledger supplies the guard.

    Returns:
        The burned record and the abandoned operation.

    Raises:
        ReleaseTransitionError: With
            :attr:`~eawf.workflow.release.lifecycle.ReleaseDenialCode.RECOVERY_BUDGET_AVAILABLE`
            when a leg still has a retry left.
    """
    burned = advance_release(
        release,
        ReleaseStatus.PARTIALLY_RELEASED,
        ReleaseGuardContext(recovery_exhausted=recovery_exhausted(config, operation)),
        target_statuses=dict(projected_target_statuses(config, operation)),
    )
    abandoned = operation.model_copy(
        update={
            "status": PublicationOperationStatus.ABANDONED,
            "revision": operation.revision + 1,
        }
    )
    logger.info(
        f"burn_release key={release.key!r} operation_id={operation.operation_id} "
        f"source_sha={release.source_sha!r} manifest_digest={release.manifest_digest!r}"
    )
    return burned, abandoned


@durable_boundary(PublicationBoundary.EFFECT_RECEIPT_WRITE)
def reconcile_target(
    release: Release,
    config: ReleaseConfig,
    operation: PublicationOperation,
    *,
    target_id: str,
    status: ReleaseTargetStatus,
    effect_receipt_ref: str | None = None,
    now: datetime,
) -> tuple[Release, PublicationOperation]:
    """Settle one leg against what the adapter finally reported.

    Reconciliation writes only a *reported* result -- the adapter's own
    word, recovered late. It is deliberately barred from the two
    ``observed_*`` statuses
    (:data:`RECONCILABLE_TARGET_STATUSES`): those assert an independent
    read-back, and letting the same verb that records an adapter's claim
    also record its verification would make an adapter the judge of its
    own publication. Only
    :func:`~eawf.workflow.release.settlement.observe_target` writes them,
    and only from an observation receipt.

    The release status does not move here: what a late report settles is
    one leg. The record still advances a revision, because its projected
    target statuses changed and a stale-revision caller must be refused.

    Args:
        release: The record whose leg is being reconciled.
        config: Loaded checkpoint configuration naming every target.
        operation: The operation whose leg is being settled.
        target_id: Which configured leg reported late.
        status: The reported result, from
            :data:`RECONCILABLE_TARGET_STATUSES`.
        effect_receipt_ref: The adapter's receipt, required by the two
            reported statuses.
        now: Timezone-aware UTC instant of the reconciliation.

    Returns:
        The record with re-projected target statuses, and the operation
        with that leg settled.

    Raises:
        ObserverOnlyStatusError: When *status* is an ``observed_*``
            status, which only the observation verb may write.
        KeyError: When *target_id* is not a configured target, or the
            operation has never attempted it.
        TargetTransitionError: When the leg cannot reach *status* from
            where it stands.
        ValidationError: When the settled row violates a record
            invariant, e.g. a reported status with no effect receipt.
    """
    if status not in RECONCILABLE_TARGET_STATUSES:
        raise ObserverOnlyStatusError(status)
    target = configured_target(config, target_id)
    settled = advance_target_attempt(
        operation,
        target=target,
        to=status,
        now=now,
        effect_receipt_ref=effect_receipt_ref,
    )
    reconciled = Release.model_validate(
        release.model_copy(
            update={
                "target_statuses": dict(projected_target_statuses(config, settled)),
                "revision": release.revision + 1,
            }
        ).model_dump(mode="json")
    )
    logger.info(
        f"reconcile_target key={release.key!r} target={target_id!r} "
        f"status={status.value!r} revision={reconciled.revision}"
    )
    return reconciled, settled


def operation_reference(operation: PublicationOperation) -> str:
    """Return the opaque reference a release stores for *operation*.

    Args:
        operation: The operation to reference.

    Returns:
        The ``operation://`` locator carried in
        :attr:`~eawf.kernel.spec.release.Release.publication_operation_ref`.
    """
    return f"operation://{operation.release_ref}/{operation.operation_id}"


def _assert_same_release(release: Release, readiness: ReleaseReadiness) -> None:
    """Raise when *readiness* does not belong to *release*.

    Args:
        release: The record.
        readiness: The sweep claimed for it.

    Raises:
        ValueError: When the release keys differ.
    """
    if readiness.release_key != release.key:
        raise ValueError(
            f"readiness for {readiness.release_key!r} cannot be applied to release {release.key!r}"
        )


__all__ = [
    "RECONCILABLE_TARGET_STATUSES",
    "RETRYABLE_TARGET_STATUSES",
    "ObserverOnlyStatusError",
    "begin_publication",
    "begin_verification",
    "burn_release",
    "operation_reference",
    "projected_target_statuses",
    "reconcile_target",
    "recovery_exhausted",
    "request_digest",
    "retry_publication",
    "retryable_targets",
    "target_results_complete",
]
