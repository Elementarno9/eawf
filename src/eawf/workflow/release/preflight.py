"""Bridge from a readiness sweep to a Release status move.

:mod:`eawf.workflow.verify.release_readiness` decides what is true about
a checkpoint; :mod:`eawf.workflow.release.lifecycle` decides which moves
are legal. This module is the one place the two meet, so the approval
guard and the preflight-result edge read the same readiness object
rather than each recomputing their own view of "green".
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from eawf.kernel.spec.release import (
    Release,
    ReleaseApprovalFreeze,
    ReleaseChannel,
    ReleaseCheckpoint,
    ReleaseInvalidation,
    ReleaseInvalidationCause,
    ReleaseStatus,
)
from eawf.kernel.spec.release_config import ReleaseConfig
from eawf.kernel.state.epoch2.regime import VerificationDebt, stable_release_blockers
from eawf.workflow.release.advance import CheckpointGateReceipt, assert_prerequisite_receipts
from eawf.workflow.release.boundaries import PublicationBoundary, durable_boundary
from eawf.workflow.release.lifecycle import (
    ReleaseDenialCode,
    ReleaseGuardContext,
    ReleaseTransitionError,
    advance_release,
    external_effect_started,
    validate_release_transition,
)
from eawf.workflow.verify.release_readiness import ReleaseReadiness

logger = logging.getLogger(__name__)

#: The first authority epoch whose checkpoints are approved on stored
#: gate receipts. The epoch-1 rungs were approved before any receipt
#: could be issued, so re-judging their approvals would refuse history
#: rather than guard a decision still to be taken.
RECEIPTED_APPROVAL_EPOCH: Final[int] = 2


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def record_preflight_result(release: Release, readiness: ReleaseReadiness) -> Release:
    """Return the record the *readiness* sweep puts *release* into.

    A green sweep leaves the candidate where it is -- it is now
    approvable. A sweep with any non-passing required row moves the
    candidate to :attr:`~eawf.kernel.spec.release.ReleaseStatus.PREFLIGHT_FAILED`,
    which is a deterministic result rather than a denial: preflight
    failing is an outcome, not an error.

    Args:
        release: The candidate the sweep was computed for.
        readiness: The sweep.

    Returns:
        The unchanged record when ready, otherwise its successor at
        PREFLIGHT_FAILED.

    Raises:
        ValueError: When *readiness* was computed for a different
            release key.
        ReleaseTransitionError: When *release* is not a candidate.
    """
    _assert_same_release(release, readiness)
    if readiness.ready:
        return release
    return advance_release(release, ReleaseStatus.PREFLIGHT_FAILED)


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def approve_release(
    release: Release,
    readiness: ReleaseReadiness,
    config: ReleaseConfig,
    *,
    approval_ref: str,
    approved_at: datetime,
    proof_digest: str,
    verification_debts: Sequence[VerificationDebt],
) -> Release:
    """Return the approved successor of *release*, or raise the named denial.

    The approval guard reads exactly the derived required set of
    *readiness*, so an approved-but-unpublishable release is
    unreachable: whatever reds the tag chokepoint also reds here. The
    successor freezes the inputs it accepted (:func:`approval_inputs`),
    so a later change to any of them is detected before external effect
    rather than published under this approval.

    Args:
        release: The candidate being approved.
        readiness: The sweep the approval binds.
        config: The checkpoint configuration naming the targets.
        approval_ref: Reference to the approval receipt.
        approved_at: When the operator approved (timezone-aware UTC).
        proof_digest: Digest of the exact artifact set approved.
        verification_debts: Every recorded verification debt. A stable
            release ships the whole line, so any open debt blocks it.

    Returns:
        The successor record at APPROVED, carrying its approved inputs.

    Raises:
        ValueError: When *readiness* was computed for a different
            release key, or *approved_at* is naive.
        ReleaseTransitionError: With
            :attr:`~eawf.workflow.release.lifecycle.ReleaseDenialCode.RELEASE_NOT_READY`
            when a required signal is not passing -- the message names
            the first red gate, so the operator reads the gate they have
            to repair rather than the row underneath it -- or when a
            stable release has an open verification debt.
        pydantic.ValidationError: When the frozen inputs are malformed,
            e.g. a proof digest that is not a ``sha256:`` digest.
    """
    _assert_same_release(release, readiness)
    if approved_at.tzinfo is None:
        raise ValueError("approved_at must be timezone-aware")
    logger.info(
        f"approve_release key={release.key!r} ready={readiness.ready} "
        f"first_red={readiness.first_red} first_red_gate={readiness.first_red_gate}"
    )
    guards = ReleaseGuardContext(gates_green=readiness.ready)
    try:
        validate_release_transition(release.status, ReleaseStatus.APPROVED, guards)
    except ReleaseTransitionError as exc:
        if exc.code is not ReleaseDenialCode.RELEASE_NOT_READY:
            raise
        raise ReleaseTransitionError(
            exc.code, exc.frm, exc.to, f"{exc}; {_blocker(readiness)}"
        ) from exc
    blockers = (
        stable_release_blockers(verification_debts)
        if release.channel is ReleaseChannel.STABLE
        else ()
    )
    if blockers:
        raise ReleaseTransitionError(
            ReleaseDenialCode.RELEASE_NOT_READY,
            release.status,
            ReleaseStatus.APPROVED,
            f"{ReleaseDenialCode.RELEASE_NOT_READY.value}: open verification debt "
            f"{list(blockers)} blocks stable approval until its deferred gate passes",
        )
    return advance_release(
        release,
        ReleaseStatus.APPROVED,
        guards,
        approval_ref=approval_ref,
        approved_inputs=approval_inputs(release, config, proof_digest=proof_digest),
    )


def approval_inputs(
    release: Release, config: ReleaseConfig, *, proof_digest: str
) -> ReleaseApprovalFreeze:
    """Return the approval-bound inputs *release* and *config* hold now.

    Args:
        release: A pinned record.
        config: Its checkpoint configuration, naming the targets.
        proof_digest: Digest of the artifact set the caller offers.

    Returns:
        The inputs an approval freezes, as they stand.

    Raises:
        pydantic.ValidationError: When the record is not pinned or a
            digest is malformed.
    """
    return ReleaseApprovalFreeze.model_validate(
        {
            "membership_refs": release.membership_refs,
            "source_sha": release.source_sha,
            "source_tree_sha": release.source_tree_sha,
            "tag": f"v{release.version}",
            "manifest_ref": release.manifest_ref,
            "manifest_digest": release.manifest_digest,
            "target_ids": sorted({target.target_id for target in config.targets}),
            "policy_revision": release.policy_revision,
            "proof_digest": proof_digest,
        }
    )


def approval_drift(
    approved: ReleaseApprovalFreeze, current: ReleaseApprovalFreeze
) -> tuple[ReleaseInvalidationCause, str] | None:
    """Return the first approved input *current* no longer matches.

    Args:
        approved: The inputs the approval froze.
        current: The inputs as they stand before external effect.

    Returns:
        The typed cause and a sentence naming the moved field, or
        ``None`` when every approved input is unchanged.
    """
    for cause, fields in _DRIFT_FIELDS:
        for name in fields:
            was, now = getattr(approved, name), getattr(current, name)
            if was != now:
                return cause, f"{name} changed from {was!r} to {now!r} after approval"
    return None


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def invalidate_changed_approval(
    release: Release, config: ReleaseConfig, *, proof_digest: str, at: datetime
) -> Release | None:
    """Return *release* returned to DRAFT when an approved input changed.

    Runs before any external effect: an approval is a decision about the
    inputs it froze, so a changed input voids it and the record goes back
    to DRAFT carrying the typed cause, rather than publishing something
    nobody approved.

    Args:
        release: The approved record about to publish.
        config: Its checkpoint configuration, naming the targets.
        proof_digest: Digest of the artifact set about to publish.
        at: Timezone-aware UTC instant of the check.

    Returns:
        The successor at DRAFT with :attr:`Release.last_invalidation`
        set, or ``None`` when nothing changed or the approval predates
        frozen inputs.

    Raises:
        ReleaseTransitionError: ``release_effect_already_started`` when
            the record already shows external effect, or
            ``illegal_release_transition`` when it is not approved.
        pydantic.ValidationError: When the current inputs are malformed.
    """
    approved = release.approved_inputs
    if approved is None:
        return None
    drift = approval_drift(approved, approval_inputs(release, config, proof_digest=proof_digest))
    if drift is None:
        return None
    cause, detail = drift
    logger.warning(f"invalidate_changed_approval key={release.key!r} cause={cause.value!r}")
    return advance_release(
        release,
        ReleaseStatus.DRAFT,
        ReleaseGuardContext(external_effect_started=external_effect_started(release)),
        approval_ref=None,
        approved_inputs=None,
        last_invalidation=ReleaseInvalidation(
            cause=cause,
            invalidated_at=at,
            detail=detail,
            prior_status=release.status,
            invalidated_approval_ref=release.approval_ref,
        ),
    )


#: The approved inputs grouped by the invalidation cause their change
#: records, in the order a drift is reported.
_DRIFT_FIELDS: Final[tuple[tuple[ReleaseInvalidationCause, tuple[str, ...]], ...]] = (
    (ReleaseInvalidationCause.HEAD_MOVED, ("source_sha", "source_tree_sha", "tag")),
    (ReleaseInvalidationCause.MEMBERSHIP_CHANGED, ("membership_refs",)),
    (
        ReleaseInvalidationCause.ARTIFACT_CHANGED,
        ("manifest_ref", "manifest_digest", "proof_digest"),
    ),
    (ReleaseInvalidationCause.TARGETS_CHANGED, ("target_ids",)),
    (ReleaseInvalidationCause.POLICY_CHANGED, ("policy_revision",)),
)


def assert_approval_receipts(
    release: Release,
    config: ReleaseConfig,
    receipts: Sequence[CheckpointGateReceipt],
    *,
    rung: ReleaseCheckpoint,
    now: datetime,
    train_id: str,
) -> tuple[str, ...]:
    """Return the receipt refs an approval of *release* stands on.

    A readiness sweep says the gates were green when it was computed; a
    receipt says a gate was proven on the exact source and manifest the
    candidate pins. From :data:`RECEIPTED_APPROVAL_EPOCH` on, an
    approval needs both, so a checkpoint cannot be approved on a gate
    nobody proved at the commit it will publish.

    Args:
        release: The candidate being approved.
        config: Its loaded configuration; ``gates.required`` names every
            gate that must carry a receipt.
        receipts: The newest stored receipt of each gate.
        rung: The train rung *release* occupies.
        now: Timezone-aware UTC instant freshness is judged at.
        train_id: Train the rung belongs to, for the error.

    Returns:
        The validated receipt references in gate order, or ``()`` for a
        rung of an earlier epoch.

    Raises:
        TrainAdvanceError: ``prerequisite_receipt_missing`` or
            ``prerequisite_receipt_stale``, naming the gate.
        ValueError: When *now* is naive, *config* describes another
            checkpoint, or a gate is offered twice.
    """
    if rung.authority_epoch < RECEIPTED_APPROVAL_EPOCH:
        return ()
    return assert_prerequisite_receipts(release, config, receipts, now=now, train_id=train_id)


def _blocker(readiness: ReleaseReadiness) -> str:
    """Return the clause naming what is holding *readiness* back.

    Args:
        readiness: The sweep that denied the approval.

    Returns:
        A clause naming the first red gate, or the waiver disposition
        when every gate is green and only waivers remain.
    """
    gate = readiness.first_red_gate
    if gate is not None:
        return f"first red gate {gate.value!r} (evidence {readiness.gate_row(gate).evidence_ref!r})"
    return (
        f"every gate is green; {readiness.waiver_count} waiver(s) are "
        f"{readiness.waiver_disposition.value!r}"
    )


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
    "RECEIPTED_APPROVAL_EPOCH",
    "approval_drift",
    "approval_inputs",
    "approve_release",
    "assert_approval_receipts",
    "invalidate_changed_approval",
    "record_preflight_result",
]
