"""Adopting a publication that happened without a release record.

Every other path in this package assumes the machinery was there: a
manifest was pinned, a sweep ran, an operator approved, an operation
dispatched, an observer read back. A version published outside all of
that has none of those facts, and the model used to have no way to say
so -- which left such a record with no truthful terminal state at all.
Cancelling would claim nothing was published; approving would claim an
approval nobody gave.

Adoption is the missing move, and its whole design is about *not*
becoming a way around the normal path:

* it records only what an independent reader could still see afterwards
  -- one :class:`~eawf.kernel.spec.release.AdoptedTargetObservation` per
  target, each carrying its own read-back evidence -- and never a
  manifest pin, a readiness sweep or an approval;
* it refuses an adoption that does not cover every configured target,
  because a record blind to half the publication is the defect it exists
  to fix, not a lesser version of the fix;
* the record it produces is distinguishable from an approved one by
  construction: :class:`~eawf.kernel.spec.release.Release` forbids
  :attr:`~eawf.kernel.spec.release.Release.adoption` and
  :attr:`~eawf.kernel.spec.release.Release.approval_ref` on the same
  record, so a reader never has to interpret a reference string to tell
  an adopted checkpoint from an earned one;
* it does not end the checkpoint. The adopted record stays at DRAFT,
  now carrying the external effect it was blind to, and is driven
  terminal by the same
  :func:`~eawf.workflow.release.publication.burn_release` the recovery
  path uses. There is one terminal burn, reached two ways.

Targets the checkpoint configuration never declared are recorded rather
than refused: an uncontrolled publication can reach somewhere the
configuration does not know about, and dropping that row would put the
record back in the dark about it.
"""

from __future__ import annotations

import logging

from eawf.kernel.spec.release import Release, ReleaseAdoption, ReleaseStatus
from eawf.kernel.spec.release_config import ReleaseConfig

logger = logging.getLogger(__name__)


def unconfigured_targets(config: ReleaseConfig, adoption: ReleaseAdoption) -> tuple[str, ...]:
    """Return the adopted target ids *config* does not declare.

    Args:
        config: Loaded checkpoint configuration naming every target.
        adoption: The adoption whose rows are compared.

    Returns:
        The observed target ids with no configured leg, sorted. Empty
        when the publication stayed inside the declared target set.
    """
    declared = {target.target_id for target in config.targets}
    return tuple(sorted(set(adoption.observed_target_statuses) - declared))


def adopt_publication(
    release: Release,
    config: ReleaseConfig,
    *,
    adoption: ReleaseAdoption,
) -> Release:
    """Record *adoption* onto *release* and return the successor.

    The status does not move: adoption is a statement about what the
    world holds, and what to do about it is the next decision, taken by
    a separate verb. What does move is the record's knowledge -- after
    this call it carries the per-target read-backs, so every guard that
    asks whether external effect has started can finally get a true
    answer out of it.

    Args:
        release: The DRAFT record of the checkpoint that was published.
        config: Loaded checkpoint configuration naming every target.
        adoption: The observed facts and the reason they are being
            adopted.

    Returns:
        The successor record at DRAFT, carrying the adoption and the
        per-target projection it asserts, at the next revision.

    Raises:
        ValueError: When the record is not at DRAFT, when it already
            carries an adoption, or when a configured target has no
            observation -- an adoption that leaves a declared leg
            unobserved would leave the record blind to part of the
            publication it claims to describe.
        ValidationError: When the successor violates a
            :class:`~eawf.kernel.spec.release.Release` invariant, e.g.
            an adoption landing beside an approval reference.
    """
    if release.status is not ReleaseStatus.DRAFT:
        raise ValueError(
            f"release {release.key!r} is {release.status.value!r}; an adoption is "
            f"recorded on a draft, before any status the machinery itself produced"
        )
    if release.adoption is not None:
        raise ValueError(
            f"release {release.key!r} already carries an adoption recorded at "
            f"{release.adoption.adopted_at.isoformat()}"
        )
    observed = adoption.observed_target_statuses
    unobserved = sorted(
        target.target_id for target in config.targets if target.target_id not in observed
    )
    if unobserved:
        raise ValueError(
            f"adoption of {release.key!r} observes no state for configured targets "
            f"{unobserved}; every declared leg must be read back or the record "
            f"stays blind to part of the publication"
        )
    successor = release.model_copy(
        update={
            "adoption": adoption,
            "target_statuses": {**dict(release.target_statuses), **dict(observed)},
            "revision": release.revision + 1,
        }
    )
    adopted = Release.model_validate(successor.model_dump(mode="json"))
    logger.info(
        f"adopt_publication key={adopted.key!r} targets={sorted(observed)} "
        f"unconfigured={list(unconfigured_targets(config, adoption))} "
        f"revision={adopted.revision}"
    )
    return adopted


__all__ = [
    "adopt_publication",
    "unconfigured_targets",
]
