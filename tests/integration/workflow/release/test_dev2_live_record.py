"""The committed ``0.7.0.dev2`` record, read back through the release stores.

Two claims over the record this repository actually ships, not over a fixture.

**The record is baked on three observed targets.** ``REL-0.7.0.dev2`` was walked
publish then reconcile then observe, and observe is the only verb that can write
``observed_success``: it reads the registry back and settles the leg against the
frozen manifest. So a baked record with three observed legs is a statement that
PyPI, npm and GitHub each served the artifact set the approval signed, and the
digest pinned here is the one the approval bound.

**The train is deliberately not advanced, and eleven of twelve gates carry a
receipt.** The twelfth, ``epoch1_stabilization``, runs the whole suite at the
pinned commit in a detached worktree. That suite asserts it never disturbed the
operator's live daemon runtime directory by comparing the directory's mtime
before and after, which is a sound witness only while the live daemon stays
idle. The daemon serving the receipts run is not idle, so the guard reds on
entry churn the suite did not cause, and no receipt can be earned for that gate
here. The remedy belongs to the tree the proof runs in, and that tree is the
pinned commit rather than this one, so it cannot be applied retroactively.

Advancing the train would only move the derived open rung onto ``dev3``, and no
``dev3`` record can occupy it: opening one needs ``membership_refs`` naming an
accepted Milestone acceptance bundle, and the canary rehearsal accepted none.
The advance therefore waits for the phase that can also cut the checkpoint.
"""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.spec.release import ReleaseStatus
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.workflow.release.records import read_release_record
from eawf.workflow.release.train_store import read_checkpoint_receipts, read_train_advances

RELEASE_KEY = "REL-0.7.0.dev2"
STATE_PATH = Path(__file__).resolve().parents[4] / ".ea" / "state.json"
APPROVED_MANIFEST_DIGEST = "sha256:" + (
    "3ea0e1250281a90d1840583cbd92c2751fc1c99b9d9f1824e69f740ea9c9e148"  # pragma: allowlist secret
)
PINNED_SOURCE_SHA = "d4f71dcffc5833e4d0498289d6d8db7cdafd54ac"  # pragma: allowlist secret
OBSERVED_TARGETS = ("github", "npm", "pypi")

#: The eleven dev2 gates proven at the pinned source. ``epoch1_stabilization``
#: is absent by the module docstring's reasoning and is asserted absent below,
#: so a gate silently appearing or vanishing reds this module.
RECEIPTED_GATES = frozenset(
    {
        ReleaseGateName.ARTIFACT_REPRODUCIBILITY,
        ReleaseGateName.CHANGELOG_ENTRY,
        ReleaseGateName.DEPENDENCY_INVENTORY,
        ReleaseGateName.FRONT_DOOR_JOURNEY,
        ReleaseGateName.HOSTED_GATE_RUNNER,
        ReleaseGateName.MIGRATION,
        ReleaseGateName.SCHEMA_STRICTNESS,
        ReleaseGateName.SECURITY_REVIEW,
        ReleaseGateName.TELEMETRY_PRODUCER,
        ReleaseGateName.VERSION_CONSISTENCY,
        ReleaseGateName.WAIVER_COUNT,
    }
)


def test_dev2_record_is_baked_on_three_observed_targets() -> None:
    """The committed record is baked on pypi, npm and github at the pinned source."""
    record = read_release_record(STATE_PATH, RELEASE_KEY)

    assert record is not None, f"{RELEASE_KEY} is absent from the release record collection"
    assert record.status is ReleaseStatus.BAKED
    assert record.manifest_digest == APPROVED_MANIFEST_DIGEST
    assert record.source_sha == PINNED_SOURCE_SHA

    statuses = record.target_statuses or {}
    assert tuple(sorted(statuses)) == OBSERVED_TARGETS
    for target in OBSERVED_TARGETS:
        assert statuses[target] == "observed_success", (
            f"target {target} settled at {statuses[target]!r}, so the record is not "
            f"baked on a read-back of every declared leg"
        )


def test_train_open_rung_past_dev2_is_deferred_with_eleven_receipts() -> None:
    """Eleven gates are receipted at the pinned source and the train has not moved."""
    receipts = read_checkpoint_receipts(STATE_PATH, RELEASE_KEY)

    assert {receipt.gate for receipt in receipts} == RECEIPTED_GATES
    assert ReleaseGateName.EPOCH1_STABILIZATION not in {receipt.gate for receipt in receipts}

    for receipt in receipts:
        assert receipt.source_sha == PINNED_SOURCE_SHA, (
            f"gate {receipt.gate.value} was proven at {receipt.source_sha}, not at the "
            f"source the record pins, so it does not vouch for this checkpoint"
        )
        assert receipt.manifest_digest == APPROVED_MANIFEST_DIGEST

    assert read_train_advances(STATE_PATH) == ()
