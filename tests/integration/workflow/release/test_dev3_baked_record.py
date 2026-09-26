"""The committed ``0.7.0.dev3`` record and advance, read back through the release stores.

These assert over the stores this repository ships, not over a fixture.

**The record is baked on four observed targets.** ``REL-0.7.0.dev3`` was
walked publish then reconcile then observe, and observe is the only verb
that writes ``observed_success``: it reads each registry back and settles
the leg against the frozen manifest. So a baked record with four observed
legs says PyPI, npm, GitHub and plugins-dist each served the artifact set
the approval signed, and the digest pinned here is the one it bound.

**The train walked past it once, on fresh receipts.** dev3 is the first
membership rung, so its gates are read from the checkpoint configuration
rendered with the record's own acceptance bundles. The one advance past
dev3 opens dev4 and cites the newest receipt of every required gate, each
bound to the pinned source and manifest and fresh at the advance's own
``advanced_at`` rather than at the time the suite runs.
"""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.release.checkpoint_template import with_membership_refs
from eawf.kernel.spec.release import Release, ReleaseStatus
from eawf.kernel.spec.release_config import ReleaseGateName, load_release_config
from eawf.workflow.release.advance import CheckpointGateReceipt, TrainAdvanceRecord
from eawf.workflow.release.records import read_release_record
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.release.train_store import read_checkpoint_receipts, read_train_advances

DEV3 = "0.7.0.dev3"
DEV3_KEY = "REL-0.7.0.dev3"
DEV4_KEY = "REL-0.7.0.dev4"
STATE_PATH = Path(__file__).resolve().parents[4] / ".ea" / "state.json"
APPROVED_MANIFEST_DIGEST = "sha256:" + (
    "c431c633f591df874501607db315baf00afe852867bfce2d95fcbbe18ca20eba"  # pragma: allowlist secret
)
PINNED_SOURCE_SHA = "026b33abddebcde0bc57eb71ff37c747db16d468"  # pragma: allowlist secret
OBSERVED_TARGETS = ("github", "npm", "plugins-dist", "pypi")


def dev3_record() -> Release:
    """Return the committed dev3 record, which must exist and be baked."""
    record = read_release_record(STATE_PATH, DEV3_KEY)
    assert record is not None, f"{DEV3_KEY} is absent from the release record collection"
    assert record.status is ReleaseStatus.BAKED
    return record


def dev3_required(record: Release) -> tuple[ReleaseGateName, ...]:
    """Return the gates dev3 requires, rendered with the record's membership refs."""
    document = with_membership_refs(
        checkpoint_config_yaml(DEV3), membership_refs=record.membership_refs
    )
    return tuple(load_release_config(document, train=V07_TRAIN).gates.required)


def dev3_advance() -> TrainAdvanceRecord:
    """Return the one recorded advance that walked the train past dev3."""
    advances = [row for row in read_train_advances(STATE_PATH) if row.closed_key == DEV3_KEY]
    assert len(advances) == 1, (
        f"expected exactly one train advance past {DEV3_KEY}, found {len(advances)}"
    )
    return advances[0]


def test_dev3_record_is_baked_on_four_observed_targets() -> None:
    """The committed record is baked on every declared target at the pinned source."""
    record = dev3_record()

    assert record.manifest_digest == APPROVED_MANIFEST_DIGEST
    assert record.source_sha == PINNED_SOURCE_SHA
    assert record.membership_refs, "dev3 requires membership, so the record must carry it"
    statuses = record.target_statuses or {}
    assert tuple(sorted(statuses)) == OBSERVED_TARGETS
    for target in OBSERVED_TARGETS:
        assert statuses[target] == "observed_success", (
            f"target {target} settled at {statuses[target]!r}, so the record is not "
            f"baked on a read-back of every declared leg"
        )


def test_dev3_train_advance_is_recorded_once_and_opens_dev4() -> None:
    """Exactly one advance closes dev3 and opens dev4.

    This asserts only the dev3 advance's own row, not where the train
    stands now: the train walks past dev4 on a later advance, and a
    "current position" assertion here would red on that legitimate next
    advance rather than on a regression of dev3's own record.
    """
    advance = dev3_advance()

    assert advance.train_id == V07_TRAIN.train_id
    assert advance.opened_key == DEV4_KEY


def test_dev3_train_advance_cites_every_required_receipt_fresh_when_judged() -> None:
    """The advance cites each required gate's newest bound receipt, fresh when judged."""
    record = dev3_record()
    advance = dev3_advance()
    required = dev3_required(record)
    newest: dict[ReleaseGateName, CheckpointGateReceipt] = {}
    for receipt in sorted(
        read_checkpoint_receipts(STATE_PATH, DEV3_KEY), key=lambda r: r.issued_at
    ):
        newest[receipt.gate] = receipt

    assert set(newest) >= set(required), (
        f"dev3 receipts cover {sorted(g.value for g in newest)}, not every required gate"
    )
    assert advance.receipt_refs == tuple(newest[gate].receipt_ref for gate in required)
    for gate in required:
        receipt = newest[gate]
        assert receipt.source_sha == PINNED_SOURCE_SHA
        assert receipt.manifest_digest == APPROVED_MANIFEST_DIGEST
        assert receipt.issued_at <= advance.advanced_at < receipt.expires_at, (
            f"gate {gate.value} receipt was not fresh when the advance was judged"
        )
