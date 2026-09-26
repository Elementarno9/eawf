"""A pinned CANDIDATE that was published out of band can still be burned.

``0.7.0.dev4`` reached every registry after its record was pinned, and the
record then had nowhere truthful to go: approval needs proof gates
receipted at a source the publication had moved past, the recovery burn
needs a publication operation nobody opened, and cancellation is refused
because the registries hold the version. These tests pin the route that
closes that -- adoption admitted at CANDIDATE, then the adopted burn
through a ``CANDIDATE -> PARTIALLY_RELEASED`` edge guarded on that
adoption -- and the refusals that keep the route from becoming a way past
approval.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.workflow.release.adoption import adopt_publication
from eawf.workflow.release.lifecycle import (
    RELEASE_TRANSITIONS,
    ReleaseDenialCode,
    ReleaseGuardName,
    ReleaseTransitionError,
    advance_release,
    cancel_release,
)
from eawf.workflow.release.publication import burn_release
from tests._release_helpers import (
    SOURCE_SHA,
    dev1_adoption,
    dev1_config,
    release_record,
)

pytestmark = pytest.mark.integration

#: The three legs the authored dev1 configuration declares.
CONFIGURED_TARGETS = ("pypi", "npm", "github")


def adopted_candidate() -> Release:
    """Return the pinned dev1 candidate after adopting all four read-backs."""
    return adopt_publication(release_record(), dev1_config(), adoption=dev1_adoption())


def partially_adopted_candidate(targets: tuple[str, ...]) -> Release:
    """Return a candidate whose adoption read back only *targets*.

    Built directly rather than through :func:`adopt_publication`, which
    refuses the shape: this is the record an adoption under a narrower
    configuration leaves behind once the configuration grows a leg.
    """
    adoption = dev1_adoption(targets=targets)
    return release_record(
        adoption=adoption,
        target_statuses=dict(adoption.observed_target_statuses),
    )


# --- adoption at CANDIDATE -----------------------------------------------


def test_adoption_admits_a_candidate_and_leaves_it_at_candidate() -> None:
    """Adoption records what the world holds; it does not move the status."""
    pinned = release_record()

    adopted = adopt_publication(pinned, dev1_config(), adoption=dev1_adoption())

    assert adopted.status is ReleaseStatus.CANDIDATE
    assert adopted.adoption == dev1_adoption()
    assert adopted.revision == pinned.revision + 1
    assert adopted.source_sha == SOURCE_SHA
    assert adopted.manifest_digest == pinned.manifest_digest
    assert dict(adopted.target_statuses) == dict(dev1_adoption().observed_target_statuses)


def test_adoption_of_a_candidate_refuses_an_unobserved_configured_target() -> None:
    """A candidate adopted blind to one declared leg is refused outright."""
    with pytest.raises(ValueError, match=r"observes no state for configured targets \['github'\]"):
        adopt_publication(
            release_record(), dev1_config(), adoption=dev1_adoption(targets=("pypi", "npm"))
        )


def test_adoption_still_refuses_a_record_past_candidate() -> None:
    """An approved record was driven by the machinery; adoption stays out."""
    approved = release_record(status=ReleaseStatus.APPROVED, approval_ref="receipt://approval/x")

    with pytest.raises(ValueError, match="recorded on a draft or a candidate"):
        adopt_publication(approved, dev1_config(), adoption=dev1_adoption())


def test_an_adopted_candidate_cannot_be_approved() -> None:
    """Adoption and approval stay exclusive, so the route is not a bypass."""
    with pytest.raises(ValidationError, match="carries an adoption and approval_ref"):
        advance_release(
            adopted_candidate(), ReleaseStatus.APPROVED, approval_ref="receipt://approval/x"
        )


def test_an_adopted_candidate_cannot_be_cancelled() -> None:
    """The registries hold the version, so a cancellation would be untrue."""
    with pytest.raises(ReleaseTransitionError) as caught:
        cancel_release(adopted_candidate())

    assert caught.value.code is ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED


# --- the burn -------------------------------------------------------------


def test_the_candidate_edge_is_declared_and_guarded_on_the_adoption() -> None:
    """The edge exists, and only the adoption guard admits it."""
    assert (
        ReleaseStatus.PARTIALLY_RELEASED,
        ReleaseGuardName.PUBLICATION_ADOPTED,
    ) in RELEASE_TRANSITIONS[ReleaseStatus.CANDIDATE]


def test_an_adopted_candidate_burns_to_partially_released() -> None:
    """The adopted burn freezes the pin and keeps the adopted projection."""
    adopted = adopted_candidate()

    burned, operation = burn_release(adopted, dev1_config(), None)

    assert operation is None
    assert burned.status is ReleaseStatus.PARTIALLY_RELEASED
    assert burned.adoption == adopted.adoption
    assert burned.approval_ref is None
    assert burned.revision == adopted.revision + 1
    assert burned.source_sha == adopted.source_sha
    assert burned.manifest_digest == adopted.manifest_digest
    assert burned.target_statuses["npm"] is ReleaseTargetStatus.OBSERVED_SUCCESS
    assert RELEASE_TRANSITIONS[burned.status] == frozenset()


def test_the_candidate_burn_reds_once_the_edge_is_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate-fire proof: without the edge the adopted candidate has no burn."""
    monkeypatch.setitem(
        RELEASE_TRANSITIONS,
        ReleaseStatus.CANDIDATE,
        frozenset(
            edge
            for edge in RELEASE_TRANSITIONS[ReleaseStatus.CANDIDATE]
            if edge[0] is not ReleaseStatus.PARTIALLY_RELEASED
        ),
    )

    with pytest.raises(ReleaseTransitionError) as caught:
        burn_release(adopted_candidate(), dev1_config(), None)

    assert caught.value.code is ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION


def test_a_candidate_with_no_adoption_is_refused_the_burn() -> None:
    """With no adoption the burn would be a terminal claim resting on nothing."""
    with pytest.raises(ValueError, match="no publication operation and no adoption"):
        burn_release(release_record(), dev1_config(), None)


def test_a_candidate_with_an_unobserved_configured_target_is_refused_the_burn() -> None:
    """The guard reads the adoption against every configured leg."""
    with pytest.raises(ReleaseTransitionError) as caught:
        burn_release(partially_adopted_candidate(("pypi", "npm")), dev1_config(), None)

    assert caught.value.code is ReleaseDenialCode.PUBLICATION_NOT_OBSERVED
    assert caught.value.frm is ReleaseStatus.CANDIDATE
    assert caught.value.to is ReleaseStatus.PARTIALLY_RELEASED


def test_a_candidate_burns_once_the_last_configured_target_is_observed() -> None:
    """Off-by-one boundary: exactly the configured set is enough."""
    burned, _operation = burn_release(
        partially_adopted_candidate(CONFIGURED_TARGETS), dev1_config(), None
    )

    assert burned.status is ReleaseStatus.PARTIALLY_RELEASED
