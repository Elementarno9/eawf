"""REL-019: the four publication edges of the Release status machine.

Under test: ``APPROVED -> PUBLISHING`` denying ``approval_stale``,
``PUBLISHING -> VERIFYING`` denying ``target_results_incomplete``,
``RECOVERING -> PUBLISHING`` denying ``unsafe_release_retry`` without an
idempotency proof or budget, and ``RECOVERING -> PARTIALLY_RELEASED``
burning the version -- freezing the pinned facts and never returning to
DRAFT or CANCELLED.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import pytest

from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.kernel.spec.publication import (
    PublicationOperation,
    PublicationOperationKind,
    PublicationOperationStatus,
    attempt_count,
    require_attempt,
)
from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.kernel.spec.release_config import ReleaseConfig
from eawf.workflow.release.lifecycle import (
    RELEASE_TRANSITIONS,
    TERMINAL_RELEASE_STATUSES,
    ReleaseDenialCode,
    ReleaseTransitionError,
)
from eawf.workflow.release.publication import (
    begin_publication,
    begin_verification,
    burn_release,
    operation_reference,
    projected_target_statuses,
    recovery_exhausted,
    request_digest,
    retry_publication,
    retryable_targets,
    target_results_complete,
)
from eawf.workflow.release.target_machine import advance_target_attempt
from eawf.workflow.verify.release_readiness import ReleaseReadiness, compute_readiness
from tests.unit.kernel.release.conftest import (
    MANIFEST_DIGEST,
    NOW,
    all_passing,
    dev1_config,
    fixed_probe,
    release_record,
)

OPERATION_ID = UUID(int=27)
PROOF_DIGEST = f"sha256:{'1' * 64}"
IDEMPOTENCY_KEY = "publish-0.7.0.dev1-01"
EFFECT = "receipt://target/effect"


def readiness(*, ready: bool = True) -> ReleaseReadiness:
    """Return a green sweep, or one reddened on a required signal."""
    probes = all_passing()
    if not ready:
        probes[ReleaseSignalName.VERSION_CONSISTENCY] = fixed_probe(ReleaseSignalStatus.FAIL)
    sweep = compute_readiness(dev1_config(), probes=probes, computed_at=NOW)
    assert sweep.ready is ready
    return sweep


def approved(**overrides: object) -> Release:
    """Return an APPROVED ``0.7.0.dev1`` record."""
    return release_record(
        status=ReleaseStatus.APPROVED,
        approval_ref="receipt://approval/dev1",
        **overrides,
    )


def publish(
    release: Release | None = None,
    config: ReleaseConfig | None = None,
) -> tuple[Release, PublicationOperation]:
    """Open a publication episode against an approved record."""
    return begin_publication(
        release or approved(),
        config or dev1_config(),
        readiness(),
        operation_id=OPERATION_ID,
        approved_manifest_digest=MANIFEST_DIGEST,
        idempotency_key=IDEMPOTENCY_KEY,
        proof_digest=PROOF_DIGEST,
        opened_at=NOW,
    )


def settle(
    operation: PublicationOperation,
    config: ReleaseConfig,
    status: ReleaseTargetStatus,
    *,
    only: str | None = None,
) -> PublicationOperation:
    """Drive every leg (or just *only*) to *status* through in_flight.

    The clock comes from each attempt's own row rather than a fixed
    instant, so the helper stays correct across retry rounds whose
    attempts start later than the first.
    """
    for target in config.targets:
        if only is not None and target.target_id != only:
            continue
        row = require_attempt(operation, target.target_id)
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
        )
        operation = advance_target_attempt(
            operation,
            target=target,
            to=status,
            now=row.deadline_at,
            effect_receipt_ref=None if status is ReleaseTargetStatus.UNKNOWN else EFFECT,
        )
    return operation


# --- APPROVED -> PUBLISHING ----------------------------------------------


def test_publish_opens_one_queued_attempt_per_configured_target() -> None:
    config = dev1_config()
    published, operation = publish(config=config)
    assert published.status is ReleaseStatus.PUBLISHING
    assert set(operation.target_ids) == {target.target_id for target in config.targets}
    assert all(attempt_count(operation, t.target_id) == 1 for t in config.targets)
    assert operation.kind is PublicationOperationKind.PUBLISH
    assert operation.status is PublicationOperationStatus.OPEN


def test_publish_stamps_the_operation_reference_and_target_statuses() -> None:
    config = dev1_config()
    published, operation = publish(config=config)
    assert published.publication_operation_ref == operation_reference(operation)
    assert dict(published.target_statuses) == {
        target.target_id: ReleaseTargetStatus.QUEUED for target in config.targets
    }
    assert published.revision == approved().revision + 1


def test_publish_raises_approval_stale_when_the_sweep_reds() -> None:
    with pytest.raises(ReleaseTransitionError) as excinfo:
        begin_publication(
            approved(),
            dev1_config(),
            readiness(ready=False),
            operation_id=OPERATION_ID,
            approved_manifest_digest=MANIFEST_DIGEST,
            idempotency_key=IDEMPOTENCY_KEY,
            proof_digest=PROOF_DIGEST,
            opened_at=NOW,
        )
    assert excinfo.value.code is ReleaseDenialCode.APPROVAL_STALE


def test_publish_raises_approval_stale_when_the_manifest_was_repinned() -> None:
    with pytest.raises(ReleaseTransitionError) as excinfo:
        begin_publication(
            approved(),
            dev1_config(),
            readiness(),
            operation_id=OPERATION_ID,
            approved_manifest_digest=f"sha256:{'d' * 64}",
            idempotency_key=IDEMPOTENCY_KEY,
            proof_digest=PROOF_DIGEST,
            opened_at=NOW,
        )
    assert excinfo.value.code is ReleaseDenialCode.APPROVAL_STALE


def test_publish_from_a_candidate_is_an_illegal_transition() -> None:
    with pytest.raises(ReleaseTransitionError) as excinfo:
        publish(release=release_record(status=ReleaseStatus.CANDIDATE))
    assert excinfo.value.code is ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION


def test_publish_rejects_a_readiness_sweep_for_another_release() -> None:
    other = compute_readiness(
        dev1_config(version="0.7.0.dev1"), probes=all_passing(), computed_at=NOW
    )
    with pytest.raises(ValueError, match="cannot be applied to release"):
        begin_publication(
            approved(),
            dev1_config(),
            other.model_copy(update={"release_key": "REL-0.7.0.dev2"}),
            operation_id=OPERATION_ID,
            approved_manifest_digest=MANIFEST_DIGEST,
            idempotency_key=IDEMPOTENCY_KEY,
            proof_digest=PROOF_DIGEST,
            opened_at=NOW,
        )


def test_publish_rejects_a_naive_instant() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        begin_publication(
            approved(),
            dev1_config(),
            readiness(),
            operation_id=OPERATION_ID,
            approved_manifest_digest=MANIFEST_DIGEST,
            idempotency_key=IDEMPOTENCY_KEY,
            proof_digest=PROOF_DIGEST,
            opened_at=datetime(2026, 9, 4, 12, 0),
        )


def test_request_digest_is_stable_per_target_and_differs_across_targets() -> None:
    config = dev1_config()
    pypi, npm = config.targets[0], config.targets[1]
    assert request_digest(config, pypi, proof_digest=PROOF_DIGEST) == request_digest(
        config, pypi, proof_digest=PROOF_DIGEST
    )
    assert request_digest(config, pypi, proof_digest=PROOF_DIGEST) != request_digest(
        config, npm, proof_digest=PROOF_DIGEST
    )
    assert request_digest(config, pypi, proof_digest=PROOF_DIGEST) != request_digest(
        config, pypi, proof_digest=f"sha256:{'9' * 64}"
    )


# --- PUBLISHING -> VERIFYING ---------------------------------------------


def test_verifying_is_denied_while_a_leg_has_no_result() -> None:
    config = dev1_config()
    published, operation = publish(config=config)
    assert not target_results_complete(config, operation)
    with pytest.raises(ReleaseTransitionError) as excinfo:
        begin_verification(published, config, operation)
    assert excinfo.value.code is ReleaseDenialCode.TARGET_RESULTS_INCOMPLETE


def test_verifying_is_denied_when_only_some_legs_reported_success() -> None:
    config = dev1_config()
    published, operation = publish(config=config)
    operation = settle(operation, config, ReleaseTargetStatus.REPORTED_SUCCESS, only="pypi")
    with pytest.raises(ReleaseTransitionError) as excinfo:
        begin_verification(published, config, operation)
    assert excinfo.value.code is ReleaseDenialCode.TARGET_RESULTS_INCOMPLETE


def test_verifying_is_denied_when_a_leg_reported_failure() -> None:
    config = dev1_config()
    published, operation = publish(config=config)
    operation = settle(operation, config, ReleaseTargetStatus.REPORTED_SUCCESS, only="pypi")
    operation = settle(operation, config, ReleaseTargetStatus.REPORTED_SUCCESS, only="npm")
    operation = settle(operation, config, ReleaseTargetStatus.REPORTED_FAILURE, only="github")
    with pytest.raises(ReleaseTransitionError) as excinfo:
        begin_verification(published, config, operation)
    assert excinfo.value.code is ReleaseDenialCode.TARGET_RESULTS_INCOMPLETE


def test_verifying_is_admitted_once_every_leg_reported_success() -> None:
    config = dev1_config()
    published, operation = publish(config=config)
    operation = settle(operation, config, ReleaseTargetStatus.REPORTED_SUCCESS)
    assert target_results_complete(config, operation)
    verifying = begin_verification(published, config, operation)
    assert verifying.status is ReleaseStatus.VERIFYING
    assert dict(verifying.target_statuses) == dict(projected_target_statuses(config, operation))


# --- RECOVERING -> PUBLISHING --------------------------------------------


def recovering() -> tuple[Release, PublicationOperation, ReleaseConfig]:
    """Return a recovering record whose legs all reported failure."""
    config = dev1_config()
    published, operation = publish(config=config)
    operation = settle(operation, config, ReleaseTargetStatus.REPORTED_FAILURE)
    record = published.model_copy(
        update={"status": ReleaseStatus.RECOVERING, "revision": published.revision + 1}
    )
    return Release.model_validate(record.model_dump(mode="json")), operation, config


def test_retry_is_denied_without_the_original_idempotency_key() -> None:
    record, operation, config = recovering()
    with pytest.raises(ReleaseTransitionError) as excinfo:
        retry_publication(
            record,
            config,
            operation,
            idempotency_key="a-different-key-01",
            proof_digest=PROOF_DIGEST,
            now=NOW + timedelta(seconds=3600),
        )
    assert excinfo.value.code is ReleaseDenialCode.UNSAFE_RELEASE_RETRY


def test_retry_is_denied_when_the_proof_digest_changed() -> None:
    record, operation, config = recovering()
    with pytest.raises(ReleaseTransitionError) as excinfo:
        retry_publication(
            record,
            config,
            operation,
            idempotency_key=IDEMPOTENCY_KEY,
            proof_digest=f"sha256:{'e' * 64}",
            now=NOW + timedelta(seconds=3600),
        )
    assert excinfo.value.code is ReleaseDenialCode.UNSAFE_RELEASE_RETRY


def test_retry_is_denied_when_no_leg_has_budget_left() -> None:
    record, operation, config = recovering()
    for _round in range(2):
        record, operation = retry_publication(
            record,
            config,
            operation,
            idempotency_key=IDEMPOTENCY_KEY,
            proof_digest=PROOF_DIGEST,
            now=NOW + timedelta(seconds=3600),
        )
        operation = settle(operation, config, ReleaseTargetStatus.REPORTED_FAILURE)
        record = Release.model_validate(
            record.model_copy(
                update={"status": ReleaseStatus.RECOVERING, "revision": record.revision + 1}
            ).model_dump(mode="json")
        )
    assert recovery_exhausted(config, operation)
    with pytest.raises(ReleaseTransitionError) as excinfo:
        retry_publication(
            record,
            config,
            operation,
            idempotency_key=IDEMPOTENCY_KEY,
            proof_digest=PROOF_DIGEST,
            now=NOW + timedelta(seconds=7200),
        )
    assert excinfo.value.code is ReleaseDenialCode.UNSAFE_RELEASE_RETRY


def test_retry_requeues_every_retryable_leg_with_the_proof() -> None:
    record, operation, config = recovering()
    assert len(retryable_targets(config, operation)) == len(config.targets)
    republished, retried = retry_publication(
        record,
        config,
        operation,
        idempotency_key=IDEMPOTENCY_KEY,
        proof_digest=PROOF_DIGEST,
        now=NOW + timedelta(seconds=3600),
    )
    assert republished.status is ReleaseStatus.PUBLISHING
    assert retried.kind is PublicationOperationKind.RETRY_TARGET
    assert all(attempt_count(retried, t.target_id) == 2 for t in config.targets)


def test_retry_out_of_publish_timeout_carries_the_same_guard() -> None:
    config = dev1_config()
    published, operation = publish(config=config)
    operation = settle(operation, config, ReleaseTargetStatus.UNKNOWN)
    timed_out = Release.model_validate(
        published.model_copy(
            update={
                "status": ReleaseStatus.PUBLISH_TIMEOUT,
                "revision": published.revision + 1,
            }
        ).model_dump(mode="json")
    )
    with pytest.raises(ReleaseTransitionError) as excinfo:
        retry_publication(
            timed_out,
            config,
            operation,
            idempotency_key="a-different-key-01",
            proof_digest=PROOF_DIGEST,
            now=NOW + timedelta(seconds=3600),
        )
    assert excinfo.value.code is ReleaseDenialCode.UNSAFE_RELEASE_RETRY


def test_retry_rejects_a_naive_instant() -> None:
    record, operation, config = recovering()
    with pytest.raises(ValueError, match="timezone-aware"):
        retry_publication(
            record,
            config,
            operation,
            idempotency_key=IDEMPOTENCY_KEY,
            proof_digest=PROOF_DIGEST,
            now=datetime(2026, 9, 4, 12, 0),
        )


# --- RECOVERING -> PARTIALLY_RELEASED (the burn) -------------------------


def exhausted() -> tuple[Release, PublicationOperation, ReleaseConfig]:
    """Return a recovering record whose every leg has spent its budget."""
    record, operation, config = recovering()
    for _round in range(2):
        record, operation = retry_publication(
            record,
            config,
            operation,
            idempotency_key=IDEMPOTENCY_KEY,
            proof_digest=PROOF_DIGEST,
            now=NOW + timedelta(seconds=3600),
        )
        operation = settle(operation, config, ReleaseTargetStatus.REPORTED_FAILURE)
        record = Release.model_validate(
            record.model_copy(
                update={"status": ReleaseStatus.RECOVERING, "revision": record.revision + 1}
            ).model_dump(mode="json")
        )
    return record, operation, config


def test_burn_is_denied_while_recovery_budget_remains() -> None:
    record, operation, config = recovering()
    assert not recovery_exhausted(config, operation)
    with pytest.raises(ReleaseTransitionError) as excinfo:
        burn_release(record, config, operation)
    assert excinfo.value.code is ReleaseDenialCode.RECOVERY_BUDGET_AVAILABLE


def test_burn_freezes_the_source_and_artifact_digests() -> None:
    record, operation, config = exhausted()
    burned, abandoned = burn_release(record, config, operation)
    assert burned.status is ReleaseStatus.PARTIALLY_RELEASED
    assert burned.version == record.version
    assert burned.source_sha == record.source_sha
    assert burned.source_tree_sha == record.source_tree_sha
    assert burned.manifest_ref == record.manifest_ref
    assert burned.manifest_digest == record.manifest_digest
    assert abandoned.status is PublicationOperationStatus.ABANDONED
    assert abandoned.proof_digest == operation.proof_digest


def test_burned_version_never_returns_to_draft_or_cancelled() -> None:
    record, operation, config = exhausted()
    burned, _abandoned = burn_release(record, config, operation)
    assert RELEASE_TRANSITIONS[burned.status] == frozenset()
    assert burned.status in TERMINAL_RELEASE_STATUSES
    for forbidden in (ReleaseStatus.DRAFT, ReleaseStatus.CANCELLED):
        with pytest.raises(ReleaseTransitionError) as excinfo:
            burn_release(
                Release.model_validate(
                    burned.model_copy(update={"status": forbidden}).model_dump(mode="json")
                ),
                config,
                operation,
            )
        assert excinfo.value.code is ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION
