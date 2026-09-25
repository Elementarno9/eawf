"""REL-011: the train walks forward only from a finished checkpoint.

The index on a :class:`~eawf.kernel.spec.release.ReleaseTrain` is the
project's claim about which checkpoint it is working on, so moving it
from a checkpoint that never shipped would be a claim about work nobody
did. These tests pin that the move happens only from ``baked`` or
``released``, that every other status -- including the two terminal
statuses that mean *abandoned* -- denies ``checkpoint_not_terminal``,
and that a denied advance leaves the train's index and the closing
record exactly as they were.

They also pin what the advance no longer does. It opens no DRAFT, so the
only way into the next checkpoint's record is ``release create`` and its
measured admission. And the receipts it judges are the newest ones the
store holds, so a stale stored receipt refuses the move exactly as a
stale offered one does.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.release import (
    Release,
    ReleaseStatus,
    ReleaseTrain,
    validate_release_against_train,
)
from eawf.kernel.spec.release_config import (
    ReleaseConfig,
    ReleaseGateName,
    load_release_config,
)
from eawf.workflow.release.advance import (
    ADVANCING_STATUSES,
    CheckpointGateReceipt,
    TrainAdvance,
    TrainAdvanceDenialCode,
    TrainAdvanceError,
    TrainAdvanceRecord,
    advance_train,
    assert_checkpoint_terminal,
    derive_train,
    draft_release_for,
)
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.release.train_store import (
    checkpoint_receipts_path,
    read_checkpoint_receipts,
    read_train_advances,
    record_checkpoint_receipt,
    record_train_advance,
)
from tests._release_helpers import (
    MANIFEST_DIGEST,
    NOW,
    SOURCE_SHA,
    dev1_config,
    release_record,
)

#: Identity minted for a DRAFT record of the next rung.
NEXT_UID = UUID(int=31)

#: The approval every post-APPROVED status requires.
APPROVAL_REF = "receipt://approval/dev1"

#: Every status the advance must refuse. The first five are in-flight;
#: ``cancelled`` and ``partially_released`` are terminal in the status
#: machine but mean the checkpoint was abandoned, not shipped.
REFUSED_STATUSES = (
    ReleaseStatus.APPROVED,
    ReleaseStatus.PUBLISHING,
    ReleaseStatus.VERIFYING,
    ReleaseStatus.RECOVERING,
    ReleaseStatus.PARTIALLY_RELEASED,
    ReleaseStatus.CANCELLED,
)


def gate_receipts(
    *,
    release_key: str = "REL-0.7.0.dev1",
    source_sha: str = SOURCE_SHA,
    manifest_digest: str = MANIFEST_DIGEST,
    gates: Sequence[ReleaseGateName] | None = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    ref_suffix: str = "",
) -> list[CheckpointGateReceipt]:
    """Return one receipt per required dev1 gate, fresh at ``NOW`` by default.

    *ref_suffix* keeps the refs of two issues of one gate distinct, as the
    store's row ids must be.
    """
    required = dev1_config().gates.required if gates is None else gates
    return [
        CheckpointGateReceipt(
            gate=gate,
            release_key=release_key,
            source_sha=source_sha,
            manifest_digest=manifest_digest,
            issued_at=NOW - timedelta(hours=1) if issued_at is None else issued_at,
            expires_at=NOW + timedelta(hours=1) if expires_at is None else expires_at,
            receipt_ref=f"receipt://gate/{gate.value}{ref_suffix}",
        )
        for gate in required
    ]


def finished(status: ReleaseStatus = ReleaseStatus.BAKED) -> Release:
    """Return the dev1 record at a terminal *status*."""
    return release_record(status=status, approval_ref=APPROVAL_REF)


def advance(
    current: Release,
    *,
    receipts: Sequence[CheckpointGateReceipt] | None = None,
    now: datetime | None = None,
) -> TrainAdvance:
    """Advance the v0.7.0 train past *current* with fresh dev1 receipts."""
    return advance_train(
        V07_TRAIN,
        current=current,
        config=dev1_config(),
        receipts=gate_receipts() if receipts is None else receipts,
        now=NOW if now is None else now,
    )


def store_receipts(state_path: Path, receipts: Sequence[CheckpointGateReceipt]) -> None:
    """Append *receipts* to the checkpoint receipt collection under *state_path*."""
    for receipt in receipts:
        record_checkpoint_receipt(state_path, receipt, recorded_at=NOW, summary="seeded")


# --- the advancing statuses --------------------------------------------------


def test_advancing_statuses_are_exactly_baked_and_released() -> None:
    """The advance set is the two shipped terminals, nothing wider."""
    assert frozenset({ReleaseStatus.BAKED, ReleaseStatus.RELEASED}) == ADVANCING_STATUSES


@pytest.mark.parametrize("status", sorted(ADVANCING_STATUSES, key=lambda s: s.value))
def test_advance_train_moves_onto_the_next_rung_from_a_shipped_checkpoint(
    status: ReleaseStatus,
) -> None:
    """A baked or released dev1 moves the index onto dev2."""
    result = advance(finished(status))

    assert result.train.current_checkpoint_index == 1
    assert result.record.closed_key == "REL-0.7.0.dev1"
    assert result.record.opened_key == "REL-0.7.0.dev2"


def test_advance_train_opens_no_draft_record() -> None:
    """The result names the next rung but carries no record for it."""
    result = advance(finished())

    assert [field.name for field in dataclasses.fields(TrainAdvance)] == [
        "train",
        "closed",
        "record",
    ]
    assert not hasattr(result, "opened")


def test_advance_train_never_builds_a_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """The advance succeeds with the DRAFT builder unusable, so it never calls it.

    ``draft_release_for`` performs no measured admission itself; only
    ``release create`` wraps it in one. An advance that still called it
    would be a way into a checkpoint record that skips admission.
    """

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("advance_train built a DRAFT record")

    monkeypatch.setattr("eawf.workflow.release.advance.draft_release_for", refuse)

    assert advance(finished()).train.current_checkpoint_index == 1


def test_advance_train_record_names_the_revisions_it_judged() -> None:
    """The persisted row carries the closed revision and the new train revision."""
    closing = release_record(status=ReleaseStatus.BAKED, approval_ref=APPROVAL_REF, revision=9)

    record = advance(closing).record

    assert record.train_id == V07_TRAIN.train_id
    assert record.closed_revision == 9
    assert record.train_revision == V07_TRAIN.revision + 1
    assert record.advanced_at == NOW
    assert record.receipt_refs == advance(closing).receipt_refs


def test_advance_train_bumps_the_train_revision() -> None:
    """The advanced train is a new revision, not a silent in-place move."""
    result = advance(finished())

    assert result.train.revision == V07_TRAIN.revision + 1


def test_advance_train_records_the_prior_checkpoint_receipt_refs() -> None:
    """The validated receipts land against the checkpoint that earned them."""
    result = advance(finished())

    recorded = result.train.gate_receipt_refs["REL-0.7.0.dev1"]
    assert recorded == tuple(
        f"receipt://gate/{gate.value}" for gate in dev1_config().gates.required
    )


# --- the refusals ------------------------------------------------------------


@pytest.mark.parametrize("status", REFUSED_STATUSES)
def test_advance_train_refuses_every_unshipped_status(status: ReleaseStatus) -> None:
    """Each non-advancing status denies checkpoint_not_terminal."""
    with pytest.raises(TrainAdvanceError) as excinfo:
        advance(finished(status))

    assert excinfo.value.code is TrainAdvanceDenialCode.CHECKPOINT_NOT_TERMINAL
    assert status.value in str(excinfo.value)


@pytest.mark.parametrize("status", REFUSED_STATUSES)
def test_advance_train_leaves_the_index_unchanged_on_refusal(status: ReleaseStatus) -> None:
    """A refused advance does not move the train it was offered."""
    with pytest.raises(TrainAdvanceError):
        advance(finished(status))

    assert V07_TRAIN.current_checkpoint_index == 0
    assert V07_TRAIN.current_checkpoint.release_key == "REL-0.7.0.dev1"


def test_advance_train_refuses_a_record_the_train_does_not_have_open() -> None:
    """Offering dev2 while dev1 is open denies checkpoint_key_mismatch."""
    stranger = release_record(
        key="REL-0.7.0.dev2",
        version="0.7.0.dev2",
        status=ReleaseStatus.BAKED,
        approval_ref=APPROVAL_REF,
    )

    with pytest.raises(TrainAdvanceError) as excinfo:
        advance(stranger)

    assert excinfo.value.code is TrainAdvanceDenialCode.CHECKPOINT_KEY_MISMATCH


def test_advance_train_refuses_at_the_last_rung() -> None:
    """A train standing on stable has no checkpoint left to open."""
    at_stable = ReleaseTrain.model_validate(
        V07_TRAIN.model_copy(
            update={"current_checkpoint_index": len(V07_TRAIN.checkpoints) - 1}
        ).model_dump(mode="json")
    )
    stable = release_record(
        key="REL-0.7.0",
        version="0.7.0",
        channel="stable",
        authority_epoch=2,
        membership_refs=("bundle://milestone/epoch2",),
        status=ReleaseStatus.RELEASED,
        approval_ref=APPROVAL_REF,
    )

    with pytest.raises(TrainAdvanceError) as excinfo:
        advance_train(
            at_stable,
            current=stable,
            config=dev1_config(),
            receipts=gate_receipts(),
            now=NOW,
        )

    assert excinfo.value.code is TrainAdvanceDenialCode.LADDER_EXHAUSTED


# --- error paths -------------------------------------------------------------


def test_advance_train_rejects_a_naive_instant() -> None:
    """Freshness judged against a naive clock is refused, not guessed."""
    with pytest.raises(ValueError, match="timezone-aware"):
        advance(finished(), now=NOW.replace(tzinfo=None))


def test_advance_train_rejects_a_configuration_for_another_checkpoint() -> None:
    """A dev1 advance may not be validated against another rung's gates."""
    mismatched = dev1_config().model_copy(update={"version": "0.7.0.dev2"})

    with pytest.raises(ValueError, match="configuration describes"):
        advance_train(
            V07_TRAIN,
            current=finished(),
            config=mismatched,
            receipts=gate_receipts(),
            now=NOW,
        )


def test_advance_train_rejects_a_gate_offered_twice() -> None:
    """Two receipts for one gate is an ambiguity, not a pick-one."""
    doubled = [*gate_receipts(), gate_receipts()[0]]

    with pytest.raises(ValueError, match="two receipts offered"):
        advance(finished(), receipts=doubled)


def test_advance_train_refuses_an_expired_receipt_as_stale() -> None:
    """A receipt past its window is stale even though it binds the source."""
    expired = gate_receipts(issued_at=NOW - timedelta(days=2), expires_at=NOW - timedelta(days=1))

    with pytest.raises(TrainAdvanceError) as excinfo:
        advance(finished(), receipts=expired)

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE
    assert excinfo.value.gate is dev1_config().gates.required[0]


def test_advance_train_refuses_a_receipt_expiring_exactly_now() -> None:
    """The window is half-open: a receipt expiring at the judged instant is stale."""
    boundary = gate_receipts(expires_at=NOW)

    with pytest.raises(TrainAdvanceError) as excinfo:
        advance(finished(), receipts=boundary)

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE


def test_advance_train_refuses_a_gate_with_no_receipt() -> None:
    """One required gate left unproven refuses the move and names the gate."""
    required = dev1_config().gates.required

    with pytest.raises(TrainAdvanceError) as excinfo:
        advance(finished(), receipts=gate_receipts(gates=required[:-1]))

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_MISSING
    assert excinfo.value.gate is required[-1]


# --- the stored receipts -----------------------------------------------------


def test_advance_train_accepts_the_newest_stored_receipts(tmp_path: Path) -> None:
    """An expired receipt stored first is superseded by the fresh one after it."""
    state_path = tmp_path / ".ea" / "state.json"
    store_receipts(
        state_path,
        gate_receipts(
            issued_at=NOW - timedelta(days=3),
            expires_at=NOW - timedelta(days=2),
            ref_suffix="/old",
        ),
    )
    store_receipts(state_path, gate_receipts(ref_suffix="/new"))

    stored = read_checkpoint_receipts(state_path, "REL-0.7.0.dev1")
    result = advance(finished(), receipts=stored)

    assert len(stored) == len(dev1_config().gates.required)
    assert result.train.current_checkpoint_index == 1


def test_advance_train_refuses_when_the_newest_stored_receipt_expired(tmp_path: Path) -> None:
    """A fresh receipt stored before an expired one does not speak for the gate."""
    state_path = tmp_path / ".ea" / "state.json"
    store_receipts(
        state_path, gate_receipts(issued_at=NOW - timedelta(minutes=30), ref_suffix="/old")
    )
    store_receipts(
        state_path,
        gate_receipts(
            issued_at=NOW - timedelta(minutes=10),
            expires_at=NOW - timedelta(minutes=5),
            ref_suffix="/new",
        ),
    )

    with pytest.raises(TrainAdvanceError) as excinfo:
        advance(finished(), receipts=read_checkpoint_receipts(state_path, "REL-0.7.0.dev1"))

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE


def test_read_checkpoint_receipts_skips_other_checkpoints(tmp_path: Path) -> None:
    """Receipts earned by another checkpoint are not this one's."""
    state_path = tmp_path / ".ea" / "state.json"
    store_receipts(state_path, gate_receipts(release_key="REL-0.7.0.dev2"))

    assert read_checkpoint_receipts(state_path, "REL-0.7.0.dev1") == ()


def test_read_checkpoint_receipts_reads_nothing_before_the_first_write(tmp_path: Path) -> None:
    """An absent collection is empty, not an error."""
    assert read_checkpoint_receipts(tmp_path / ".ea" / "state.json", "REL-0.7.0.dev1") == ()


def test_read_checkpoint_receipts_refuses_a_corrupt_row(tmp_path: Path) -> None:
    """A line that is not an envelope is a refusal, never a skipped gate."""
    state_path = tmp_path / ".ea" / "state.json"
    path = checkpoint_receipts_path(state_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"not": "an envelope"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="is not an envelope"):
        read_checkpoint_receipts(state_path, "REL-0.7.0.dev1")


def test_record_checkpoint_receipt_refuses_a_naive_instant(tmp_path: Path) -> None:
    """A row whose instant carries no zone cannot be ordered, so it is refused."""
    with pytest.raises(ValueError, match="timezone-aware"):
        record_checkpoint_receipt(
            tmp_path / ".ea" / "state.json",
            gate_receipts()[0],
            recorded_at=NOW.replace(tzinfo=None),
            summary="naive",
        )


# --- the advance row ---------------------------------------------------------


def test_train_advance_record_refuses_closing_and_opening_one_rung() -> None:
    """An advance that stays on its rung is not an advance."""
    with pytest.raises(ValidationError, match="closes and opens the same rung"):
        TrainAdvanceRecord(
            train_id="TRAIN-0.7.0",
            closed_key="REL-0.7.0.dev2",
            closed_revision=3,
            opened_key="REL-0.7.0.dev2",
            receipt_refs=("receipt://gate/migration",),
            advanced_at=NOW,
            train_revision=1,
        )


def test_train_advance_record_refuses_a_train_revision_of_zero() -> None:
    """Revision zero is the train before any advance, so no advance can carry it."""
    with pytest.raises(ValidationError):
        TrainAdvanceRecord(
            train_id="TRAIN-0.7.0",
            closed_key="REL-0.7.0.dev2",
            closed_revision=3,
            opened_key="REL-0.7.0.dev3",
            receipt_refs=("receipt://gate/migration",),
            advanced_at=NOW,
            train_revision=0,
        )


# --- the terminal predicate --------------------------------------------------


@pytest.mark.parametrize("status", sorted(ADVANCING_STATUSES, key=lambda s: s.value))
def test_assert_checkpoint_terminal_admits_a_shipped_checkpoint(status: ReleaseStatus) -> None:
    """The predicate returns quietly for the two shipped terminals."""
    assert assert_checkpoint_terminal(V07_TRAIN, finished(status)) is None


def test_assert_checkpoint_terminal_names_the_admitted_statuses() -> None:
    """The denial tells the operator which statuses would have worked."""
    with pytest.raises(TrainAdvanceError) as excinfo:
        assert_checkpoint_terminal(V07_TRAIN, finished(ReleaseStatus.VERIFYING))

    message = str(excinfo.value)
    assert "baked" in message
    assert "released" in message


# --- the record the advance mints --------------------------------------------


def test_draft_release_for_a_membership_rung_refuses_empty_bundles() -> None:
    """A rung that requires acceptance bundles will not open without them."""
    rung = V07_TRAIN.checkpoint_for_version("0.7.0.dev3")
    drafted = draft_release_for(rung, uid=NEXT_UID)

    with pytest.raises(ValueError, match="requires non-empty membership_refs"):
        validate_release_against_train(drafted, V07_TRAIN)


def test_draft_release_for_an_epoch1_rung_refuses_membership_bundles() -> None:
    """dev2 forbids bundles outright; offering one is a validation error."""
    rung = V07_TRAIN.checkpoint_for_version("0.7.0.dev2")

    with pytest.raises(ValueError, match="membership_refs must be empty"):
        draft_release_for(rung, uid=NEXT_UID, membership_refs=("bundle://early",))


# --- the dev2 checkpoint -----------------------------------------------------

#: The dev2 record the train closes, and the rung it opens.
DEV2_KEY = "REL-0.7.0.dev2"
DEV3_KEY = "REL-0.7.0.dev3"


def dev2_config() -> ReleaseConfig:
    """Return the authored dev2 configuration, whose profile requires twelve gates."""
    return load_release_config(checkpoint_config_yaml("0.7.0.dev2"), train=V07_TRAIN)


def baked_dev2() -> Release:
    """Return the dev2 record at ``baked``, pinned to the shared source."""
    return release_record(
        uid=UUID(int=32),
        key=DEV2_KEY,
        version="0.7.0.dev2",
        status=ReleaseStatus.BAKED,
        approval_ref="receipt://approval/dev2",
    )


def train_on_dev2() -> ReleaseTrain:
    """Return the train standing on dev2, as the stores place it once dev2 is recorded."""
    train = derive_train(V07_TRAIN, recorded_keys=("REL-0.7.0.dev1", DEV2_KEY), advances=())
    assert train.current_checkpoint.release_key == DEV2_KEY
    return train


def advance_dev2(state_path: Path) -> TrainAdvance:
    """Advance past dev2 on the receipts stored under *state_path*."""
    return advance_train(
        train_on_dev2(),
        current=baked_dev2(),
        config=dev2_config(),
        receipts=read_checkpoint_receipts(state_path, DEV2_KEY),
        now=NOW,
    )


def test_dev2_profile_requires_twelve_gates_including_epoch1_stabilization() -> None:
    """The count the advance judges is the authored dev2 profile, not a fixture's."""
    required = dev2_config().gates.required

    assert len(required) == 12
    assert ReleaseGateName.EPOCH1_STABILIZATION in required


def test_advance_train_refuses_dev2_with_eleven_of_twelve_receipts(tmp_path: Path) -> None:
    """dev2 with every gate but ``epoch1_stabilization`` stored does not move."""
    state_path = tmp_path / ".ea" / "state.json"
    eleven = [
        gate
        for gate in dev2_config().gates.required
        if gate is not ReleaseGateName.EPOCH1_STABILIZATION
    ]
    store_receipts(state_path, gate_receipts(release_key=DEV2_KEY, gates=eleven))

    assert len(read_checkpoint_receipts(state_path, DEV2_KEY)) == 11
    with pytest.raises(TrainAdvanceError) as excinfo:
        advance_dev2(state_path)

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_MISSING
    assert excinfo.value.gate is ReleaseGateName.EPOCH1_STABILIZATION
    assert excinfo.value.release_key == DEV2_KEY
    assert read_train_advances(state_path) == ()


def test_advance_train_accepts_dev2_with_twelve_receipts_and_records_it(
    tmp_path: Path,
) -> None:
    """All twelve dev2 receipts open dev3, and the recorded row moves the derived train."""
    state_path = tmp_path / ".ea" / "state.json"
    required = dev2_config().gates.required
    store_receipts(state_path, gate_receipts(release_key=DEV2_KEY, gates=required))

    result = advance_dev2(state_path)
    record_train_advance(state_path, result.record, recorded_at=NOW, summary="advance dev2")

    assert result.record.closed_key == DEV2_KEY
    assert result.record.opened_key == DEV3_KEY
    assert len(result.receipt_refs) == 12
    assert read_train_advances(state_path) == (result.record,)
    moved = derive_train(
        V07_TRAIN,
        recorded_keys=("REL-0.7.0.dev1", DEV2_KEY),
        advances=read_train_advances(state_path),
    )
    assert moved.current_checkpoint.release_key == DEV3_KEY


def test_advance_train_refuses_dev2_receipts_bound_to_other_source(tmp_path: Path) -> None:
    """Twelve receipts earned at a different commit do not vouch for dev2."""
    state_path = tmp_path / ".ea" / "state.json"
    store_receipts(
        state_path,
        gate_receipts(
            release_key=DEV2_KEY, source_sha="d" * 40, gates=dev2_config().gates.required
        ),
    )

    with pytest.raises(TrainAdvanceError) as excinfo:
        advance_dev2(state_path)

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE
