"""The open rung is read off the stores, not off a constant.

The train's source declaration always stands at its first rung, so an
index read from it says ``dev1`` long after ``dev1`` was burned and
``dev2`` baked. These tests pin the derivation that replaces it: the open
rung is the later of the highest rung carrying a record and the rung
after the last recorded advance. They also pin that ``release.show``
reports that derived rung, both as the index and as the checkpoint it
describes when no version is named.

The two facts are exercised apart and together, because each covers a
case the other misses. A checkpoint burned to ``partially_released``
never advances, but its successor's record moves the train past it. A
baked checkpoint that advanced has no successor record until
``release create`` opens one.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from eawf.kernel.spec.release import Release, ReleaseStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import show
from eawf.workflow.release.adoption import adopt_publication
from eawf.workflow.release.advance import (
    TrainAdvanceRecord,
    derive_train,
    open_rung_index,
    render_train_ladder,
)
from eawf.workflow.release.publication import burn_release
from eawf.workflow.release.records import record_release
from eawf.workflow.release.train import V07_TRAIN
from eawf.workflow.release.train_store import (
    read_train_advances,
    record_train_advance,
    train_advances_path,
)
from tests._release_helpers import NOW, dev1_adoption, dev1_config, dev1_draft, release_record

DEV1_KEY = "REL-0.7.0.dev1"
DEV2_KEY = "REL-0.7.0.dev2"
DEV3_KEY = "REL-0.7.0.dev3"

#: The receipt refs the dev2 advance carries; the derivation reads only
#: the keys, so any refs do.
DEV2_REFS = ("checkpoint-receipt://REL-0.7.0.dev2/migration/20260917T120000.000000Z",)


def dev2_advance(**overrides: object) -> TrainAdvanceRecord:
    """Return the recorded advance past the dev2 checkpoint."""
    payload: dict[str, object] = {
        "train_id": V07_TRAIN.train_id,
        "closed_key": DEV2_KEY,
        "closed_revision": 9,
        "opened_key": DEV3_KEY,
        "receipt_refs": DEV2_REFS,
        "advanced_at": NOW,
        "train_revision": 1,
    }
    payload.update(overrides)
    return TrainAdvanceRecord.model_validate(payload)


def burned_dev1() -> Release:
    """Return dev1 as its burn left it: partially released."""
    adopted = adopt_publication(dev1_draft(), dev1_config(), adoption=dev1_adoption())
    burned, _operation = burn_release(adopted, dev1_config(), None)
    return burned


def dev2_at(status: ReleaseStatus) -> Release:
    """Return the dev2 record at *status*."""
    return release_record(
        key=DEV2_KEY,
        version="0.7.0.dev2",
        status=status,
        approval_ref="receipt://approval/dev2",
    )


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a state root whose stores hold burned dev1 and approved dev2."""
    path = tmp_path / ".ea" / "state.json"
    for record in (burned_dev1(), dev2_at(ReleaseStatus.APPROVED)):
        record_release(path, record, recorded_at=NOW, summary=f"seed {record.key}")
    return path


def context(state_path: Path | None) -> MethodContext:
    """Return a method context bound to *state_path*."""
    return MethodContext(
        started_at="2026-09-17T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.7.0.dev2",
        state_path=state_path,
    )


# --- open_rung_index ---------------------------------------------------------


def test_open_rung_index_is_the_first_rung_when_nothing_is_recorded() -> None:
    """An empty store leaves the train where source declares it."""
    assert open_rung_index(V07_TRAIN, recorded_keys=(), advances=()) == 0


def test_open_rung_index_stays_on_a_burned_rung_with_no_successor() -> None:
    """A burned dev1 with nothing above it is still the highest rung on record."""
    assert open_rung_index(V07_TRAIN, recorded_keys=(DEV1_KEY,), advances=()) == 0


def test_open_rung_index_reads_the_successor_record_past_a_burned_rung() -> None:
    """dev1 partially released plus dev2 approved puts the train on dev2."""
    assert open_rung_index(V07_TRAIN, recorded_keys=(DEV1_KEY, DEV2_KEY), advances=()) == 1


def test_open_rung_index_reads_the_rung_after_a_recorded_advance() -> None:
    """A recorded dev2 advance puts the train on dev3 before dev3 has a record."""
    index = open_rung_index(
        V07_TRAIN, recorded_keys=(DEV1_KEY, DEV2_KEY), advances=(dev2_advance(),)
    )

    assert index == 2
    assert V07_TRAIN.checkpoints[index].release_key == DEV3_KEY


def test_open_rung_index_takes_the_later_of_the_two_facts() -> None:
    """A record above the last advance wins over the advance."""
    dev1_advance = dev2_advance(closed_key=DEV1_KEY, opened_key=DEV2_KEY)

    index = open_rung_index(
        V07_TRAIN, recorded_keys=(DEV1_KEY, DEV2_KEY, DEV3_KEY), advances=(dev1_advance,)
    )

    assert index == 2


def test_open_rung_index_reaches_the_last_rung() -> None:
    """A record on the stable rung puts the train on its last index."""
    stable = V07_TRAIN.checkpoints[-1].release_key

    index = open_rung_index(V07_TRAIN, recorded_keys=(stable,), advances=())

    assert index == len(V07_TRAIN.checkpoints) - 1


def test_open_rung_index_skips_records_of_another_train() -> None:
    """A key this ladder does not declare belongs to someone else's train."""
    assert open_rung_index(V07_TRAIN, recorded_keys=("REL-0.8.0.dev1",), advances=()) == 0


def test_open_rung_index_skips_advances_of_another_train() -> None:
    """An advance recorded on another train does not move this one."""
    foreign = dev2_advance(train_id="TRAIN-0.8.0")

    assert open_rung_index(V07_TRAIN, recorded_keys=(DEV1_KEY,), advances=(foreign,)) == 0


def test_open_rung_index_refuses_an_advance_onto_an_undeclared_rung() -> None:
    """An advance of this train naming a rung it lacks is a corrupt store."""
    stray = dev2_advance(opened_key="REL-0.7.0.dev9")

    with pytest.raises(ValueError, match="does not declare"):
        open_rung_index(V07_TRAIN, recorded_keys=(), advances=(stray,))


# --- derive_train ------------------------------------------------------------


def test_derive_train_counts_one_revision_per_advance() -> None:
    """The derived train is as many revisions on as it has advances."""
    train = derive_train(V07_TRAIN, recorded_keys=(DEV1_KEY, DEV2_KEY), advances=(dev2_advance(),))

    assert train.current_checkpoint_index == 2
    assert train.revision == V07_TRAIN.revision + 1
    assert train.gate_receipt_refs[DEV2_KEY] == DEV2_REFS


def test_derive_train_marks_the_rungs_below_the_open_one_passed() -> None:
    """The rendered ladder reads passed, passed, open, then pending."""
    train = derive_train(V07_TRAIN, recorded_keys=(DEV1_KEY, DEV2_KEY), advances=(dev2_advance(),))

    statuses = [row["status"] for row in render_train_ladder(train)["checkpoints"]]
    assert statuses == ["passed", "passed", "open", *["pending"] * 5]


def test_derive_train_leaves_the_source_declaration_untouched() -> None:
    """Deriving a position never moves the declared train."""
    derive_train(V07_TRAIN, recorded_keys=(DEV1_KEY, DEV2_KEY), advances=(dev2_advance(),))

    assert V07_TRAIN.current_checkpoint_index == 0
    assert V07_TRAIN.revision == 0


# --- release.show ------------------------------------------------------------


def test_show_reports_the_successor_of_a_burned_rung_as_open(state_path: Path) -> None:
    """Before any advance, show reports index 1 and describes dev2."""
    result = asyncio.run(show(context(state_path), {}))

    assert result["current_checkpoint_index"] == 1
    assert result["checkpoint"]["release_key"] == DEV2_KEY
    assert result["record"]["status"] == ReleaseStatus.APPROVED.value


def test_show_reports_the_rung_after_a_recorded_advance(state_path: Path) -> None:
    """After the dev2 advance, show reports index 2 and a dev3 never opened."""
    record_train_advance(state_path, dev2_advance(), recorded_at=NOW, summary="advance dev2")

    result = asyncio.run(show(context(state_path), {"version": None}))

    assert result["current_checkpoint_index"] == 2
    assert result["checkpoint"]["release_key"] == DEV3_KEY
    assert result["record"] is None


def test_show_of_a_named_version_still_reports_the_derived_index(state_path: Path) -> None:
    """Naming a rung changes the rung described, not the open index."""
    result = asyncio.run(show(context(state_path), {"version": "0.7.0.dev1"}))

    assert result["current_checkpoint_index"] == 1
    assert result["checkpoint"]["release_key"] == DEV1_KEY
    assert result["record"]["status"] == ReleaseStatus.PARTIALLY_RELEASED.value


def test_show_without_a_state_root_reports_the_declared_first_rung() -> None:
    """With nothing to read, show describes the ladder as source declares it."""
    result = asyncio.run(show(context(None), {}))

    assert result["current_checkpoint_index"] == 0
    assert result["checkpoint"]["release_key"] == DEV1_KEY
    assert result["record"] is None


def test_show_refuses_a_corrupt_advance_collection(state_path: Path) -> None:
    """An unreadable advance row must not read as a train that never moved."""
    path = train_advances_path(state_path)
    path.write_text('{"not": "an envelope"}\n', encoding="utf-8")

    with pytest.raises(DaemonValidationError, match="is not an envelope"):
        asyncio.run(show(context(state_path), {}))


# --- the advance collection --------------------------------------------------


def test_read_train_advances_returns_rows_in_write_order(state_path: Path) -> None:
    """Two advances read back in the order they were recorded."""
    first = dev2_advance(closed_key=DEV1_KEY, opened_key=DEV2_KEY)
    second = dev2_advance(advanced_at=NOW + timedelta(days=1), train_revision=2)
    for row in (first, second):
        record_train_advance(state_path, row, recorded_at=row.advanced_at, summary="advance")

    assert read_train_advances(state_path) == (first, second)


def test_read_train_advances_is_empty_before_the_first_advance(tmp_path: Path) -> None:
    """A train that never moved has no advance collection yet."""
    assert read_train_advances(tmp_path / ".ea" / "state.json") == ()


def test_read_train_advances_refuses_a_row_of_another_kind(state_path: Path) -> None:
    """A record row filed into the advance collection is refused, not skipped."""
    records = state_path.parent / "store" / "release_record.jsonl"
    train_advances_path(state_path).write_text(records.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="is filed under 'release_record'"):
        read_train_advances(state_path)


def test_record_train_advance_refuses_a_naive_instant(state_path: Path) -> None:
    """An advance whose instant carries no zone cannot be ordered."""
    with pytest.raises(ValueError, match="timezone-aware"):
        record_train_advance(
            state_path, dev2_advance(), recorded_at=NOW.replace(tzinfo=None), summary="naive"
        )
