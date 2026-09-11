"""The Release status machine: guarded edges plus their named denials.

The lifecycle entities of the workflow (wave, phase, iter, spec) share
one table shape in :mod:`eawf.workflow.lifecycle.spec`. A Release needs
a different guard vocabulary -- manifest completeness, gate greenness,
approval freshness, independent observation -- and, unlike those tables,
each denied edge carries a *named error code* that the operator surface
and the daemon both quote. Rather than widening the shared
:class:`~eawf.workflow.lifecycle.spec.GuardContext` with release-only
booleans, the release machine owns its own table here.

Two rules shape the table and are worth stating before reading it:

* **No terminal state returns to DRAFT.** ``BAKED``, ``RELEASED``,
  ``CANCELLED`` and ``PARTIALLY_RELEASED`` have no out-edges. A burned
  or published version is corrected by a *new* version linked through
  ``supersedes_release_ref``, never by reopening the old record.
* **Only observation bakes.** ``VERIFYING -> BAKED`` and
  ``VERIFYING -> RELEASED`` are guarded on independently observed
  target states, so three adapters reporting success can never by
  themselves finish a release.

DRAFT carries two edges beyond the pin, and both exist because a
publication can happen without this machine's participation. A draft
that touched nothing may be abandoned through ``DRAFT -> CANCELLED``;
one that adopted an uncontrolled publication may not, because the
``no_external_effect`` guard is computed from the record by
:func:`external_effect_started` rather than supplied by the caller.
``DRAFT -> PARTIALLY_RELEASED`` is where such a record stops: the burn,
reached with no publication operation to exhaust because none was ever
opened.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.workflow.release.boundaries import PublicationBoundary, durable_boundary

logger = logging.getLogger(__name__)


class ReleaseGuardName(StrEnum):
    """Named predicates a guarded release edge can attach.

    Values:
        NONE: No predicate; the source status alone permits the move.
        MANIFEST_COMPLETE: Membership accepted, source exact and clean,
            target set complete -- the pin precondition.
        GATES_GREEN: Every derived-required readiness row passes.
        APPROVAL_FRESH: The approval receipt still binds the exact
            manifest digest and the tag preflight recomputes green.
        NO_EXTERNAL_EFFECT: No tag, upload or other external effect has
            been observed for this release yet.
        TARGET_RESULTS_COMPLETE: Every configured leg carries a success
            receipt.
        OBSERVED_PRERELEASE: Every required target independently exposes
            the exact prerelease on its non-default channel.
        OBSERVED_STABLE: Every required target independently exposes the
            exact stable version and digests.
        IDEMPOTENT_RETRY: The retry carries an idempotency proof and the
            source, tag and artifact digests are unchanged, with retry
            budget remaining.
        RECOVERY_EXHAUSTED: The recovery budget is spent -- or never
            existed, for a publication that ran outside the machinery --
            and the operator acknowledges the burned version.
    """

    NONE = "none"
    MANIFEST_COMPLETE = "manifest_complete"
    GATES_GREEN = "gates_green"
    APPROVAL_FRESH = "approval_fresh"
    NO_EXTERNAL_EFFECT = "no_external_effect"
    TARGET_RESULTS_COMPLETE = "target_results_complete"
    OBSERVED_PRERELEASE = "observed_prerelease"
    OBSERVED_STABLE = "observed_stable"
    IDEMPOTENT_RETRY = "idempotent_retry"
    RECOVERY_EXHAUSTED = "recovery_exhausted"


class ReleaseDenialCode(StrEnum):
    """Named error a denied release transition raises.

    Values:
        RELEASE_MANIFEST_INCOMPLETE: The pin precondition is unmet.
        RELEASE_NOT_READY: A required gate is not green at approval.
        APPROVAL_STALE: The approval no longer binds the exact manifest.
        PUBLICATION_NOT_OBSERVED: A required target was never
            independently observed.
        RELEASE_EFFECT_ALREADY_STARTED: Cancellation came after an
            external effect.
        TARGET_RESULTS_INCOMPLETE: A configured leg carries no result.
        UNSAFE_RELEASE_RETRY: A retry without an idempotency proof.
        RECOVERY_BUDGET_AVAILABLE: A burn declared while retries remain.
        ILLEGAL_RELEASE_TRANSITION: The edge is absent from the table.
    """

    RELEASE_MANIFEST_INCOMPLETE = "release_manifest_incomplete"
    RELEASE_NOT_READY = "release_not_ready"
    APPROVAL_STALE = "approval_stale"
    PUBLICATION_NOT_OBSERVED = "publication_not_observed"
    RELEASE_EFFECT_ALREADY_STARTED = "release_effect_already_started"
    TARGET_RESULTS_INCOMPLETE = "target_results_incomplete"
    UNSAFE_RELEASE_RETRY = "unsafe_release_retry"
    RECOVERY_BUDGET_AVAILABLE = "recovery_budget_available"
    ILLEGAL_RELEASE_TRANSITION = "illegal_release_transition"


class ReleaseTransitionError(Exception):
    """A release status move was denied.

    Attributes:
        code: The named denial the transition table declares for this
            edge, or :attr:`ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION`
            when the edge does not exist at all.
        frm: Status the record is in.
        to: Status the caller intended to move to.
    """

    def __init__(
        self,
        code: ReleaseDenialCode,
        frm: ReleaseStatus,
        to: ReleaseStatus,
        message: str,
    ) -> None:
        """Store the typed denial *code* alongside the edge it denied."""
        super().__init__(message)
        self.code = code
        self.frm = frm
        self.to = to


@dataclass(frozen=True, slots=True)
class ReleaseGuardContext:
    """Predicate inputs the named release guards evaluate.

    The caller computes each predicate (it owns the readiness object,
    the receipts and the ledger) and passes booleans here, so the
    validator stays a pure status-machine evaluator. Every field
    defaults to the permissive value so a test or a caller that only
    exercises one guard does not have to spell the other nine.

    Attributes:
        manifest_complete: Backs :attr:`ReleaseGuardName.MANIFEST_COMPLETE`.
        gates_green: Backs :attr:`ReleaseGuardName.GATES_GREEN`.
        approval_fresh: Backs :attr:`ReleaseGuardName.APPROVAL_FRESH`.
        external_effect_started: Inverted to back
            :attr:`ReleaseGuardName.NO_EXTERNAL_EFFECT`.
        target_results_complete: Backs
            :attr:`ReleaseGuardName.TARGET_RESULTS_COMPLETE`.
        observed_prerelease: Backs
            :attr:`ReleaseGuardName.OBSERVED_PRERELEASE`.
        observed_stable: Backs :attr:`ReleaseGuardName.OBSERVED_STABLE`.
        idempotent_retry: Backs :attr:`ReleaseGuardName.IDEMPOTENT_RETRY`.
        recovery_exhausted: Backs
            :attr:`ReleaseGuardName.RECOVERY_EXHAUSTED`.
    """

    manifest_complete: bool = True
    gates_green: bool = True
    approval_fresh: bool = True
    external_effect_started: bool = False
    target_results_complete: bool = True
    observed_prerelease: bool = True
    observed_stable: bool = True
    idempotent_retry: bool = True
    recovery_exhausted: bool = True


#: Guarded release status machine. Each edge is a
#: ``(target, ReleaseGuardName)`` pair; the four terminal statuses carry
#: empty out-edge sets. Derived from the release lifecycle table: pin,
#: preflight, the three pre-effect invalidation edges, approval,
#: cancellation, publication, timeout, verification, observation and
#: recovery.
RELEASE_TRANSITIONS: Final[
    dict[ReleaseStatus, frozenset[tuple[ReleaseStatus, ReleaseGuardName]]]
] = {
    ReleaseStatus.DRAFT: frozenset(
        {
            (ReleaseStatus.CANDIDATE, ReleaseGuardName.MANIFEST_COMPLETE),
            (ReleaseStatus.CANCELLED, ReleaseGuardName.NO_EXTERNAL_EFFECT),
            (ReleaseStatus.PARTIALLY_RELEASED, ReleaseGuardName.RECOVERY_EXHAUSTED),
        }
    ),
    ReleaseStatus.CANDIDATE: frozenset(
        {
            (ReleaseStatus.PREFLIGHT_FAILED, ReleaseGuardName.NONE),
            (ReleaseStatus.DRAFT, ReleaseGuardName.NO_EXTERNAL_EFFECT),
            (ReleaseStatus.CANCELLED, ReleaseGuardName.NO_EXTERNAL_EFFECT),
            (ReleaseStatus.APPROVED, ReleaseGuardName.GATES_GREEN),
        }
    ),
    ReleaseStatus.PREFLIGHT_FAILED: frozenset(
        {
            (ReleaseStatus.DRAFT, ReleaseGuardName.NO_EXTERNAL_EFFECT),
            (ReleaseStatus.CANCELLED, ReleaseGuardName.NO_EXTERNAL_EFFECT),
        }
    ),
    ReleaseStatus.APPROVED: frozenset(
        {
            (ReleaseStatus.DRAFT, ReleaseGuardName.NO_EXTERNAL_EFFECT),
            (ReleaseStatus.CANCELLED, ReleaseGuardName.NO_EXTERNAL_EFFECT),
            (ReleaseStatus.PUBLISHING, ReleaseGuardName.APPROVAL_FRESH),
        }
    ),
    ReleaseStatus.PUBLISHING: frozenset(
        {
            (ReleaseStatus.VERIFYING, ReleaseGuardName.TARGET_RESULTS_COMPLETE),
            (ReleaseStatus.PUBLISH_TIMEOUT, ReleaseGuardName.NONE),
            (ReleaseStatus.RECOVERING, ReleaseGuardName.NONE),
        }
    ),
    ReleaseStatus.PUBLISH_TIMEOUT: frozenset(
        {
            (ReleaseStatus.PUBLISHING, ReleaseGuardName.IDEMPOTENT_RETRY),
            (ReleaseStatus.RECOVERING, ReleaseGuardName.NONE),
        }
    ),
    ReleaseStatus.VERIFYING: frozenset(
        {
            (ReleaseStatus.BAKED, ReleaseGuardName.OBSERVED_PRERELEASE),
            (ReleaseStatus.RELEASED, ReleaseGuardName.OBSERVED_STABLE),
            (ReleaseStatus.RECOVERING, ReleaseGuardName.NONE),
        }
    ),
    ReleaseStatus.RECOVERING: frozenset(
        {
            (ReleaseStatus.PUBLISHING, ReleaseGuardName.IDEMPOTENT_RETRY),
            (
                ReleaseStatus.PARTIALLY_RELEASED,
                ReleaseGuardName.RECOVERY_EXHAUSTED,
            ),
        }
    ),
    ReleaseStatus.CANCELLED: frozenset(),
    ReleaseStatus.BAKED: frozenset(),
    ReleaseStatus.RELEASED: frozenset(),
    ReleaseStatus.PARTIALLY_RELEASED: frozenset(),
}


#: The named error each guarded edge raises when its guard is unmet.
#: Every guarded edge in :data:`RELEASE_TRANSITIONS` appears here; the
#: unguarded (``NONE``) edges do not, because they cannot be denied.
RELEASE_DENIALS: Final[Mapping[tuple[ReleaseStatus, ReleaseStatus], ReleaseDenialCode]] = {
    (ReleaseStatus.DRAFT, ReleaseStatus.CANDIDATE): (ReleaseDenialCode.RELEASE_MANIFEST_INCOMPLETE),
    (ReleaseStatus.DRAFT, ReleaseStatus.CANCELLED): (
        ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED
    ),
    (ReleaseStatus.DRAFT, ReleaseStatus.PARTIALLY_RELEASED): (
        ReleaseDenialCode.RECOVERY_BUDGET_AVAILABLE
    ),
    (ReleaseStatus.CANDIDATE, ReleaseStatus.APPROVED): (ReleaseDenialCode.RELEASE_NOT_READY),
    (ReleaseStatus.CANDIDATE, ReleaseStatus.DRAFT): (
        ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED
    ),
    (ReleaseStatus.CANDIDATE, ReleaseStatus.CANCELLED): (
        ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED
    ),
    (ReleaseStatus.PREFLIGHT_FAILED, ReleaseStatus.DRAFT): (
        ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED
    ),
    (ReleaseStatus.PREFLIGHT_FAILED, ReleaseStatus.CANCELLED): (
        ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED
    ),
    (ReleaseStatus.APPROVED, ReleaseStatus.DRAFT): (
        ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED
    ),
    (ReleaseStatus.APPROVED, ReleaseStatus.CANCELLED): (
        ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED
    ),
    (ReleaseStatus.APPROVED, ReleaseStatus.PUBLISHING): (ReleaseDenialCode.APPROVAL_STALE),
    (ReleaseStatus.PUBLISHING, ReleaseStatus.VERIFYING): (
        ReleaseDenialCode.TARGET_RESULTS_INCOMPLETE
    ),
    (ReleaseStatus.PUBLISH_TIMEOUT, ReleaseStatus.PUBLISHING): (
        ReleaseDenialCode.UNSAFE_RELEASE_RETRY
    ),
    (ReleaseStatus.VERIFYING, ReleaseStatus.BAKED): (ReleaseDenialCode.PUBLICATION_NOT_OBSERVED),
    (ReleaseStatus.VERIFYING, ReleaseStatus.RELEASED): (ReleaseDenialCode.PUBLICATION_NOT_OBSERVED),
    (ReleaseStatus.RECOVERING, ReleaseStatus.PUBLISHING): (ReleaseDenialCode.UNSAFE_RELEASE_RETRY),
    (ReleaseStatus.RECOVERING, ReleaseStatus.PARTIALLY_RELEASED): (
        ReleaseDenialCode.RECOVERY_BUDGET_AVAILABLE
    ),
}


#: Statuses with no out-edges. A release that reaches one of these is
#: finished; correction is a new version, never a reopen.
TERMINAL_RELEASE_STATUSES: Final[frozenset[ReleaseStatus]] = frozenset(
    status for status, edges in RELEASE_TRANSITIONS.items() if not edges
)


def _guard_satisfied(guard: ReleaseGuardName, ctx: ReleaseGuardContext) -> bool:
    """Return whether *guard* holds against *ctx*.

    Args:
        guard: The named predicate attached to the edge.
        ctx: Predicate inputs computed by the caller.

    Returns:
        ``True`` when the predicate holds. :attr:`ReleaseGuardName.NONE`
        always holds; :attr:`ReleaseGuardName.NO_EXTERNAL_EFFECT` is the
        negation of :attr:`ReleaseGuardContext.external_effect_started`.
    """
    return {
        ReleaseGuardName.NONE: True,
        ReleaseGuardName.MANIFEST_COMPLETE: ctx.manifest_complete,
        ReleaseGuardName.GATES_GREEN: ctx.gates_green,
        ReleaseGuardName.APPROVAL_FRESH: ctx.approval_fresh,
        ReleaseGuardName.NO_EXTERNAL_EFFECT: not ctx.external_effect_started,
        ReleaseGuardName.TARGET_RESULTS_COMPLETE: ctx.target_results_complete,
        ReleaseGuardName.OBSERVED_PRERELEASE: ctx.observed_prerelease,
        ReleaseGuardName.OBSERVED_STABLE: ctx.observed_stable,
        ReleaseGuardName.IDEMPOTENT_RETRY: ctx.idempotent_retry,
        ReleaseGuardName.RECOVERY_EXHAUSTED: ctx.recovery_exhausted,
    }[guard]


def external_effect_started(release: Release) -> bool:
    """Return whether *release* carries evidence of external effect.

    The ``no_external_effect`` guard used to be answered by whichever
    caller happened to be moving the record, and no caller ever computed
    it -- which is how a version published to four registries could
    still have satisfied it. The answer belongs to the record, so it is
    derived here from the three facts a record can hold about the world:
    an adopted uncontrolled publication, an opened publication episode,
    or any leg that got past ``not_started``.

    Args:
        release: The record to interrogate.

    Returns:
        ``True`` when the record itself can see that something was
        published, queued or adopted under this version.
    """
    return (
        release.adoption is not None
        or release.publication_operation_ref is not None
        or any(
            status is not ReleaseTargetStatus.NOT_STARTED
            for status in release.target_statuses.values()
        )
    )


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def cancel_release(release: Release) -> Release:
    """Abandon *release* before any external effect, or refuse.

    A cancellation is a claim that nothing was published under this
    version, so the claim is checked against the record rather than
    taken from the caller: the guard context is built by
    :func:`external_effect_started`, not accepted as an argument. A
    record that adopted an uncontrolled publication, opened an episode
    or moved a leg is refused ``release_effect_already_started``.

    Args:
        release: The record to cancel.

    Returns:
        The successor record at :attr:`ReleaseStatus.CANCELLED`.

    Raises:
        ReleaseTransitionError: With
            :attr:`ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED`
            when the record can see external effect, or
            :attr:`ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION` when
            the status it stands in has no cancel edge at all.
    """
    cancelled = advance_release(
        release,
        ReleaseStatus.CANCELLED,
        ReleaseGuardContext(external_effect_started=external_effect_started(release)),
    )
    logger.info(f"cancel_release key={release.key!r} frm={release.status.value!r}")
    return cancelled


def next_release_statuses(status: ReleaseStatus) -> frozenset[ReleaseStatus]:
    """Return the statuses reachable from *status* in one transition.

    Args:
        status: Source status.

    Returns:
        The set of legal targets, ignoring guards. Empty for a terminal
        status.
    """
    return frozenset(target for target, _guard in RELEASE_TRANSITIONS[status])


def validate_release_transition(
    frm: ReleaseStatus,
    to: ReleaseStatus,
    ctx: ReleaseGuardContext | None = None,
) -> None:
    """Guard one release status move against :data:`RELEASE_TRANSITIONS`.

    Args:
        frm: Status the record is in.
        to: Status the caller intends to move to.
        ctx: Predicate inputs. ``None`` is an all-satisfied context,
            which is correct for the unguarded edges.

    Raises:
        ReleaseTransitionError: When the edge is absent from the table
            (code :attr:`ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION`)
            or a guard on it is unmet (the code
            :data:`RELEASE_DENIALS` declares for that edge).
    """
    context = ctx if ctx is not None else ReleaseGuardContext()
    guards = sorted(
        (guard for target, guard in RELEASE_TRANSITIONS[frm] if target == to),
        key=lambda guard: guard.value,
    )
    if not guards:
        raise ReleaseTransitionError(
            ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION,
            frm,
            to,
            f"illegal release transition {frm.value!r} -> {to.value!r}; "
            f"legal targets: {sorted(s.value for s in next_release_statuses(frm))}",
        )
    for guard in guards:
        if _guard_satisfied(guard, context):
            continue
        code = RELEASE_DENIALS[(frm, to)]
        raise ReleaseTransitionError(
            code,
            frm,
            to,
            f"{code.value}: release transition {frm.value!r} -> {to.value!r} "
            f"blocked by guard {guard.value!r}",
        )


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def advance_release(
    release: Release,
    to: ReleaseStatus,
    ctx: ReleaseGuardContext | None = None,
    **updates: object,
) -> Release:
    """Return a new :class:`Release` at *to*, after validating the edge.

    Records are frozen, so an advance produces a successor rather than
    mutating in place: the returned record carries the new status, an
    incremented revision, and whichever pinned fields the caller
    supplies through *updates* (the manifest binding on a pin, the
    approval reference on an approval).

    Args:
        release: The record to advance.
        to: Target status.
        ctx: Guard predicate inputs.
        **updates: Additional field overrides applied alongside the
            status change, e.g. ``manifest_digest=...``.

    Returns:
        The successor record.

    Raises:
        ReleaseTransitionError: When the edge is illegal or a guard is
            unmet.
        ValueError: When the successor violates a :class:`Release`
            invariant, e.g. advancing to CANDIDATE without a pin.
    """
    validate_release_transition(release.status, to, ctx)
    successor = release.model_copy(
        update={"status": to, "revision": release.revision + 1, **updates}
    )
    validated = Release.model_validate(successor.model_dump(mode="json"))
    logger.info(
        f"advance_release key={release.key!r} frm={release.status.value!r} "
        f"to={to.value!r} revision={validated.revision}"
    )
    return validated


__all__ = [
    "RELEASE_DENIALS",
    "RELEASE_TRANSITIONS",
    "TERMINAL_RELEASE_STATUSES",
    "ReleaseDenialCode",
    "ReleaseGuardContext",
    "ReleaseGuardName",
    "ReleaseTransitionError",
    "advance_release",
    "cancel_release",
    "external_effect_started",
    "next_release_statuses",
    "validate_release_transition",
]
