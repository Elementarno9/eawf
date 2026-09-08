"""REL-016/REL-018: settling a leg from an independent read-back.

This module is where an observation lands on the record. The vocabulary
it settles -- the frozen manifest, the request and the observation
itself -- lives in :mod:`eawf.workflow.release.observation`; collecting
one from a registry is
:func:`eawf.workflow.release.adapters.collect_observation`. The three
steps are named apart because a reader tracing a baked release has to be
able to tell "what was asked", "what came back" and "what was written"
from the call site alone.

Everything else in this package records what an adapter *said*. This
module records what an independent read-back *found*, and it is the sole
writer of :attr:`~eawf.kernel.spec.release.ReleaseTargetStatus.OBSERVED_SUCCESS`
and :attr:`~eawf.kernel.spec.release.ReleaseTargetStatus.OBSERVED_MISMATCH`.
The separation is the whole point of the vocabulary: three adapters
reporting success is three claims by the parties that made them, and a
release that baked on that would be a release nobody checked.

Three rules follow from that and are enforced here rather than trusted:

* **An observation is required, not a flag.** :func:`observe_target`
  takes a
  :class:`~eawf.workflow.release.observation.PublicationObservation` and
  writes its ``evidence_ref`` as the row's observation receipt, so an
  observed row always points back at the exact registry response that
  produced it.
* **An inconclusive read-back settles nothing.** A read-back that could
  not reach the registry, or could not read its answer, raises
  :class:`InconclusiveObservationError` instead of writing a status.
  Turning "we did not find out" into either verdict is how a release
  bakes on evidence nobody collected.
* **A contradiction routes to recovery, it does not merely annotate.**
  A mismatching or missing read-back moves the release to
  ``RECOVERING`` in the same step that settles the leg, because the
  interesting failure is the one where a leg is marked bad and the
  release keeps walking toward BAKED anyway.

Baking is the mirror rule: ``VERIFYING -> BAKED`` (and ``-> RELEASED``
for a stable version) is guarded on every *required* target standing at
``observed_success``, and until then it denies
``publication_not_observed``. The "off the default channel" half of
REL-016 is enforced upstream, in the adapters: a prerelease that the
stable default channel resolves is an ``observed_mismatch``, so it can
never be one of the rows this guard counts.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from eawf.kernel.spec.publication import PublicationOperation
from eawf.kernel.spec.release import (
    Release,
    ReleaseStatus,
    ReleaseTargetStatus,
    is_prerelease,
)
from eawf.kernel.spec.release_config import ReleaseConfig
from eawf.workflow.release.boundaries import PublicationBoundary, durable_boundary
from eawf.workflow.release.lifecycle import ReleaseGuardContext, advance_release
from eawf.workflow.release.observation import (
    ObservationResult,
    PublicationObservation,
    configured_target,
)
from eawf.workflow.release.publication import projected_target_statuses
from eawf.workflow.release.target_machine import (
    advance_target_attempt,
    current_target_status,
)

logger = logging.getLogger(__name__)

#: The target status each conclusive read-back result writes. ``MISSING``
#: shares ``observed_mismatch`` with ``MISMATCH`` deliberately: a leg
#: that reported success and cannot be found afterwards contradicts its
#: own report exactly as a wrong digest does, and both are recovery
#: questions. ``UNKNOWN`` is absent because it settles nothing.
OBSERVED_STATUS_FOR_RESULT: Final[Mapping[ObservationResult, ReleaseTargetStatus]] = {
    ObservationResult.MATCH: ReleaseTargetStatus.OBSERVED_SUCCESS,
    ObservationResult.MISMATCH: ReleaseTargetStatus.OBSERVED_MISMATCH,
    ObservationResult.MISSING: ReleaseTargetStatus.OBSERVED_MISMATCH,
}


class InconclusiveObservationError(ValueError):
    """A read-back that settled nothing was offered as a verdict.

    Attributes:
        target_id: The leg the read-back addressed.
        code: The observation code naming why nothing was settled.
    """

    def __init__(self, observation: PublicationObservation) -> None:
        """Store the leg and the code alongside the operator message."""
        super().__init__(
            f"observation_inconclusive: the read-back of target "
            f"{observation.target_id!r} answered {observation.code.value!r} and "
            f"settles nothing; {observation.detail}"
        )
        self.target_id = observation.target_id
        self.code = observation.code


def required_targets_observed(config: ReleaseConfig, operation: PublicationOperation) -> bool:
    """Return whether every required leg stands at ``observed_success``.

    Only the required targets count: an optional leg that was never
    published cannot hold the checkpoint hostage. An optional leg that
    *was* published and came back wrong is not silently forgiven either
    -- it settles as ``observed_mismatch``, which routes the release into
    recovery before this guard is ever reached.

    Args:
        config: Loaded checkpoint configuration naming every target.
        operation: The operation whose ledger is read.

    Returns:
        ``True`` when every required target was independently observed
        as a success.
    """
    return all(
        current_target_status(operation, target_id) is ReleaseTargetStatus.OBSERVED_SUCCESS
        for target_id in config.required_target_ids
    )


@durable_boundary(PublicationBoundary.OBSERVATION_RECEIPT_WRITE)
def observe_target(
    release: Release,
    config: ReleaseConfig,
    operation: PublicationOperation,
    *,
    observation: PublicationObservation,
    now: datetime,
    effect_receipt_ref: str | None = None,
) -> tuple[Release, PublicationOperation]:
    """Settle one leg against *observation* and route the release.

    Args:
        release: The record whose leg was read back.
        config: Loaded checkpoint configuration naming every target.
        operation: The operation whose leg is being settled.
        observation: The independent read-back. Its ``evidence_ref``
            becomes the row's observation receipt.
        now: Timezone-aware UTC instant of the settlement.
        effect_receipt_ref: The adapter's receipt, for a leg that timed
            out without one. An observed row still needs the effect
            receipt of the call it is observing.

    Returns:
        The routed record and the operation with that leg observed.

    Raises:
        InconclusiveObservationError: When the read-back settled nothing.
        KeyError: When the observation names an unconfigured target, or
            the operation has never attempted it.
        TargetTransitionError: When the leg cannot be observed from
            where it stands, e.g. it never reported.
        ReleaseTransitionError: When the routed release move is illegal
            from the record's current status.
        ValueError: When *now* is naive.
        ValidationError: When the observed row violates a record
            invariant, e.g. carrying no effect receipt.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not observation.conclusive:
        raise InconclusiveObservationError(observation)
    target = configured_target(config, observation.target_id)
    settled = advance_target_attempt(
        operation,
        target=target,
        to=OBSERVED_STATUS_FOR_RESULT[observation.result],
        now=now,
        effect_receipt_ref=effect_receipt_ref,
        observation_receipt_ref=observation.evidence_ref,
        observation_matched=observation.matched,
    )
    routed = route_after_observation(release, config, settled, observation=observation)
    logger.info(
        f"observe_target key={release.key!r} target={observation.target_id!r} "
        f"code={observation.code.value!r} status={routed.status.value!r} "
        f"evidence_ref={observation.evidence_ref!r}"
    )
    return routed, settled


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def route_after_observation(
    release: Release,
    config: ReleaseConfig,
    operation: PublicationOperation,
    *,
    observation: PublicationObservation,
) -> Release:
    """Return the record in the status *observation* puts it into.

    Three outcomes, in order: a contradicted read-back routes to
    ``RECOVERING``; a matched one that completes the required set bakes
    (or releases, for a stable version); anything else leaves the status
    alone and only re-projects the per-target statuses, which still
    advances a revision so a stale-revision caller is refused.

    Args:
        release: The record whose leg was just observed.
        config: Loaded checkpoint configuration naming every target.
        operation: The operation with that leg already settled.
        observation: The read-back that settled it.

    Returns:
        The routed record.

    Raises:
        ReleaseTransitionError: When the routed move is illegal from the
            record's current status.
    """
    statuses = dict(projected_target_statuses(config, operation))
    if not observation.matched:
        return advance_release(
            release,
            ReleaseStatus.RECOVERING,
            ReleaseGuardContext(),
            target_statuses=statuses,
        )
    if release.status is ReleaseStatus.VERIFYING and required_targets_observed(config, operation):
        return bake_release(release, config, operation)
    return Release.model_validate(
        release.model_copy(
            update={"target_statuses": statuses, "revision": release.revision + 1}
        ).model_dump(mode="json")
    )


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def bake_release(
    release: Release,
    config: ReleaseConfig,
    operation: PublicationOperation,
) -> Release:
    """Finish *release*: BAKED for a prerelease, RELEASED for a stable.

    Both edges share one guard -- every required target independently
    observed as a success -- and both deny ``publication_not_observed``
    when it is unmet. Which edge is taken is a pure function of the
    version, so a stable checkpoint cannot land in the prerelease
    terminal state or the other way round.

    Args:
        release: The verifying record.
        config: Loaded checkpoint configuration naming every target.
        operation: The operation whose ledger supplies the guard.

    Returns:
        The record at BAKED or RELEASED.

    Raises:
        ReleaseTransitionError: With
            :attr:`~eawf.workflow.release.lifecycle.ReleaseDenialCode.PUBLICATION_NOT_OBSERVED`
            when a required target was never independently observed, or
            ``illegal_release_transition`` from a non-VERIFYING record.
    """
    observed = required_targets_observed(config, operation)
    finished = ReleaseStatus.BAKED if is_prerelease(release.version) else ReleaseStatus.RELEASED
    baked = advance_release(
        release,
        finished,
        ReleaseGuardContext(observed_prerelease=observed, observed_stable=observed),
        target_statuses=dict(projected_target_statuses(config, operation)),
    )
    logger.info(
        f"bake_release key={release.key!r} to={finished.value!r} "
        f"required={list(config.required_target_ids)}"
    )
    return baked


__all__ = [
    "OBSERVED_STATUS_FOR_RESULT",
    "InconclusiveObservationError",
    "bake_release",
    "observe_target",
    "required_targets_observed",
    "route_after_observation",
]
