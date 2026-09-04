"""The next checkpoint is a new record; the prior one is never rewritten.

A train that mutated ``dev1`` into ``dev2`` would let a published
version be retroactively re-pointed at different source, which is the
one thing the immutable-record design exists to prevent. These tests pin
that the advance mints a distinct DRAFT record with its own identity and
its own revision, that the closing record's status, manifest digest and
receipts read identically before and after, and that the ladder renderer
behind ``eawf release train show --json`` reports the index and every
checkpoint's status.
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.release import ReleaseStatus
from eawf.workflow.release.advance import (
    CheckpointLadderStatus,
    ladder_status,
    render_train_ladder,
    render_train_ladder_text,
)
from eawf.workflow.release.train import V07_TRAIN
from tests.unit.kernel.release.test_train_advance import (
    NEXT_UID,
    advance,
    finished,
    gate_receipts,
)

#: The JSON keys ``eawf release train show --json`` promises.
LADDER_KEYS = frozenset(
    {
        "train_id",
        "target_version",
        "current_checkpoint_index",
        "current_checkpoint",
        "revision",
        "checkpoints",
    }
)


# --- the record the advance opens --------------------------------------------


def test_the_opened_checkpoint_is_the_next_rung_in_draft() -> None:
    """dev2 opens as a DRAFT record keyed for its own version."""
    opened = advance(finished()).opened

    assert opened.key == "REL-0.7.0.dev2"
    assert opened.version == "0.7.0.dev2"
    assert opened.status is ReleaseStatus.DRAFT


def test_the_opened_checkpoint_starts_at_its_own_revision() -> None:
    """A new rung is revision 0, not a continuation of the prior count."""
    result = advance(finished())

    assert result.closed.revision == 0
    assert result.opened.revision == 0
    assert result.opened.uid == NEXT_UID
    assert result.opened.uid != result.closed.uid


def test_the_opened_checkpoint_inherits_no_pin() -> None:
    """dev2 carries none of dev1's source or manifest binding."""
    opened = advance(finished()).opened

    assert opened.source_sha is None
    assert opened.source_tree_sha is None
    assert opened.manifest_ref is None
    assert opened.manifest_digest is None


# --- the record the advance closes -------------------------------------------


def test_the_closing_record_is_returned_untouched() -> None:
    """Every field of the dev1 record reads identically after the advance."""
    dev1 = finished()
    before = dev1.model_dump(mode="json")

    result = advance(dev1)

    assert result.closed is dev1
    assert result.closed.model_dump(mode="json") == before


def test_the_closing_record_keeps_its_status_and_manifest_digest() -> None:
    """The three fields a rewrite would have moved are pinned explicitly."""
    dev1 = finished()

    result = advance(dev1)

    assert result.closed.status is ReleaseStatus.BAKED
    assert result.closed.manifest_digest == dev1.manifest_digest
    assert result.closed.source_sha == dev1.source_sha


def test_the_prior_receipts_are_recorded_not_consumed() -> None:
    """The receipts the advance read are unchanged and land on the train."""
    receipts = gate_receipts()
    before = [receipt.model_dump(mode="json") for receipt in receipts]

    result = advance(finished(), receipts=receipts)

    assert [receipt.model_dump(mode="json") for receipt in receipts] == before
    assert result.train.gate_receipt_refs["REL-0.7.0.dev1"] == result.receipt_refs


# --- the rendered ladder -----------------------------------------------------


def test_the_ladder_payload_carries_the_promised_keys() -> None:
    """The JSON body names the train, the index and the checkpoints."""
    payload = render_train_ladder(V07_TRAIN)

    assert set(payload) == LADDER_KEYS
    assert payload["train_id"] == "TRAIN-0.7.0"
    assert payload["current_checkpoint_index"] == 0
    assert payload["current_checkpoint"] == "REL-0.7.0.dev1"


def test_every_checkpoint_row_carries_a_status() -> None:
    """The ladder reports one status per rung, none omitted."""
    rows = render_train_ladder(V07_TRAIN)["checkpoints"]

    assert len(rows) == len(V07_TRAIN.checkpoints)
    assert [row["status"] for row in rows] == ["open", *["pending"] * 6]
    assert [row["release_key"] for row in rows] == [
        rung.release_key for rung in V07_TRAIN.checkpoints
    ]


def test_the_advanced_ladder_marks_the_prior_rung_passed() -> None:
    """After the advance dev1 reads passed and dev2 reads open."""
    payload = render_train_ladder(advance(finished()).train)

    assert payload["current_checkpoint_index"] == 1
    assert payload["current_checkpoint"] == "REL-0.7.0.dev2"
    assert [row["status"] for row in payload["checkpoints"]] == [
        "passed",
        "open",
        *["pending"] * 5,
    ]


def test_the_advanced_ladder_carries_the_prior_receipt_refs() -> None:
    """The rung that closed shows the receipts that let it close."""
    result = advance(finished())

    rows = render_train_ladder(result.train)["checkpoints"]

    assert rows[0]["gate_receipt_refs"] == list(result.receipt_refs)
    assert rows[1]["gate_receipt_refs"] == []


def test_the_text_ladder_marks_only_the_open_rung() -> None:
    """The human rendering carries exactly one open marker."""
    lines = render_train_ladder_text(V07_TRAIN).splitlines()

    assert lines[0] == "TRAIN-0.7.0 -> 0.7.0"
    assert len([line for line in lines if line.startswith(" *")]) == 1
    assert "REL-0.7.0.dev1" in lines[1]


# --- the ladder-status boundary ----------------------------------------------


@pytest.mark.parametrize(
    ("index", "expected"),
    [
        (0, CheckpointLadderStatus.OPEN),
        (1, CheckpointLadderStatus.PENDING),
        (6, CheckpointLadderStatus.PENDING),
    ],
)
def test_ladder_status_reads_the_open_index(index: int, expected: CheckpointLadderStatus) -> None:
    """The first, second and last rungs each read correctly."""
    assert ladder_status(V07_TRAIN, index) is expected


@pytest.mark.parametrize("index", [-1, 7])
def test_ladder_status_refuses_an_index_off_the_ladder(index: int) -> None:
    """One past either end is an error, not a silent pending."""
    with pytest.raises(IndexError, match="off a ladder"):
        ladder_status(V07_TRAIN, index)
