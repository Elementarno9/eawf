"""REL-011: the train walks forward only from a finished checkpoint.

The index on a :class:`~eawf.kernel.spec.release.ReleaseTrain` is the
project's claim about which checkpoint it is working on, so moving it
from a checkpoint that never shipped would be a claim about work nobody
did. These tests pin that the move happens only from ``baked`` or
``released``, that every other status -- including the two terminal
statuses that mean *abandoned* -- denies ``checkpoint_not_terminal``,
and that a denied advance leaves the train's index and the closing
record exactly as they were.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from eawf.kernel.spec.release import (
    Release,
    ReleaseStatus,
    ReleaseTrain,
    validate_release_against_train,
)
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.workflow.release.advance import (
    ADVANCING_STATUSES,
    CheckpointGateReceipt,
    TrainAdvance,
    TrainAdvanceDenialCode,
    TrainAdvanceError,
    advance_train,
    assert_checkpoint_terminal,
    draft_release_for,
)
from eawf.workflow.release.train import V07_TRAIN
from tests._release_helpers import (
    MANIFEST_DIGEST,
    NOW,
    SOURCE_SHA,
    dev1_config,
    release_record,
)

#: Identity minted for the record the advance opens.
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
) -> list[CheckpointGateReceipt]:
    """Return one fresh receipt per required dev1 gate."""
    required = dev1_config().gates.required if gates is None else gates
    return [
        CheckpointGateReceipt(
            gate=gate,
            release_key=release_key,
            source_sha=source_sha,
            manifest_digest=manifest_digest,
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
            receipt_ref=f"receipt://gate/{gate.value}",
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
        next_uid=NEXT_UID,
    )


# --- the advancing statuses --------------------------------------------------


def test_advancing_statuses_are_exactly_baked_and_released() -> None:
    """The advance set is the two shipped terminals, nothing wider."""
    assert frozenset({ReleaseStatus.BAKED, ReleaseStatus.RELEASED}) == ADVANCING_STATUSES


@pytest.mark.parametrize("status", sorted(ADVANCING_STATUSES, key=lambda s: s.value))
def test_advance_train_opens_the_next_rung_from_a_shipped_checkpoint(
    status: ReleaseStatus,
) -> None:
    """A baked or released dev1 moves the index onto dev2."""
    result = advance(finished(status))

    assert result.train.current_checkpoint_index == 1
    assert result.opened.key == "REL-0.7.0.dev2"
    assert result.opened.status is ReleaseStatus.DRAFT


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
            next_uid=NEXT_UID,
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
            next_uid=NEXT_UID,
        )


def test_advance_train_rejects_a_gate_offered_twice() -> None:
    """Two receipts for one gate is an ambiguity, not a pick-one."""
    doubled = [*gate_receipts(), gate_receipts()[0]]

    with pytest.raises(ValueError, match="two receipts offered"):
        advance(finished(), receipts=doubled)


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
