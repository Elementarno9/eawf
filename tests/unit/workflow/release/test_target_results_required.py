"""Only required targets gate the PUBLISHING -> VERIFYING edge.

Under test: :func:`target_results_complete` counting exactly the targets
:func:`required_targets_observed` counts at the bake, so an optional
target that was never published -- never attempted, or left queued --
cannot hold a record at PUBLISHING while a required one still can; the
two edges :func:`follow_guarded_edges` walks once a leg settles; and the
dispatch a publish job's receipt proves, which lets
:func:`reconcile_target` settle a leg nothing marked as dispatched.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from eawf.kernel.spec.publication import (
    PublicationOperation,
    PublicationOperationKind,
    require_attempt,
)
from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.kernel.spec.release_config import ReleaseConfig
from eawf.workflow.release.lifecycle import ReleaseDenialCode, ReleaseTransitionError
from eawf.workflow.release.observation import configured_target
from eawf.workflow.release.publication import (
    begin_publication,
    begin_verification,
    reconcile_target,
    request_digest,
    target_results_complete,
)
from eawf.workflow.release.settlement import follow_guarded_edges, required_targets_observed
from eawf.workflow.release.target_machine import (
    TargetDenialCode,
    TargetTransitionError,
    advance_target_attempt,
    current_target_status,
    open_target_attempt,
)
from eawf.workflow.verify.release_readiness import compute_readiness
from tests._release_helpers import MANIFEST_DIGEST, NOW, all_passing, dev1_config, release_record

pytestmark = pytest.mark.unit

OPERATION_ID = UUID(int=303)
PROOF_DIGEST = f"sha256:{'1' * 64}"
IDEMPOTENCY_KEY = "publish-0.7.0.dev1-303"
EFFECT = "receipt://target/effect"
READ_BACK = "observation://target/read-back"


def with_optional(*target_ids: str) -> ReleaseConfig:
    """Return the dev1 configuration with *target_ids* made optional."""
    targets = dev1_config().model_dump(mode="json")["targets"]
    for target in targets:
        if target["target_id"] in target_ids:
            target["required"] = False
    return dev1_config(targets=targets)


def published(config: ReleaseConfig) -> tuple[Release, PublicationOperation]:
    """Open a publication with every configured leg queued."""
    return begin_publication(
        release_record(status=ReleaseStatus.APPROVED, approval_ref="receipt://approval/dev1"),
        config,
        compute_readiness(config, probes=all_passing(), computed_at=NOW),
        operation_id=OPERATION_ID,
        approved_manifest_digest=MANIFEST_DIGEST,
        idempotency_key=IDEMPOTENCY_KEY,
        proof_digest=PROOF_DIGEST,
        opened_at=NOW,
    )


def attempted_only(config: ReleaseConfig, *target_ids: str) -> PublicationOperation:
    """Return an operation that queued *target_ids* and nothing else."""
    operation = PublicationOperation(
        operation_id=OPERATION_ID,
        release_ref="REL-0.7.0.dev1",
        kind=PublicationOperationKind.PUBLISH,
        proof_digest=PROOF_DIGEST,
        idempotency_key=IDEMPOTENCY_KEY,
        opened_at=NOW,
    )
    for target_id in target_ids:
        target = configured_target(config, target_id)
        operation = open_target_attempt(
            operation,
            target=target,
            now=NOW,
            request_digest=request_digest(config, target, proof_digest=PROOF_DIGEST),
        )
    return operation


def report(
    operation: PublicationOperation,
    config: ReleaseConfig,
    *target_ids: str,
    status: ReleaseTargetStatus = ReleaseTargetStatus.REPORTED_SUCCESS,
) -> PublicationOperation:
    """Dispatch each of *target_ids* and settle it on *status*."""
    for target_id in target_ids:
        target = configured_target(config, target_id)
        row = require_attempt(operation, target_id)
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
        )
        operation = advance_target_attempt(
            operation, target=target, to=status, now=row.deadline_at, effect_receipt_ref=EFFECT
        )
    return operation


def observe(
    operation: PublicationOperation, config: ReleaseConfig, *target_ids: str
) -> PublicationOperation:
    """Read each reported leg of *target_ids* back as a match."""
    for target_id in target_ids:
        operation = advance_target_attempt(
            operation,
            target=configured_target(config, target_id),
            to=ReleaseTargetStatus.OBSERVED_SUCCESS,
            now=require_attempt(operation, target_id).deadline_at,
            observation_receipt_ref=READ_BACK,
            observation_matched=True,
        )
    return operation


# --- target_results_complete ------------------------------------------------


def test_target_results_complete_ignores_an_optional_leg_never_attempted() -> None:
    config = with_optional("github")
    operation = report(attempted_only(config, "pypi", "npm"), config, "pypi", "npm")
    assert current_target_status(operation, "github") is ReleaseTargetStatus.NOT_STARTED
    assert target_results_complete(config, operation)


def test_target_results_complete_ignores_an_optional_leg_left_queued() -> None:
    config = with_optional("github")
    _release, operation = published(config)
    operation = report(operation, config, "pypi", "npm")
    assert current_target_status(operation, "github") is ReleaseTargetStatus.QUEUED
    assert target_results_complete(config, operation)


def test_target_results_complete_ignores_an_optional_leg_that_reported_failure() -> None:
    config = with_optional("github")
    _release, operation = published(config)
    operation = report(operation, config, "pypi", "npm")
    operation = report(operation, config, "github", status=ReleaseTargetStatus.REPORTED_FAILURE)
    assert target_results_complete(config, operation)


def test_target_results_complete_is_false_while_a_required_leg_is_queued() -> None:
    config = with_optional("github")
    _release, operation = published(config)
    operation = report(operation, config, "pypi")
    assert not target_results_complete(config, operation)


def test_target_results_complete_is_false_for_a_required_leg_never_attempted() -> None:
    config = dev1_config()
    operation = report(attempted_only(config, "pypi", "npm"), config, "pypi", "npm")
    assert current_target_status(operation, "github") is ReleaseTargetStatus.NOT_STARTED
    assert not target_results_complete(config, operation)


def test_target_results_complete_is_false_for_a_required_leg_that_reported_failure() -> None:
    config = dev1_config()
    _release, operation = published(config)
    operation = report(operation, config, "pypi", "npm")
    operation = report(operation, config, "github", status=ReleaseTargetStatus.REPORTED_FAILURE)
    assert not target_results_complete(config, operation)


def test_target_results_complete_counts_an_observed_success() -> None:
    config = dev1_config()
    _release, operation = published(config)
    operation = observe(report(operation, config, "pypi", "npm", "github"), config, "pypi")
    assert target_results_complete(config, operation)


def test_target_results_complete_is_vacuous_when_no_target_is_required() -> None:
    config = with_optional("pypi", "npm", "github")
    assert config.required_target_ids == ()
    assert target_results_complete(config, attempted_only(config))


def test_target_results_complete_counts_the_targets_the_bake_counts() -> None:
    config = with_optional("github")
    _release, operation = published(config)
    operation = observe(report(operation, config, "pypi", "npm"), config, "pypi", "npm")
    assert target_results_complete(config, operation)
    assert required_targets_observed(config, operation)


# --- begin_verification -----------------------------------------------------


def test_begin_verification_admits_an_unpublished_optional_leg() -> None:
    config = with_optional("github")
    release, operation = published(config)
    operation = report(operation, config, "pypi", "npm")
    verifying = begin_verification(release, config, operation)
    assert verifying.status is ReleaseStatus.VERIFYING
    assert verifying.target_statuses["github"] is ReleaseTargetStatus.QUEUED


def test_begin_verification_denies_an_unpublished_required_leg() -> None:
    config = dev1_config()
    release, operation = published(config)
    operation = report(operation, config, "pypi", "npm")
    with pytest.raises(ReleaseTransitionError) as excinfo:
        begin_verification(release, config, operation)
    assert excinfo.value.code is ReleaseDenialCode.TARGET_RESULTS_INCOMPLETE


# --- follow_guarded_edges ---------------------------------------------------


def test_follow_guarded_edges_verifies_without_the_optional_leg() -> None:
    config = with_optional("github")
    release, operation = published(config)
    operation = report(operation, config, "pypi", "npm")
    walked = follow_guarded_edges(release, config, operation)
    assert [record.status for record in walked] == [ReleaseStatus.VERIFYING]
    assert walked[0].revision == release.revision + 1


def test_follow_guarded_edges_verifies_and_bakes_once_required_legs_are_observed() -> None:
    config = with_optional("github")
    release, operation = published(config)
    operation = observe(report(operation, config, "pypi", "npm"), config, "pypi", "npm")
    walked = follow_guarded_edges(release, config, operation)
    assert [record.status for record in walked] == [ReleaseStatus.VERIFYING, ReleaseStatus.BAKED]
    assert [record.revision for record in walked] == [release.revision + 1, release.revision + 2]


def test_follow_guarded_edges_bakes_a_verifying_record() -> None:
    config = dev1_config()
    release, operation = published(config)
    operation = report(operation, config, "pypi", "npm", "github")
    verifying = begin_verification(release, config, operation)
    operation = observe(operation, config, "pypi", "npm", "github")
    walked = follow_guarded_edges(verifying, config, operation)
    assert [record.status for record in walked] == [ReleaseStatus.BAKED]


def test_follow_guarded_edges_walks_nowhere_while_a_required_leg_is_queued() -> None:
    config = dev1_config()
    release, operation = published(config)
    operation = report(operation, config, "pypi", "npm")
    assert follow_guarded_edges(release, config, operation) == ()


def test_follow_guarded_edges_walks_nowhere_from_a_status_without_those_edges() -> None:
    config = dev1_config()
    release, operation = published(config)
    operation = observe(report(operation, config, "pypi", "npm", "github"), config, "pypi")
    recovering = release.model_copy(update={"status": ReleaseStatus.RECOVERING})
    assert follow_guarded_edges(recovering, config, operation) == ()


# --- reconcile_target and the proven dispatch -------------------------------


def test_reconcile_target_settles_a_queued_leg_when_dispatch_is_proven() -> None:
    config = dev1_config()
    release, operation = published(config)
    reconciled, settled = reconcile_target(
        release,
        config,
        operation,
        target_id="pypi",
        status=ReleaseTargetStatus.REPORTED_SUCCESS,
        effect_receipt_ref=EFFECT,
        now=NOW,
        dispatch_proven=True,
    )
    assert require_attempt(settled, "pypi").status is ReleaseTargetStatus.REPORTED_SUCCESS
    assert settled.revision == operation.revision + 2
    assert reconciled.status is ReleaseStatus.PUBLISHING
    assert reconciled.target_statuses["pypi"] is ReleaseTargetStatus.REPORTED_SUCCESS


def test_reconcile_target_refuses_a_queued_leg_without_proof() -> None:
    config = dev1_config()
    release, operation = published(config)
    with pytest.raises(TargetTransitionError) as excinfo:
        reconcile_target(
            release,
            config,
            operation,
            target_id="pypi",
            status=ReleaseTargetStatus.REPORTED_SUCCESS,
            effect_receipt_ref=EFFECT,
            now=NOW,
        )
    assert excinfo.value.code is TargetDenialCode.ILLEGAL_TARGET_TRANSITION


def test_reconcile_target_needs_no_second_dispatch_for_a_leg_in_flight() -> None:
    config = dev1_config()
    release, operation = published(config)
    row = require_attempt(operation, "pypi")
    in_flight = advance_target_attempt(
        operation,
        target=configured_target(config, "pypi"),
        to=ReleaseTargetStatus.IN_FLIGHT,
        now=row.started_at,
    )
    _reconciled, settled = reconcile_target(
        release,
        config,
        in_flight,
        target_id="pypi",
        status=ReleaseTargetStatus.REPORTED_SUCCESS,
        effect_receipt_ref=EFFECT,
        now=NOW,
        dispatch_proven=True,
    )
    assert settled.revision == in_flight.revision + 1


def test_reconcile_target_refuses_a_proven_dispatch_of_a_settled_leg() -> None:
    config = dev1_config()
    release, operation = published(config)
    operation = report(operation, config, "pypi")
    with pytest.raises(TargetTransitionError) as excinfo:
        reconcile_target(
            release,
            config,
            operation,
            target_id="pypi",
            status=ReleaseTargetStatus.REPORTED_SUCCESS,
            effect_receipt_ref=EFFECT,
            now=NOW,
            dispatch_proven=True,
        )
    assert excinfo.value.code is TargetDenialCode.ILLEGAL_TARGET_TRANSITION
