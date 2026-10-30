"""A published CANDIDATE reaches a terminal status once every target is recorded.

A candidate can be published by a run that opened no publication
operation, so neither approval nor the recovery burn has anything to
act on and cancellation is refused because the registries hold the
version. The record ends through adoption followed by the adopted burn;
these tests pin the boundaries of that route: the configured target set
must be fully recorded, and only an observed status counts as recorded.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.release import (
    Release,
    ReleaseAdoption,
    ReleaseStatus,
    ReleaseTargetStatus,
)
from eawf.kernel.spec.release_config import ReleaseConfig
from eawf.workflow.release.adoption import adopt_publication
from eawf.workflow.release.lifecycle import (
    TERMINAL_RELEASE_STATUSES,
    ReleaseDenialCode,
    ReleaseTransitionError,
)
from eawf.workflow.release.publication import burn_release
from tests._release_helpers import (
    DEV1_DRAFT_UID,
    adopted_observation,
    dev1_adoption,
    dev1_config,
    release_record,
)

#: The three legs the authored dev1 configuration declares.
CONFIGURED_TARGETS = ("pypi", "npm", "github")


def single_target_config() -> ReleaseConfig:
    """Return the dev1 configuration narrowed to its ``pypi`` leg."""
    full = dev1_config().model_dump(mode="json")
    return dev1_config(targets=[row for row in full["targets"] if row["target_id"] == "pypi"])


def all_success_adoption(targets: tuple[str, ...]) -> ReleaseAdoption:
    """Return an adoption that observed every one of *targets* matching."""
    return dev1_adoption(
        targets=targets,
        observations=tuple(
            adopted_observation(target, observed_status="observed_success") for target in targets
        ),
    )


def test_a_candidate_with_every_target_recorded_reaches_a_terminal_status() -> None:
    config = dev1_config()
    adopted = adopt_publication(
        release_record(), config, adoption=all_success_adoption(CONFIGURED_TARGETS)
    )
    burned, operation = burn_release(adopted, config, None)
    assert burned.status is ReleaseStatus.PARTIALLY_RELEASED
    assert burned.status in TERMINAL_RELEASE_STATUSES
    assert operation is None
    assert {
        target: burned.target_statuses[target] for target in CONFIGURED_TARGETS
    } == dict.fromkeys(CONFIGURED_TARGETS, ReleaseTargetStatus.OBSERVED_SUCCESS)
    assert burned.revision == release_record().revision + 2


def test_a_single_target_candidate_reaches_a_terminal_status() -> None:
    config = single_target_config()
    adopted = adopt_publication(release_record(), config, adoption=all_success_adoption(("pypi",)))
    burned, _operation = burn_release(adopted, config, None)
    assert burned.status is ReleaseStatus.PARTIALLY_RELEASED
    assert dict(burned.target_statuses) == {"pypi": ReleaseTargetStatus.OBSERVED_SUCCESS}


def test_the_burn_keeps_the_pinned_source_and_manifest() -> None:
    config = dev1_config()
    candidate = release_record()
    adopted = adopt_publication(
        candidate, config, adoption=all_success_adoption(CONFIGURED_TARGETS)
    )
    burned, _operation = burn_release(adopted, config, None)
    assert (burned.source_sha, burned.manifest_digest, burned.version) == (
        candidate.source_sha,
        candidate.manifest_digest,
        candidate.version,
    )


def test_a_candidate_with_no_recorded_target_is_refused_the_burn() -> None:
    with pytest.raises(ValueError, match="no publication operation and no adoption"):
        burn_release(release_record(), dev1_config(), None)


def test_an_adoption_recording_no_target_is_refused() -> None:
    with pytest.raises(ValidationError):
        dev1_adoption(targets=())


def test_a_candidate_missing_one_target_status_is_refused_the_adoption() -> None:
    with pytest.raises(ValueError, match=r"configured targets \['github'\]"):
        adopt_publication(
            release_record(), dev1_config(), adoption=all_success_adoption(("pypi", "npm"))
        )


def test_a_candidate_missing_one_target_status_is_refused_the_burn() -> None:
    adoption = all_success_adoption(("pypi", "npm"))
    short = release_record(
        adoption=adoption, target_statuses=dict(adoption.observed_target_statuses)
    )
    with pytest.raises(ReleaseTransitionError) as caught:
        burn_release(short, dev1_config(), None)
    assert caught.value.code is ReleaseDenialCode.PUBLICATION_NOT_OBSERVED


@pytest.mark.parametrize("status", ["published", "OBSERVED_SUCCESS", ""])
def test_an_unknown_target_status_is_refused(status: str) -> None:
    with pytest.raises(ValidationError):
        adopted_observation("pypi", observed_status=status)


@pytest.mark.parametrize("status", ["not_started", "reported_success"])
def test_a_reported_rather_than_observed_status_is_refused(status: str) -> None:
    with pytest.raises(ValidationError, match="independent read-back"):
        adopted_observation("pypi", observed_status=status)


def test_an_unknown_target_status_on_the_record_is_refused() -> None:
    with pytest.raises(ValidationError):
        release_record(target_statuses={"pypi": "published"})


def test_a_draft_record_takes_the_same_route() -> None:
    config = dev1_config()
    draft: Release = release_record(uid=DEV1_DRAFT_UID, status=ReleaseStatus.DRAFT)
    adopted = adopt_publication(draft, config, adoption=all_success_adoption(CONFIGURED_TARGETS))
    burned, _operation = burn_release(adopted, config, None)
    assert burned.status is ReleaseStatus.PARTIALLY_RELEASED
