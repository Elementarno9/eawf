"""Cutting the ``0.7.0.dev1`` checkpoint to APPROVED, and cutting nothing else.

Two claims are pinned here, and one gap is pinned honestly beside them.

**The eight gates.** The ``dev1`` profile declares eight gates. Five
read a readiness row; three are settled by running an argv at the
pinned source revision and attaching a receipt. A checkpoint is
approvable only when every one of the eight carries a *fresh* receipt
bound to the exact source and manifest the record pins -- a receipt
earned on other source, on another manifest, or past its expiry, is not
evidence about this build.

**No external effect.** Reaching APPROVED publishes nothing. The
approved record leaves every publication target ``not_started``, carries
no publication operation reference, and binds a manifest digest the
post-merge bake can reuse as its proof digest without re-pinning. The
absence of the ``v0.7.0.dev1`` tag -- locally and on the publishing
remote -- is asserted here rather than assumed, because the tag push
*is* the publication decision.

**The gap.** Three required rows (``artifacts``, ``dependencies``,
``credentials``) cannot be greened by anything a working copy runs: the
first two read receipts a CI job writes on a tag run, and the third has
no producer at this checkpoint at all. The three proof-command gates are
likewise never green *in a sweep*, because the sweep does not run them.
Those facts are pinned by exact set equality, so the day a producer
lands this module reds and the claim gets revisited instead of quietly
staying stale.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from eawf import __version__
from eawf.kernel.release.gate_binding import GateEvidenceKind
from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalProbe,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseGateProfile,
    ReleaseStatus,
    ReleaseTargetStatus,
    validate_release_against_train,
)
from eawf.kernel.spec.release_config import (
    ReleaseConfig,
    ReleaseGateName,
    load_release_config,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.release import show as show_rpc
from eawf.workflow.release.advance import (
    CheckpointGateReceipt,
    TrainAdvanceDenialCode,
    TrainAdvanceError,
    assert_prerequisite_receipts,
    draft_release_for,
)
from eawf.workflow.release.lifecycle import advance_release
from eawf.workflow.release.preflight import approve_release
from eawf.workflow.release.producers import RECEIPT_PRODUCER_JOB, build_receipt_probes
from eawf.workflow.release.publication import begin_publication
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import ReleaseReadiness, compute_readiness

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

DEV1_VERSION = "0.7.0.dev1"
DEV1_KEY = f"REL-{DEV1_VERSION}"
DEV1_TAG = f"v{DEV1_VERSION}"
PUBLISHING_REMOTE = "origin"

SOURCE_SHA = "a" * 40
TREE_SHA = "b" * 40
MANIFEST_DIGEST = f"sha256:{'c' * 64}"
APPROVAL_REF = "receipt://approval/rel-0.7.0.dev1"

#: Repository this checkout is rooted at; the tag-absence and
#: version/changelog assertions read the shipped tree, not a fixture.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: The required rows no working copy can green at this checkpoint.
#: ``artifacts`` and ``dependencies`` read CI receipts; ``credentials``
#: has no producer at all and is required only because the dev1
#: configuration declares required publication targets.
PRODUCERLESS_REQUIRED_ROWS = frozenset(
    {
        ReleaseSignalName.ARTIFACTS,
        ReleaseSignalName.DEPENDENCIES,
        ReleaseSignalName.CREDENTIALS,
    }
)

#: The dev1 gates settled by running an argv rather than by a row.
PROOF_GATES: dict[ReleaseGateName, str] = {
    ReleaseGateName.EPOCH1_STABILIZATION: "epoch1_stabilization_suite",
    ReleaseGateName.TELEMETRY_PRODUCER: "telemetry_turn_cost_record",
    ReleaseGateName.FRONT_DOOR_JOURNEY: "front_door_install_smoke",
}

CTX = MethodContext(
    started_at="2026-09-07T00:00:00+00:00",
    pid=4321,
    protocol_version="1",
    version=DEV1_VERSION,
)


def dev1_config() -> ReleaseConfig:
    """Return the authored dev1 checkpoint configuration."""
    return load_release_config(checkpoint_config_yaml(DEV1_VERSION), train=V07_TRAIN)


def fixed_probe(status: ReleaseSignalStatus) -> ReleaseSignalProbe:
    """Return a probe reporting *status* for whichever signal it is asked."""
    remediation = "" if status is ReleaseSignalStatus.PASS else f"repair the {status.value} row"

    def run(_context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        return ReleaseSignalOutcome(status=status, remediation=remediation)

    return run


def all_passing() -> dict[ReleaseSignalName, ReleaseSignalProbe]:
    """Return a probe registry in which every signal passes."""
    return dict.fromkeys(ReleaseSignalName, fixed_probe(ReleaseSignalStatus.PASS))


def green_sweep() -> ReleaseReadiness:
    """Return the dev1 sweep every required row of which passes."""
    return compute_readiness(dev1_config(), probes=all_passing(), computed_at=NOW)


def pinned_candidate(**overrides: Any) -> Release:
    """Return the dev1 DRAFT record advanced to a pinned CANDIDATE."""
    draft = draft_release_for(V07_TRAIN.checkpoint_for_version(DEV1_VERSION), uid=UUID(int=26))
    return advance_release(
        draft,
        ReleaseStatus.CANDIDATE,
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref="artifact://release/manifest/0.7.0.dev1",
        manifest_digest=MANIFEST_DIGEST,
        **overrides,
    )


def receipt_for(gate: ReleaseGateName, **overrides: Any) -> CheckpointGateReceipt:
    """Return a fresh receipt for *gate* bound to the pinned source."""
    payload: dict[str, Any] = {
        "gate": gate,
        "release_key": DEV1_KEY,
        "source_sha": SOURCE_SHA,
        "manifest_digest": MANIFEST_DIGEST,
        "issued_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(hours=1),
        "receipt_ref": f"receipt://gate/{gate.value}",
    }
    payload.update(overrides)
    return CheckpointGateReceipt(**payload)


def eight_receipts(**overrides: Any) -> list[CheckpointGateReceipt]:
    """Return one fresh receipt per required dev1 gate, in profile order."""
    return [receipt_for(gate, **overrides) for gate in dev1_config().gates.required]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one read-only git command against this checkout."""
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


# --- the eight gates -------------------------------------------------


def test_approved_eight_gates_open_a_dev_channel_epoch_one_record() -> None:
    rung = V07_TRAIN.checkpoint_for_version(DEV1_VERSION)
    draft = draft_release_for(rung, uid=UUID(int=26))
    assert draft.key == DEV1_KEY
    assert draft.channel is ReleaseChannel.DEV
    assert draft.authority_epoch == 1
    assert draft.membership_refs == ()
    assert draft.status is ReleaseStatus.DRAFT
    assert validate_release_against_train(draft, V07_TRAIN) is rung
    assert rung.gate_profile is ReleaseGateProfile.DEV1
    assert rung.requires_membership is False


def test_approved_eight_gates_are_the_rung_the_train_surface_reports() -> None:
    result = asyncio.run(show_rpc(CTX, {"version": DEV1_VERSION}))
    assert result["train_id"] == V07_TRAIN.train_id
    assert result["checkpoint"] == {
        "release_key": DEV1_KEY,
        "authority_epoch": 1,
        "gate_profile": "dev1",
        "requires_membership": False,
    }
    assert len(dev1_config().gates.required) == 8


def test_approved_eight_gates_each_carry_a_receipt_bound_to_the_pinned_source() -> None:
    config = dev1_config()
    refs = assert_prerequisite_receipts(
        pinned_candidate(),
        config,
        eight_receipts(),
        now=NOW,
        train_id=V07_TRAIN.train_id,
    )
    assert refs == tuple(f"receipt://gate/{gate.value}" for gate in config.gates.required)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_sha", "d" * 40),
        ("manifest_digest", f"sha256:{'e' * 64}"),
        ("release_key", "REL-0.7.0.dev2"),
    ],
)
def test_approved_eight_gates_refuse_a_receipt_that_binds_another_build(
    field: str, value: str
) -> None:
    receipts = eight_receipts()
    receipts[0] = receipt_for(receipts[0].gate, **{field: value})
    with pytest.raises(TrainAdvanceError) as excinfo:
        assert_prerequisite_receipts(
            pinned_candidate(),
            dev1_config(),
            receipts,
            now=NOW,
            train_id=V07_TRAIN.train_id,
        )
    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE
    assert field in str(excinfo.value)


def test_approved_eight_gates_refuse_a_receipt_that_has_expired() -> None:
    receipts = eight_receipts()
    receipts[0] = receipt_for(
        receipts[0].gate,
        issued_at=NOW - timedelta(hours=3),
        expires_at=NOW - timedelta(minutes=1),
    )
    with pytest.raises(TrainAdvanceError) as excinfo:
        assert_prerequisite_receipts(
            pinned_candidate(),
            dev1_config(),
            receipts,
            now=NOW,
            train_id=V07_TRAIN.train_id,
        )
    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE
    assert "expired at" in str(excinfo.value)


def test_approved_eight_gates_name_the_one_gate_carrying_no_receipt() -> None:
    receipts = eight_receipts()
    dropped = receipts.pop(4)
    with pytest.raises(TrainAdvanceError) as excinfo:
        assert_prerequisite_receipts(
            pinned_candidate(),
            dev1_config(),
            receipts,
            now=NOW,
            train_id=V07_TRAIN.train_id,
        )
    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_MISSING
    assert excinfo.value.gate is dropped.gate


def test_approved_eight_gates_refuse_a_receipt_expiring_before_it_was_issued() -> None:
    with pytest.raises(ValueError, match="expires_at must be after issued_at"):
        receipt_for(ReleaseGateName.CHANGELOG_ENTRY, expires_at=NOW - timedelta(hours=9))


def test_approved_eight_gates_carry_the_pinned_candidate_to_approved() -> None:
    sweep = green_sweep()
    assert sweep.ready is True
    approved = approve_release(
        pinned_candidate(), sweep, approval_ref=APPROVAL_REF, approved_at=NOW
    )
    assert approved.status is ReleaseStatus.APPROVED
    assert approved.approval_ref == APPROVAL_REF
    assert approved.manifest_digest == MANIFEST_DIGEST
    assert approved.source_sha == SOURCE_SHA


def test_approved_eight_gates_settle_version_and_changelog_from_the_shipped_tree() -> None:
    # The two rows this wave's own commit moved: the version module and
    # the changelog section are read out of the checkout, not a fixture.
    probes = build_tag_probes(
        TagPreflightInputs(
            repo_root=REPO_ROOT,
            version=DEV1_VERSION,
            tag=DEV1_TAG,
            package_version=__version__,
            remote=PUBLISHING_REMOTE,
        )
    )
    sweep = compute_readiness(dev1_config(), probes=probes, computed_at=NOW)
    assert __version__ == DEV1_VERSION
    for signal in (
        ReleaseSignalName.VERSION_CONSISTENCY,
        ReleaseSignalName.CHANGELOG,
        ReleaseSignalName.MIGRATION,
    ):
        assert sweep.row(signal).status is ReleaseSignalStatus.PASS, signal


def test_approved_eight_gates_leave_three_required_rows_without_a_producer(
    tmp_path: Path,
) -> None:
    # Only the rows a working copy can answer are stubbed green; the
    # three under test are left to the shipped producers, reading a
    # checkout that carries no receipts.
    probes = {
        signal: fixed_probe(ReleaseSignalStatus.PASS)
        for signal in ReleaseSignalName
        if signal not in PRODUCERLESS_REQUIRED_ROWS
    } | build_receipt_probes(tmp_path)
    sweep = compute_readiness(dev1_config(), probes=probes, computed_at=NOW)
    unavailable = {
        row.signal
        for row in sweep.signals
        if row.signal in set(sweep.required_signals)
        and row.status is ReleaseSignalStatus.UNAVAILABLE
    }
    assert unavailable == set(PRODUCERLESS_REQUIRED_ROWS)
    assert sweep.ready is False
    assert RECEIPT_PRODUCER_JOB in sweep.row(ReleaseSignalName.ARTIFACTS).remediation
    assert RECEIPT_PRODUCER_JOB in sweep.row(ReleaseSignalName.DEPENDENCIES).remediation
    assert "no producer is registered" in sweep.row(ReleaseSignalName.CREDENTIALS).remediation


def test_approved_eight_gates_leave_the_three_proof_gates_unavailable_in_a_sweep() -> None:
    sweep = green_sweep()
    proof = {
        row.gate: row for row in sweep.gates if row.evidence_kind is GateEvidenceKind.PROOF_COMMAND
    }
    assert set(proof) == set(PROOF_GATES)
    for gate, command_id in PROOF_GATES.items():
        assert proof[gate].status is ReleaseSignalStatus.UNAVAILABLE
        assert proof[gate].evidence_ref == f"proof:{command_id}"
        assert command_id in proof[gate].remediation


# --- no external effect ----------------------------------------------


def test_approved_no_effect_leaves_every_target_row_not_started() -> None:
    targets = {
        target.target_id: ReleaseTargetStatus.NOT_STARTED for target in dev1_config().targets
    }
    assert len(targets) == 3
    approved = approve_release(
        pinned_candidate(target_statuses=targets),
        green_sweep(),
        approval_ref=APPROVAL_REF,
        approved_at=NOW,
    )
    assert dict(approved.target_statuses) == targets
    assert set(approved.target_statuses.values()) == {ReleaseTargetStatus.NOT_STARTED}


def test_approved_no_effect_leaves_the_publication_operation_ref_null() -> None:
    approved = approve_release(
        pinned_candidate(), green_sweep(), approval_ref=APPROVAL_REF, approved_at=NOW
    )
    assert approved.publication_operation_ref is None
    assert approved.target_statuses == {}
    assert approved.last_invalidation is None
    assert approved.supersedes_release_ref is None


def test_approved_no_effect_binds_a_proof_digest_the_bake_can_reuse() -> None:
    # The bake runs after the phase merges. It is exercised here over an
    # in-memory copy -- no adapter is called and nothing is persisted --
    # purely to prove the approved manifest digest is reusable as the
    # proof digest, so the bake never has to re-pin the record.
    approved = approve_release(
        pinned_candidate(), green_sweep(), approval_ref=APPROVAL_REF, approved_at=NOW
    )
    published, operation = begin_publication(
        approved,
        dev1_config(),
        green_sweep(),
        operation_id=UUID(int=260),
        approved_manifest_digest=approved.manifest_digest or "",
        idempotency_key="rel-0.7.0.dev1-bake-1",
        proof_digest=approved.manifest_digest or "",
        opened_at=NOW,
    )
    assert operation.proof_digest == approved.manifest_digest == MANIFEST_DIGEST
    assert published.manifest_digest == approved.manifest_digest
    assert published.source_sha == approved.source_sha


def test_approved_no_effect_leaves_no_local_tag() -> None:
    listing = _git("tag", "--list", DEV1_TAG)
    assert listing.returncode == 0, listing.stderr
    assert listing.stdout.strip() == ""


def test_approved_no_effect_leaves_no_tag_on_the_publishing_remote() -> None:
    if _git("remote", "get-url", PUBLISHING_REMOTE).returncode != 0:
        pytest.skip(f"no {PUBLISHING_REMOTE!r} remote configured in this checkout")
    listing = _git("ls-remote", "--tags", PUBLISHING_REMOTE, f"refs/tags/{DEV1_TAG}")
    if listing.returncode != 0:
        pytest.skip(f"{PUBLISHING_REMOTE!r} is unreachable: {listing.stderr.strip()}")
    assert listing.stdout.strip() == ""
