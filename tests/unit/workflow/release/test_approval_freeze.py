"""DEL-017, REL-014 and PLAN-006: what a release approval freezes and what voids it.

DEL-017: an approval copies out the exact inputs it accepted --
membership, source and tag, the manifest (which covers artifacts and
claims), targets, policy and the proof digest -- into a frozen record on
the release.

REL-014: any change to one of those inputs before external effect voids
the approval, returns the release to DRAFT, and records the typed cause.

PLAN-006: an open verification debt blocks stable release approval.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.kernel.spec.release import (
    Release,
    ReleaseApprovalFreeze,
    ReleaseChannel,
    ReleaseInvalidationCause,
    ReleaseStatus,
)
from eawf.kernel.state.epoch2.regime import GateClass, VerificationDebt
from eawf.workflow.release.lifecycle import ReleaseDenialCode, ReleaseTransitionError
from eawf.workflow.release.preflight import (
    approval_drift,
    approval_inputs,
    approve_release,
    invalidate_changed_approval,
)
from eawf.workflow.verify.release_readiness import ReleaseReadiness, compute_readiness
from tests._release_helpers import (
    MANIFEST_DIGEST,
    NOW,
    SOURCE_SHA,
    TREE_SHA,
    all_passing,
    dev1_config,
    fixed_probe,
    release_record,
)

APPROVAL_REF = "receipt://approval/0135"
OTHER_DIGEST = f"sha256:{'d' * 64}"
MILESTONE = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"
EVIDENCE = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"
TARGETS = tuple(sorted(target.target_id for target in dev1_config().targets))


def _sweep(key: str = "REL-0.7.0.dev1") -> ReleaseReadiness:
    sweep = compute_readiness(dev1_config(), probes=all_passing(), computed_at=NOW)
    return sweep.model_copy(update={"release_key": key})


def _approve(candidate: Release | None = None, **kwargs: Any) -> Release:
    record = candidate if candidate is not None else release_record()
    options: dict[str, Any] = {"proof_digest": MANIFEST_DIGEST, "verification_debts": ()}
    options.update(kwargs)
    return approve_release(
        record,
        _sweep(record.key),
        dev1_config(),
        approval_ref=APPROVAL_REF,
        approved_at=NOW,
        **options,
    )


def _freeze(**overrides: Any) -> ReleaseApprovalFreeze:
    fields: dict[str, Any] = {
        "membership_refs": (),
        "source_sha": SOURCE_SHA,
        "source_tree_sha": TREE_SHA,
        "tag": "v0.7.0.dev1",
        "manifest_ref": "artifact://release/manifest",
        "manifest_digest": MANIFEST_DIGEST,
        "target_ids": TARGETS,
        "policy_revision": 1,
        "proof_digest": MANIFEST_DIGEST,
    }
    fields.update(overrides)
    return ReleaseApprovalFreeze.model_validate(fields)


def _debt(**overrides: Any) -> VerificationDebt:
    fields: dict[str, Any] = {
        "key": "VDT-0001",
        "scope_ref": MILESTONE,
        "incident_ref": "INC-001",
        "deferred_gate": GateClass.REVIEW,
        "opened_at": NOW,
    }
    fields.update(overrides)
    return VerificationDebt.model_validate(fields)


def _stable_candidate() -> Release:
    return release_record(
        key="REL-0.7.0", version="0.7.0", channel=ReleaseChannel.STABLE, authority_epoch=2
    )


# ---- DEL-017: the approval freezes what it accepted -----------------------------


def test_del_017_approval_freezes_every_accepted_input() -> None:
    approved = _approve()
    assert approved.status is ReleaseStatus.APPROVED
    assert approved.approved_inputs == _freeze()


def test_del_017_the_frozen_record_is_immutable() -> None:
    frozen = _approve().approved_inputs
    assert frozen is not None
    with pytest.raises(ValidationError):
        frozen.proof_digest = OTHER_DIGEST  # type: ignore[misc]


def test_del_017_the_freeze_round_trips_through_the_stored_record() -> None:
    approved = _approve()
    assert Release.model_validate_json(approved.model_dump_json()) == approved


def test_del_017_a_freeze_without_an_approval_is_refused() -> None:
    with pytest.raises(ValidationError, match="approval_ref is unset"):
        release_record(approved_inputs=_freeze())


def test_del_017_a_freeze_naming_another_tag_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"is not v0\.7\.0\.dev1"):
        release_record(approval_ref=APPROVAL_REF, approved_inputs=_freeze(tag="v0.7.0.dev2"))


@pytest.mark.parametrize("targets", [(), ("pypi", "npm"), ("npm", "npm")])
def test_del_017_the_target_set_is_one_sorted_non_empty_spelling(
    targets: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        _freeze(target_ids=targets)


def test_del_017_a_single_target_freezes() -> None:
    assert _freeze(target_ids=("pypi",)).target_ids == ("pypi",)


def test_del_017_a_malformed_proof_digest_is_refused() -> None:
    with pytest.raises(ValidationError):
        _approve(proof_digest="md5:abc")


def test_del_017_an_unpinned_record_has_no_inputs_to_freeze() -> None:
    with pytest.raises(ValidationError):
        approval_inputs(
            release_record(status=ReleaseStatus.DRAFT, source_sha=None),
            dev1_config(),
            proof_digest=MANIFEST_DIGEST,
        )


# ---- REL-014: a changed input voids the approval --------------------------------


def test_rel_014_unchanged_inputs_keep_the_approval() -> None:
    approved = _approve()
    assert (
        invalidate_changed_approval(approved, dev1_config(), proof_digest=MANIFEST_DIGEST, at=NOW)
        is None
    )


@pytest.mark.parametrize(
    ("change", "cause"),
    [
        ({"source_sha": "e" * 40}, ReleaseInvalidationCause.HEAD_MOVED),
        ({"source_tree_sha": "e" * 40}, ReleaseInvalidationCause.HEAD_MOVED),
        (
            {"membership_refs": (f"{MILESTONE}#MAB-0001",)},
            ReleaseInvalidationCause.MEMBERSHIP_CHANGED,
        ),
        ({"manifest_digest": OTHER_DIGEST}, ReleaseInvalidationCause.ARTIFACT_CHANGED),
        ({"manifest_ref": "artifact://release/other"}, ReleaseInvalidationCause.ARTIFACT_CHANGED),
        ({"proof_digest": OTHER_DIGEST}, ReleaseInvalidationCause.ARTIFACT_CHANGED),
        ({"target_ids": ("npm",)}, ReleaseInvalidationCause.TARGETS_CHANGED),
        ({"policy_revision": 2}, ReleaseInvalidationCause.POLICY_CHANGED),
    ],
)
def test_rel_014_each_changed_input_names_its_typed_cause(
    change: dict[str, Any], cause: ReleaseInvalidationCause
) -> None:
    drift = approval_drift(_freeze(), _freeze(**change))
    assert drift is not None
    assert drift[0] is cause
    assert next(iter(change)) in drift[1]


def test_rel_014_no_change_is_no_drift() -> None:
    assert approval_drift(_freeze(), _freeze()) is None


def test_rel_014_a_moved_head_returns_the_release_to_draft() -> None:
    approved = _approve()
    moved = approved.model_copy(update={"source_sha": "e" * 40})
    voided = invalidate_changed_approval(
        moved, dev1_config(), proof_digest=MANIFEST_DIGEST, at=NOW + timedelta(minutes=5)
    )
    assert voided is not None
    assert voided.status is ReleaseStatus.DRAFT
    assert voided.approval_ref is None
    assert voided.approved_inputs is None
    assert voided.revision == approved.revision + 1
    invalidation = voided.last_invalidation
    assert invalidation is not None
    assert invalidation.cause is ReleaseInvalidationCause.HEAD_MOVED
    assert invalidation.prior_status is ReleaseStatus.APPROVED
    assert invalidation.invalidated_approval_ref == APPROVAL_REF
    assert invalidation.invalidated_at == NOW + timedelta(minutes=5)


def test_rel_014_a_different_artifact_set_voids_the_approval() -> None:
    voided = invalidate_changed_approval(
        _approve(), dev1_config(), proof_digest=OTHER_DIGEST, at=NOW
    )
    assert voided is not None
    assert voided.last_invalidation is not None
    assert voided.last_invalidation.cause is ReleaseInvalidationCause.ARTIFACT_CHANGED


def test_rel_014_a_changed_target_set_voids_the_approval() -> None:
    fewer = dev1_config(targets=[dev1_config().targets[0].model_dump(mode="json")])
    voided = invalidate_changed_approval(_approve(), fewer, proof_digest=MANIFEST_DIGEST, at=NOW)
    assert voided is not None
    assert voided.last_invalidation is not None
    assert voided.last_invalidation.cause is ReleaseInvalidationCause.TARGETS_CHANGED


def test_rel_014_a_change_after_external_effect_is_refused_not_voided() -> None:
    started = _approve().model_copy(
        update={"source_sha": "e" * 40, "publication_operation_ref": "operation://release/1"}
    )
    with pytest.raises(ReleaseTransitionError) as caught:
        invalidate_changed_approval(started, dev1_config(), proof_digest=MANIFEST_DIGEST, at=NOW)
    assert caught.value.code is ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED


def test_rel_014_an_approval_that_froze_nothing_is_not_judged() -> None:
    legacy = release_record(status=ReleaseStatus.APPROVED, approval_ref=APPROVAL_REF)
    assert (
        invalidate_changed_approval(legacy, dev1_config(), proof_digest=OTHER_DIGEST, at=NOW)
        is None
    )


# ---- PLAN-006: open verification debt blocks stable approval ---------------------


def test_plan_006_an_open_debt_blocks_stable_approval() -> None:
    with pytest.raises(ReleaseTransitionError) as caught:
        _approve(_stable_candidate(), verification_debts=(_debt(),))
    assert caught.value.code is ReleaseDenialCode.RELEASE_NOT_READY
    assert "VDT-0001" in str(caught.value)


def test_plan_006_a_discharged_debt_lets_stable_approve() -> None:
    discharged = _debt().discharge(
        head=SOURCE_SHA, evidence_ref=parse_qualified_urn(EVIDENCE), at=NOW + timedelta(hours=1)
    )
    approved = _approve(_stable_candidate(), verification_debts=(discharged,))
    assert approved.status is ReleaseStatus.APPROVED


def test_plan_006_an_open_debt_does_not_block_a_prerelease() -> None:
    approved = _approve(verification_debts=(_debt(),))
    assert approved.status is ReleaseStatus.APPROVED


def test_plan_006_a_red_sweep_still_names_its_gate_before_any_debt() -> None:
    probes = all_passing()
    probes[ReleaseSignalName.CHANGELOG] = fixed_probe(ReleaseSignalStatus.FAIL)
    red = compute_readiness(dev1_config(), probes=probes, computed_at=NOW)
    with pytest.raises(ReleaseTransitionError) as caught:
        approve_release(
            _stable_candidate(),
            red.model_copy(update={"release_key": "REL-0.7.0"}),
            dev1_config(),
            approval_ref=APPROVAL_REF,
            approved_at=NOW,
            proof_digest=MANIFEST_DIGEST,
            verification_debts=(_debt(),),
        )
    assert "changelog_entry" in str(caught.value)
    assert "VDT-0001" not in str(caught.value)
