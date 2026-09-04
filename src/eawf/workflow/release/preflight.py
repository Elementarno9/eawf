"""Bridge from a readiness sweep to a Release status move.

:mod:`eawf.workflow.verify.release_readiness` decides what is true about
a checkpoint; :mod:`eawf.workflow.release.lifecycle` decides which moves
are legal. This module is the one place the two meet, so the approval
guard and the preflight-result edge read the same readiness object
rather than each recomputing their own view of "green".
"""

from __future__ import annotations

import logging
from datetime import datetime

from eawf.kernel.spec.release import Release, ReleaseStatus
from eawf.workflow.release.lifecycle import (
    ReleaseDenialCode,
    ReleaseGuardContext,
    ReleaseTransitionError,
    advance_release,
)
from eawf.workflow.verify.release_readiness import ReleaseReadiness

logger = logging.getLogger(__name__)


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


def approve_release(
    release: Release,
    readiness: ReleaseReadiness,
    *,
    approval_ref: str,
    approved_at: datetime,
) -> Release:
    """Return the approved successor of *release*, or raise the named denial.

    The approval guard reads exactly the derived required set of
    *readiness*, so an approved-but-unpublishable release is
    unreachable: whatever reds the tag chokepoint also reds here.

    Args:
        release: The candidate being approved.
        readiness: The sweep the approval binds.
        approval_ref: Reference to the approval receipt.
        approved_at: When the operator approved (timezone-aware UTC).

    Returns:
        The successor record at APPROVED.

    Raises:
        ValueError: When *readiness* was computed for a different
            release key, or *approved_at* is naive.
        ReleaseTransitionError: With
            :attr:`~eawf.workflow.release.lifecycle.ReleaseDenialCode.RELEASE_NOT_READY`
            when a required signal is not passing. The message names the
            first red gate, so the operator reads the gate they have to
            repair rather than the row underneath it.
    """
    _assert_same_release(release, readiness)
    if approved_at.tzinfo is None:
        raise ValueError("approved_at must be timezone-aware")
    logger.info(
        f"approve_release key={release.key!r} ready={readiness.ready} "
        f"first_red={readiness.first_red} first_red_gate={readiness.first_red_gate}"
    )
    try:
        return advance_release(
            release,
            ReleaseStatus.APPROVED,
            ReleaseGuardContext(gates_green=readiness.ready),
            approval_ref=approval_ref,
        )
    except ReleaseTransitionError as exc:
        if exc.code is not ReleaseDenialCode.RELEASE_NOT_READY:
            raise
        raise ReleaseTransitionError(
            exc.code, exc.frm, exc.to, f"{exc}; {_blocker(readiness)}"
        ) from exc


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


__all__ = ["approve_release", "record_preflight_result"]
