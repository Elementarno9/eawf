"""The committed ``0.7.0.dev2`` advance, read back through the release stores.

These assert over the stores this repository ships, not over a fixture.
``REL-0.7.0.dev2`` is baked, and the train walked past it only once every
gate its profile requires carried a receipt bound to the record's pinned
source and manifest. So the stores must hold twelve of twelve receipts
for dev2 and exactly one advance that closes dev2 and opens dev3, and
that advance must cite exactly those receipts, each fresh at the instant
the advance was judged.

Freshness is judged at the advance's own ``advanced_at``, never at the
time the suite runs: a receipt goes stale a day after it is issued, and
a stale receipt says nothing about whether it held when the train moved.
"""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.spec.release import Release, ReleaseStatus
from eawf.kernel.spec.release_config import ReleaseGateName, load_release_config
from eawf.workflow.release.advance import TrainAdvanceRecord, derive_train
from eawf.workflow.release.records import read_release_record, read_release_records
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.release.train_store import read_checkpoint_receipts, read_train_advances

DEV2_KEY = "REL-0.7.0.dev2"
DEV3_KEY = "REL-0.7.0.dev3"
STATE_PATH = Path(__file__).resolve().parents[4] / ".ea" / "state.json"


def dev2_required() -> tuple[ReleaseGateName, ...]:
    """Return the gates the authored dev2 profile requires, in gate order."""
    return tuple(
        load_release_config(checkpoint_config_yaml("0.7.0.dev2"), train=V07_TRAIN).gates.required
    )


def dev2_record() -> Release:
    """Return the committed dev2 record, which must exist and be baked."""
    record = read_release_record(STATE_PATH, DEV2_KEY)
    assert record is not None, f"{DEV2_KEY} is absent from the release record collection"
    assert record.status is ReleaseStatus.BAKED
    return record


def dev2_advance() -> TrainAdvanceRecord:
    """Return the one recorded advance that walked the train past dev2."""
    advances = [row for row in read_train_advances(STATE_PATH) if row.closed_key == DEV2_KEY]
    assert len(advances) == 1, (
        f"expected exactly one train advance past {DEV2_KEY}, found {len(advances)}"
    )
    return advances[0]


def test_dev2_holds_twelve_of_twelve_receipts_at_the_pinned_source() -> None:
    """Every gate the dev2 profile requires has a receipt bound to the record."""
    record = dev2_record()
    required = dev2_required()
    receipts = read_checkpoint_receipts(STATE_PATH, DEV2_KEY)

    assert len(required) == 12
    assert {receipt.gate for receipt in receipts} == set(required), (
        f"dev2 receipts cover {sorted(r.gate.value for r in receipts)}, not the "
        f"{len(required)} gates its profile requires"
    )
    for receipt in receipts:
        assert receipt.source_sha == record.source_sha, (
            f"gate {receipt.gate.value} was proven at {receipt.source_sha}, not at the "
            f"source the record pins, so it does not vouch for this checkpoint"
        )
        assert receipt.manifest_digest == record.manifest_digest


def test_dev2_train_advance_is_recorded_once_and_opens_dev3() -> None:
    """Exactly one advance closes dev2, and the derived train stands on dev3."""
    advance = dev2_advance()

    assert advance.train_id == V07_TRAIN.train_id
    assert advance.opened_key == DEV3_KEY
    train = derive_train(
        V07_TRAIN,
        recorded_keys=read_release_records(STATE_PATH),
        advances=read_train_advances(STATE_PATH),
    )
    rung_keys = [rung.release_key for rung in V07_TRAIN.checkpoints]
    assert train.current_checkpoint_index > rung_keys.index(DEV2_KEY)


def test_dev2_train_advance_cites_the_twelve_receipts_fresh_when_judged() -> None:
    """The advance cites the stored receipts in gate order, each fresh at ``advanced_at``."""
    advance = dev2_advance()
    newest = {receipt.gate: receipt for receipt in read_checkpoint_receipts(STATE_PATH, DEV2_KEY)}

    assert advance.receipt_refs == tuple(newest[gate].receipt_ref for gate in dev2_required())
    for receipt in newest.values():
        assert receipt.issued_at <= advance.advanced_at < receipt.expires_at, (
            f"gate {receipt.gate.value} receipt was not fresh when the advance was judged"
        )
