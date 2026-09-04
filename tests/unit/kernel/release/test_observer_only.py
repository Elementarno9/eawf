"""Only the observation verb writes an observed target status.

Under test: ``observed_success`` and ``observed_mismatch`` reachable
only through :func:`observe_target`, and only from a
:class:`PublicationObservation` whose evidence reference lands on the
row; the publish, verify and reconcile paths capped at
``reported_success`` / ``reported_failure`` / ``unknown``; and three
reported successes failing to derive BAKED -- ``VERIFYING -> BAKED``
denies ``publication_not_observed`` until every required target has been
independently read back.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import pytest

from eawf.kernel.spec.publication import (
    OBSERVED_TARGET_STATUSES,
    SETTLED_TARGET_STATUSES,
    PublicationOperation,
    require_attempt,
)
from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseStatus,
    ReleaseTargetStatus,
)
from eawf.kernel.spec.release_config import ReleaseConfig
from eawf.workflow.release.adapters import observe_publication
from eawf.workflow.release.lifecycle import ReleaseDenialCode, ReleaseTransitionError
from eawf.workflow.release.observation import PublicationObservation
from eawf.workflow.release.observe import (
    OBSERVED_STATUS_FOR_RESULT,
    InconclusiveObservationError,
    bake_release,
    observe_target,
    required_targets_observed,
    route_after_observation,
)
from eawf.workflow.release.publication import (
    RECONCILABLE_TARGET_STATUSES,
    ObserverOnlyStatusError,
    begin_publication,
    begin_verification,
    reconcile_target,
)
from eawf.workflow.release.target_machine import (
    TargetTransitionError,
    advance_target_attempt,
)
from eawf.workflow.verify.release_readiness import ReleaseReadiness, compute_readiness
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

OPERATION_ID = UUID(int=28)
PROOF_DIGEST = f"sha256:{'1' * 64}"
IDEMPOTENCY_KEY = "publish-0.7.0.dev1-28"
EFFECT = "receipt://target/effect"
TARGET_IDS = ("pypi", "npm", "github")


def readiness() -> ReleaseReadiness:
    """Return a green sweep for the dev1 checkpoint."""
    return compute_readiness(dev1_config(), probes=all_passing(), computed_at=NOW)


def publish(config: ReleaseConfig) -> tuple[Release, PublicationOperation]:
    """Open a publication episode against an approved dev1 record."""
    return begin_publication(
        release_record(status=ReleaseStatus.APPROVED, approval_ref="receipt://approval/dev1"),
        config,
        readiness(),
        operation_id=OPERATION_ID,
        approved_manifest_digest=MANIFEST_DIGEST,
        idempotency_key=IDEMPOTENCY_KEY,
        proof_digest=PROOF_DIGEST,
        opened_at=NOW,
    )


def dispatch(
    operation: PublicationOperation,
    config: ReleaseConfig,
    *,
    only: str | None = None,
) -> PublicationOperation:
    """Move every leg (or just *only*) from queued to in_flight."""
    for target in config.targets:
        if only is not None and target.target_id != only:
            continue
        row = require_attempt(operation, target.target_id)
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
        )
    return operation


def settle(
    operation: PublicationOperation,
    config: ReleaseConfig,
    status: ReleaseTargetStatus,
    *,
    only: str | None = None,
) -> PublicationOperation:
    """Drive every leg (or just *only*) to *status* through in_flight."""
    operation = dispatch(operation, config, only=only)
    for target in config.targets:
        if only is not None and target.target_id != only:
            continue
        row = require_attempt(operation, target.target_id)
        operation = advance_target_attempt(
            operation,
            target=target,
            to=status,
            now=row.deadline_at,
            effect_receipt_ref=None if status is ReleaseTargetStatus.UNKNOWN else EFFECT,
        )
    return operation


def observation(target_id: str, case: str) -> PublicationObservation:
    """Return the observation the recorded *case* supports for *target_id*."""
    return observe_publication(
        read_back_request(target_id), response=recorded_response(target_id, case), observed_at=NOW
    )


def verifying() -> tuple[Release, PublicationOperation, ReleaseConfig]:
    """Return a record at VERIFYING with every leg at reported_success."""
    config = dev1_config()
    published, operation = publish(config)
    operation = settle(operation, config, ReleaseTargetStatus.REPORTED_SUCCESS)
    return begin_verification(published, config, operation), operation, config


# --- the observed statuses have exactly one writer ------------------------


def test_the_observed_statuses_are_the_settled_ones_reconcile_cannot_write() -> None:
    assert OBSERVED_TARGET_STATUSES == SETTLED_TARGET_STATUSES - RECONCILABLE_TARGET_STATUSES


@pytest.mark.parametrize("status", sorted(OBSERVED_TARGET_STATUSES, key=lambda s: s.value))
def test_reconcile_refuses_to_write_an_observed_status(status: ReleaseTargetStatus) -> None:
    release, operation, config = verifying()
    with pytest.raises(ObserverOnlyStatusError) as excinfo:
        reconcile_target(release, config, operation, target_id="pypi", status=status, now=NOW)
    assert excinfo.value.status is status
    assert "observer_only_status" in str(excinfo.value)


@pytest.mark.parametrize("status", sorted(RECONCILABLE_TARGET_STATUSES, key=lambda s: s.value))
def test_reconcile_writes_every_reported_status(status: ReleaseTargetStatus) -> None:
    config = dev1_config()
    published, operation = publish(config)
    operation = dispatch(operation, config, only="pypi")
    reconciled, settled = reconcile_target(
        published,
        config,
        operation,
        target_id="pypi",
        status=status,
        effect_receipt_ref=None if status is ReleaseTargetStatus.UNKNOWN else EFFECT,
        now=require_attempt(operation, "pypi").deadline_at,
    )
    row = require_attempt(settled, "pypi")
    assert row.status is status
    assert row.observation_receipt_ref is None
    assert reconciled.status is ReleaseStatus.PUBLISHING


def test_publish_and_verify_leave_every_leg_unobserved() -> None:
    release, operation, _config = verifying()
    assert release.status is ReleaseStatus.VERIFYING
    assert all(
        require_attempt(operation, target_id).status is ReleaseTargetStatus.REPORTED_SUCCESS
        for target_id in TARGET_IDS
    )
    assert all(
        require_attempt(operation, target_id).observation_receipt_ref is None
        for target_id in TARGET_IDS
    )


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_observe_writes_the_observation_receipt_onto_the_row(target_id: str) -> None:
    release, operation, config = verifying()
    read_back = observation(target_id, "match")
    _observed, settled = observe_target(release, config, operation, observation=read_back, now=NOW)
    row = require_attempt(settled, target_id)
    assert row.status is ReleaseTargetStatus.OBSERVED_SUCCESS
    assert row.observation_receipt_ref == read_back.evidence_ref
    assert row.effect_receipt_ref == EFFECT


def test_an_inconclusive_read_back_writes_no_status() -> None:
    release, operation, config = verifying()
    with pytest.raises(InconclusiveObservationError) as excinfo:
        observe_target(
            release, config, operation, observation=observation("pypi", "unknown"), now=NOW
        )
    assert excinfo.value.target_id == "pypi"
    assert "observation_inconclusive" in str(excinfo.value)


def test_the_result_to_status_map_has_no_entry_for_unknown() -> None:
    assert set(OBSERVED_STATUS_FOR_RESULT.values()) == OBSERVED_TARGET_STATUSES
    assert "unknown" not in {result.value for result in OBSERVED_STATUS_FOR_RESULT}


def test_observing_a_leg_that_never_reported_is_refused() -> None:
    config = dev1_config()
    published, operation = publish(config)
    with pytest.raises(TargetTransitionError):
        observe_target(
            published, config, operation, observation=observation("pypi", "match"), now=NOW
        )


def test_observing_an_unconfigured_target_raises() -> None:
    release, operation, config = verifying()
    stray = observation("pypi", "match").model_copy(update={"target_id": "crates"})
    with pytest.raises(KeyError, match="configures no target"):
        observe_target(release, config, operation, observation=stray, now=NOW)


def test_a_naive_observation_instant_is_refused() -> None:
    release, operation, config = verifying()
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        observe_target(
            release,
            config,
            operation,
            observation=observation("pypi", "match"),
            now=datetime(2026, 9, 4, 12, 0),
        )


# --- three reported successes never derive BAKED --------------------------


def test_three_reported_successes_do_not_observe_the_required_set() -> None:
    _release, operation, config = verifying()
    assert required_targets_observed(config, operation) is False


def test_baking_on_three_reported_successes_denies_publication_not_observed() -> None:
    release, operation, config = verifying()
    with pytest.raises(ReleaseTransitionError) as excinfo:
        bake_release(release, config, operation)
    assert excinfo.value.code is ReleaseDenialCode.PUBLICATION_NOT_OBSERVED


@pytest.mark.parametrize("observed_count", (0, 1, 2))
def test_baking_before_every_required_target_is_observed_is_denied(observed_count: int) -> None:
    release, operation, config = verifying()
    for target_id in TARGET_IDS[:observed_count]:
        release, operation = observe_target(
            release, config, operation, observation=observation(target_id, "match"), now=NOW
        )
    assert release.status is ReleaseStatus.VERIFYING
    with pytest.raises(ReleaseTransitionError) as excinfo:
        bake_release(release, config, operation)
    assert excinfo.value.code is ReleaseDenialCode.PUBLICATION_NOT_OBSERVED


def test_the_last_matched_observation_bakes_the_prerelease() -> None:
    release, operation, config = verifying()
    for target_id in TARGET_IDS:
        release, operation = observe_target(
            release, config, operation, observation=observation(target_id, "match"), now=NOW
        )
    assert required_targets_observed(config, operation) is True
    assert release.status is ReleaseStatus.BAKED
    assert dict(release.target_statuses) == dict.fromkeys(
        TARGET_IDS, ReleaseTargetStatus.OBSERVED_SUCCESS
    )


def test_a_baked_record_keeps_its_pinned_source_and_manifest() -> None:
    release, operation, config = verifying()
    for target_id in TARGET_IDS:
        release, operation = observe_target(
            release, config, operation, observation=observation(target_id, "match"), now=NOW
        )
    original = release_record()
    assert release.source_sha == original.source_sha
    assert release.source_tree_sha == original.source_tree_sha
    assert release.manifest_digest == original.manifest_digest


def test_baking_a_stable_version_reaches_released_not_baked() -> None:
    release, operation, config = verifying()
    for target_id in TARGET_IDS:
        release, operation = observe_target(
            release, config, operation, observation=observation(target_id, "match"), now=NOW
        )
    stable = release.model_copy(
        update={
            "key": "REL-0.7.0",
            "version": "0.7.0",
            "channel": ReleaseChannel.STABLE,
            "status": ReleaseStatus.VERIFYING,
        }
    )
    assert bake_release(stable, config, operation).status is ReleaseStatus.RELEASED


def test_baking_from_a_publishing_record_is_an_illegal_transition() -> None:
    config = dev1_config()
    published, operation = publish(config)
    with pytest.raises(ReleaseTransitionError) as excinfo:
        bake_release(published, config, operation)
    assert excinfo.value.code is ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION


def test_an_optional_leg_left_unobserved_does_not_hold_the_bake() -> None:
    config = dev1_config()
    body = config.model_dump(mode="json")
    body["targets"][2]["required"] = False
    optional_github = dev1_config(targets=body["targets"])
    published, operation = publish(optional_github)
    operation = settle(operation, optional_github, ReleaseTargetStatus.REPORTED_SUCCESS)
    release = begin_verification(published, optional_github, operation)
    for target_id in ("pypi", "npm"):
        release, operation = observe_target(
            release,
            optional_github,
            operation,
            observation=observation(target_id, "match"),
            now=NOW,
        )
    assert release.status is ReleaseStatus.BAKED


def test_an_observation_that_only_re_projects_still_advances_the_revision() -> None:
    release, operation, config = verifying()
    before = release.revision
    release, operation = observe_target(
        release, config, operation, observation=observation("pypi", "match"), now=NOW
    )
    assert release.status is ReleaseStatus.VERIFYING
    assert release.revision == before + 1
    assert release.target_statuses["pypi"] is ReleaseTargetStatus.OBSERVED_SUCCESS


def test_routing_a_matched_read_back_outside_verifying_leaves_the_status() -> None:
    config = dev1_config()
    published, operation = publish(config)
    operation = settle(operation, config, ReleaseTargetStatus.REPORTED_SUCCESS)
    routed = route_after_observation(
        published, config, operation, observation=observation("pypi", "match")
    )
    assert routed.status is ReleaseStatus.PUBLISHING
    assert routed.revision == published.revision + 1


def test_observing_a_timed_out_leg_without_an_effect_receipt_is_refused() -> None:
    config = dev1_config()
    published, operation = publish(config)
    operation = settle(operation, config, ReleaseTargetStatus.UNKNOWN, only="pypi")
    with pytest.raises(ValueError, match="requires effect_receipt_ref"):
        observe_target(
            published, config, operation, observation=observation("pypi", "match"), now=NOW
        )


def test_observing_a_timed_out_leg_carries_the_recovered_effect_receipt() -> None:
    config = dev1_config()
    published, operation = publish(config)
    operation = settle(operation, config, ReleaseTargetStatus.UNKNOWN, only="pypi")
    _routed, settled = observe_target(
        published,
        config,
        operation,
        observation=observation("pypi", "match"),
        now=NOW,
        effect_receipt_ref=EFFECT,
    )
    row = require_attempt(settled, "pypi")
    assert row.status is ReleaseTargetStatus.OBSERVED_SUCCESS
    assert row.effect_receipt_ref == EFFECT
