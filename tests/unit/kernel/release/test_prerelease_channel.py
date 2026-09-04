"""REL-016: a prerelease on the default channel is a mismatch.

Under test: the two ways a checkpoint can be correctly built and wrongly
exposed -- the npm ``latest`` tag resolving the prerelease, and a
source-host release object not flagged prerelease -- each reaching
``observed_mismatch`` under a stable code *while every digest matches*,
and each routing the Release into ``RECOVERING``. Alongside them the
republication case: an already-published version whose digests differ
from the frozen manifest is a mismatch, while an identical
already-existing artifact is an ordinary ``observed_success``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest

from eawf.kernel.spec.publication import PublicationOperation, require_attempt
from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.kernel.spec.release_config import DEFAULT_DIST_TAG, ReleaseConfig
from eawf.workflow.release.adapters import observe_publication
from eawf.workflow.release.lifecycle import ReleaseDenialCode, ReleaseTransitionError
from eawf.workflow.release.observation import (
    ObservationCode,
    ObservationResult,
    PublicationObservation,
    RecordedResponse,
)
from eawf.workflow.release.observe import bake_release, observe_target
from eawf.workflow.release.publication import begin_publication, begin_verification
from eawf.workflow.release.target_machine import advance_target_attempt
from eawf.workflow.verify.release_readiness import compute_readiness
from tests.unit.kernel.release.conftest import (
    MANIFEST_DIGEST,
    NOW,
    all_passing,
    dev1_config,
    read_back_request,
    recorded_response,
    release_record,
)

pytestmark = pytest.mark.unit

OPERATION_ID = UUID(int=16)
PROOF_DIGEST = f"sha256:{'1' * 64}"
IDEMPOTENCY_KEY = "publish-0.7.0.dev1-16"
EFFECT = "receipt://target/effect"

#: The two legs whose registries have a stable default channel. A
#: package index has none -- an installer resolves a prerelease only when
#: asked -- so it is deliberately absent rather than tested vacuously.
DEFAULT_CHANNEL_TARGETS = ("npm", "github")


def observation(target_id: str, case: str) -> PublicationObservation:
    """Return the observation the recorded *case* supports for *target_id*."""
    return observe_publication(
        read_back_request(target_id), response=recorded_response(target_id, case), observed_at=NOW
    )


def verifying() -> tuple[Release, PublicationOperation, ReleaseConfig]:
    """Return a record at VERIFYING with every leg at reported_success."""
    config = dev1_config()
    published, operation = begin_publication(
        release_record(status=ReleaseStatus.APPROVED, approval_ref="receipt://approval/dev1"),
        config,
        compute_readiness(config, probes=all_passing(), computed_at=NOW),
        operation_id=OPERATION_ID,
        approved_manifest_digest=MANIFEST_DIGEST,
        idempotency_key=IDEMPOTENCY_KEY,
        proof_digest=PROOF_DIGEST,
        opened_at=NOW,
    )
    for target in config.targets:
        row = require_attempt(operation, target.target_id)
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
        )
        operation = advance_target_attempt(
            operation,
            target=target,
            to=ReleaseTargetStatus.REPORTED_SUCCESS,
            now=row.deadline_at,
            effect_receipt_ref=EFFECT,
        )
    return begin_verification(published, config, operation), operation, config


# --- a prerelease exposed on the stable default channel -------------------


@pytest.mark.parametrize("target_id", DEFAULT_CHANNEL_TARGETS)
def test_a_prerelease_on_the_default_channel_is_a_mismatch(target_id: str) -> None:
    read_back = observation(target_id, "default-channel")
    assert read_back.result is ObservationResult.MISMATCH
    assert read_back.code is ObservationCode.PRERELEASE_ON_DEFAULT_CHANNEL


@pytest.mark.parametrize("target_id", DEFAULT_CHANNEL_TARGETS)
def test_the_default_channel_mismatch_stands_even_with_matching_digests(target_id: str) -> None:
    exposed = observation(target_id, "default-channel")
    matched = observation(target_id, "match")
    assert dict(exposed.observed_digests) == dict(matched.observed_digests)
    assert matched.code is ObservationCode.MATCHED
    assert exposed.code is ObservationCode.PRERELEASE_ON_DEFAULT_CHANNEL


def test_the_npm_default_channel_mismatch_names_the_tag_it_belongs_on() -> None:
    read_back = observation("npm", "default-channel")
    assert DEFAULT_DIST_TAG in read_back.detail
    assert "next" in read_back.detail


def test_the_source_host_mismatch_names_the_missing_prerelease_flag() -> None:
    read_back = observation("github", "default-channel")
    assert "not flagged prerelease" in read_back.detail


@pytest.mark.parametrize("target_id", DEFAULT_CHANNEL_TARGETS)
def test_a_default_channel_exposure_routes_the_release_to_recovering(target_id: str) -> None:
    release, operation, config = verifying()
    recovering, settled = observe_target(
        release, config, operation, observation=observation(target_id, "default-channel"), now=NOW
    )
    assert recovering.status is ReleaseStatus.RECOVERING
    assert require_attempt(settled, target_id).status is ReleaseTargetStatus.OBSERVED_MISMATCH
    assert recovering.target_statuses[target_id] is ReleaseTargetStatus.OBSERVED_MISMATCH


@pytest.mark.parametrize("target_id", DEFAULT_CHANNEL_TARGETS)
def test_a_default_channel_exposure_cannot_bake(target_id: str) -> None:
    release, operation, config = verifying()
    _recovering, settled = observe_target(
        release, config, operation, observation=observation(target_id, "default-channel"), now=NOW
    )
    with pytest.raises(ReleaseTransitionError) as excinfo:
        bake_release(release, config, settled)
    assert excinfo.value.code is ReleaseDenialCode.PUBLICATION_NOT_OBSERVED


def test_a_stable_version_on_the_default_channel_is_not_a_mismatch() -> None:
    payload = json.loads(json.dumps(recorded_response("npm", "match").payload))
    payload["dist-tags"][DEFAULT_DIST_TAG] = "0.7.0"
    payload["versions"] = {"0.7.0": payload["versions"]["0.7.0-dev.1"]}
    read_back = observe_publication(
        replace(read_back_request("npm"), version="0.7.0"),
        response=RecordedResponse(status=200, payload=payload),
        observed_at=NOW,
    )
    assert read_back.code is ObservationCode.MATCHED


# --- republication of an already-published version ------------------------


@pytest.mark.parametrize("target_id", ("pypi", "npm", "github"))
def test_a_republication_with_different_digests_is_a_mismatch(target_id: str) -> None:
    read_back = observation(target_id, "mismatch")
    assert read_back.result is ObservationResult.MISMATCH
    assert read_back.code is ObservationCode.DIGEST_MISMATCH


@pytest.mark.parametrize("target_id", ("pypi", "npm", "github"))
def test_a_republication_with_different_digests_routes_to_recovering(target_id: str) -> None:
    release, operation, config = verifying()
    recovering, settled = observe_target(
        release, config, operation, observation=observation(target_id, "mismatch"), now=NOW
    )
    assert recovering.status is ReleaseStatus.RECOVERING
    assert require_attempt(settled, target_id).status is ReleaseTargetStatus.OBSERVED_MISMATCH


@pytest.mark.parametrize("target_id", ("pypi", "npm", "github"))
def test_an_identical_already_existing_artifact_is_observed_success(target_id: str) -> None:
    release, operation, config = verifying()
    observed, settled = observe_target(
        release, config, operation, observation=observation(target_id, "match"), now=NOW
    )
    assert require_attempt(settled, target_id).status is ReleaseTargetStatus.OBSERVED_SUCCESS
    assert observed.status is ReleaseStatus.VERIFYING


@pytest.mark.parametrize("target_id", ("pypi", "npm", "github"))
def test_a_version_that_vanished_after_reporting_success_routes_to_recovering(
    target_id: str,
) -> None:
    release, operation, config = verifying()
    recovering, settled = observe_target(
        release, config, operation, observation=observation(target_id, "missing"), now=NOW
    )
    assert recovering.status is ReleaseStatus.RECOVERING
    assert require_attempt(settled, target_id).status is ReleaseTargetStatus.OBSERVED_MISMATCH


def test_recovering_is_reached_once_and_a_second_mismatch_is_illegal() -> None:
    release, operation, config = verifying()
    recovering, settled = observe_target(
        release, config, operation, observation=observation("npm", "mismatch"), now=NOW
    )
    with pytest.raises(ReleaseTransitionError) as excinfo:
        observe_target(
            recovering, config, settled, observation=observation("pypi", "mismatch"), now=NOW
        )
    assert excinfo.value.code is ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION


def test_every_mismatch_code_carries_a_stable_value() -> None:
    codes = {
        observation(target_id, case).code.value
        for target_id, case in (
            ("npm", "default-channel"),
            ("github", "default-channel"),
            ("pypi", "mismatch"),
            ("pypi", "missing"),
        )
    }
    assert codes == {"prerelease_on_default_channel", "digest_mismatch", "version_absent"}
