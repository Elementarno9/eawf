"""A terminal record ends up in exactly one canonical place, crash or not.

The property is asserted by actually crashing. Each generated scenario
runs the transaction with a fault injected at one of its four durable
boundaries, leaves the half-written tree on disk, runs recovery over it,
and then asserts the record resolves to exactly one tier. The derived
index is asserted separately: it is not canonical, so recovery has to
make it agree with the ledger rather than the other way round.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.state.epoch2 import Task, TaskPriority, TaskStatus
from eawf.kernel.state.epoch2.task import TERMINAL_TASK_STATUSES
from eawf.kernel.store.compaction import (
    CompactionCrashError,
    CompactionCrashPoint,
    RecordInTwoPlacesError,
    RecordLocation,
    compact_terminal_record,
    compact_terminal_task,
    locate_record,
    read_document,
    recover_store_tree,
    write_document,
)
from eawf.kernel.store.index import regenerate_indexes
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.paths import index_dir, ledger_path
from eawf.kernel.store.tiers import Epoch2Collection

pytestmark = pytest.mark.property

_RECORDED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_BINDING = {
    "head_sha": "a" * 40,
    "tree_sha": "b" * 40,
    "contract_digest": f"sha256:{'c' * 64}",
    "policy_revision": 1,
    "evidence_digest": f"sha256:{'d' * 64}",
}

#: Terminal statuses, sorted so a generated scenario is reproducible.
TERMINAL_STATUSES: tuple[TaskStatus, ...] = tuple(
    sorted(TERMINAL_TASK_STATUSES, key=lambda status: status.value)
)

_task_keys = st.integers(min_value=1, max_value=9999).map(lambda ordinal: f"EAWF-{ordinal:04d}")


def _criterion() -> CriterionSpec:
    return CriterionSpec(
        id="CR-01",
        text="the terminal record lands in exactly one place",
        kind="deterministic",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension="reliability",
        measurable_signal="uv run pytest tests/property/kernel/store",
    )


def _task(key: str, status: TaskStatus) -> Task:
    return Task(
        uid=UUID(int=abs(hash(key)) % (2**128)),
        key=key,
        urn=f"eawf://WS/PRJ-EAWF/REPO/task/{key}",
        origin={"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        revision=1,
        created_at=_RECORDED_AT,
        updated_at=_RECORDED_AT,
        batch_ref="eawf://WS/PRJ-EAWF/REPO/batch/BAT-0001",
        priority=TaskPriority.P1,
        due_scope="eawf://WS/PRJ-EAWF/REPO/milestone/MLS-0001",
        intent=f"deliver {key}",
        contract_revision=1,
        criteria=(_criterion(),),
        status=status,
        integrated_binding=_BINDING if status is TaskStatus.COMPLETED else None,
    )


def _seed_tree(root: Path, tasks: tuple[Task, ...]) -> Path:
    """Write a staging tree whose document holds *tasks* in flight."""
    state_path = root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": "2.0",
        "task": {task.key: task.model_dump(mode="json") for task in tasks},
        "track": {"TRK-RUNTIME": {"key": "TRK-RUNTIME"}},
    }
    write_document(state_path, document)
    return state_path


def _index_bytes(state_path: Path) -> dict[str, bytes]:
    directory = index_dir(state_path)
    if not directory.exists():
        return {}
    return {item.name: item.read_bytes() for item in sorted(directory.iterdir())}


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    key=_task_keys,
    status=st.sampled_from(TERMINAL_STATUSES),
    crash_at=st.one_of(st.none(), st.sampled_from(tuple(CompactionCrashPoint))),
    siblings=st.lists(_task_keys, min_size=0, max_size=3, unique=True),
)
def test_record_is_in_exactly_one_place_after_recovery(
    tmp_path_factory: pytest.TempPathFactory,
    key: str,
    status: TaskStatus,
    crash_at: CompactionCrashPoint | None,
    siblings: list[str],
) -> None:
    """Whatever boundary the process dies at, recovery leaves one copy."""
    root = tmp_path_factory.mktemp("tree")
    tasks = (
        _task(key, status),
        *(_task(sibling, TaskStatus.RUNNING) for sibling in siblings if sibling != key),
    )
    state_path = _seed_tree(root, tasks)

    with contextlib.suppress(CompactionCrashError):
        compact_terminal_task(
            state_path, task=tasks[0], recorded_at=_RECORDED_AT, crash_at=crash_at
        )

    recover_store_tree(state_path)

    location = locate_record(state_path, collection=Epoch2Collection.TASK, record_key=key)
    crashed_before_append = crash_at is CompactionCrashPoint.BEFORE_LEDGER_APPEND
    assert location is (RecordLocation.DOCUMENT if crashed_before_append else RecordLocation.LEDGER)

    document = read_document(state_path)
    for sibling in tasks[1:]:
        assert sibling.key in document["task"]


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    key=_task_keys,
    status=st.sampled_from(TERMINAL_STATUSES),
    crash_at=st.sampled_from(tuple(CompactionCrashPoint)),
)
def test_index_agrees_with_the_ledger_after_recovery(
    tmp_path_factory: pytest.TempPathFactory,
    key: str,
    status: TaskStatus,
    crash_at: CompactionCrashPoint,
) -> None:
    """Recovery rebuilds the derived tier to whatever the ledger says."""
    root = tmp_path_factory.mktemp("tree")
    task = _task(key, status)
    state_path = _seed_tree(root, (task,))

    with contextlib.suppress(CompactionCrashError):
        compact_terminal_task(state_path, task=task, recorded_at=_RECORDED_AT, crash_at=crash_at)

    recover_store_tree(state_path)
    after_recovery = _index_bytes(state_path)
    if index_dir(state_path).exists():
        shutil.rmtree(index_dir(state_path))
    regenerate_indexes(state_path)

    assert after_recovery == _index_bytes(state_path)


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_terminal_task_lands_in_task_jsonl_and_leaves_the_document(
    tmp_path: Path, status: TaskStatus
) -> None:
    """The uninterrupted transaction: one file gains it, the other loses it."""
    task = _task("EAWF-0042", status)
    state_path = _seed_tree(tmp_path, (task,))

    result = compact_terminal_task(state_path, task=task, recorded_at=_RECORDED_AT)

    ledger = ledger_path(state_path, Epoch2Collection.TASK)
    assert ledger.name == "task.jsonl"
    records = read_ledger_records(ledger)
    assert [record.record_key for record in records] == ["EAWF-0042"]
    assert records[0].status == status.value
    assert read_document(state_path)["task"] == {}
    assert result.document_rows_remaining == 0
    assert (
        locate_record(state_path, collection=Epoch2Collection.TASK, record_key="EAWF-0042")
        is RecordLocation.LEDGER
    )


@pytest.mark.parametrize("crash_at", tuple(CompactionCrashPoint))
def test_each_crash_boundary_leaves_a_recoverable_tree(
    tmp_path: Path, crash_at: CompactionCrashPoint
) -> None:
    """Every injected boundary really fires and really recovers."""
    task = _task("EAWF-0042", TaskStatus.FAILED)
    state_path = _seed_tree(tmp_path, (task,))

    with pytest.raises(CompactionCrashError, match=crash_at.value):
        compact_terminal_task(state_path, task=task, recorded_at=_RECORDED_AT, crash_at=crash_at)

    recover_store_tree(state_path)

    expected = (
        RecordLocation.DOCUMENT
        if crash_at is CompactionCrashPoint.BEFORE_LEDGER_APPEND
        else RecordLocation.LEDGER
    )
    located = locate_record(state_path, collection=Epoch2Collection.TASK, record_key="EAWF-0042")
    assert located is expected


def test_a_crash_between_the_two_writes_is_visible_before_recovery(
    tmp_path: Path,
) -> None:
    """The interrupted state is real: both tiers hold it until recovery runs."""
    task = _task("EAWF-0042", TaskStatus.CANCELLED)
    state_path = _seed_tree(tmp_path, (task,))

    with pytest.raises(CompactionCrashError):
        compact_terminal_task(
            state_path,
            task=task,
            recorded_at=_RECORDED_AT,
            crash_at=CompactionCrashPoint.AFTER_LEDGER_APPEND,
        )

    with pytest.raises(RecordInTwoPlacesError):
        locate_record(state_path, collection=Epoch2Collection.TASK, record_key="EAWF-0042")

    report = recover_store_tree(state_path)
    assert report.document_rows_dropped == ("task/EAWF-0042",)


def test_recovery_repairs_a_torn_ledger_tail(tmp_path: Path) -> None:
    """A crash mid-append leaves the record in the document alone."""
    task = _task("EAWF-0042", TaskStatus.FAILED)
    state_path = _seed_tree(tmp_path, (task,))
    ledger = ledger_path(state_path, Epoch2Collection.TASK)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b'{"collection":"task","record_key":"EAWF-0042"')

    report = recover_store_tree(state_path)

    assert report.repaired_ledgers == (Epoch2Collection.TASK,)
    assert (
        locate_record(state_path, collection=Epoch2Collection.TASK, record_key="EAWF-0042")
        is RecordLocation.DOCUMENT
    )


def test_recovery_of_an_untouched_tree_changes_nothing(tmp_path: Path) -> None:
    """Recovery over a healthy tree repairs nothing and drops nothing."""
    task = _task("EAWF-0042", TaskStatus.RUNNING)
    state_path = _seed_tree(tmp_path, (task,))
    before = state_path.read_bytes()

    report = recover_store_tree(state_path)

    assert report.repaired_ledgers == ()
    assert report.document_rows_dropped == ()
    assert state_path.read_bytes() == before


def test_recovery_is_idempotent(tmp_path: Path) -> None:
    """Running recovery twice finds nothing left to finish."""
    task = _task("EAWF-0042", TaskStatus.FAILED)
    state_path = _seed_tree(tmp_path, (task,))
    with pytest.raises(CompactionCrashError):
        compact_terminal_task(
            state_path,
            task=task,
            recorded_at=_RECORDED_AT,
            crash_at=CompactionCrashPoint.AFTER_LEDGER_APPEND,
        )
    recover_store_tree(state_path)

    second = recover_store_tree(state_path)

    assert second.document_rows_dropped == ()
    assert second.repaired_ledgers == ()


def test_compaction_refuses_a_task_still_in_flight(tmp_path: Path) -> None:
    """An unfinished Task would commit a row that is about to change."""
    task = _task("EAWF-0042", TaskStatus.RUNNING)
    state_path = _seed_tree(tmp_path, (task,))

    with pytest.raises(ValueError, match="compaction admits"):
        compact_terminal_task(state_path, task=task, recorded_at=_RECORDED_AT)
    assert not ledger_path(state_path, Epoch2Collection.TASK).exists()


def test_compaction_refuses_a_record_the_document_does_not_hold(tmp_path: Path) -> None:
    """Appending a row that never was in flight would duplicate history."""
    state_path = _seed_tree(tmp_path, ())
    record = LedgerRecord(
        collection=Epoch2Collection.TASK,
        record_key="EAWF-0042",
        status="COMPLETED",
        recorded_at=_RECORDED_AT,
    )

    with pytest.raises(KeyError, match="EAWF-0042"):
        compact_terminal_record(state_path, record=record)
    assert not ledger_path(state_path, Epoch2Collection.TASK).exists()


def test_compaction_refuses_a_collection_that_is_not_a_ledger(tmp_path: Path) -> None:
    """A document collection never compacts, because it never terminates."""
    state_path = _seed_tree(tmp_path, ())
    record = LedgerRecord(
        collection=Epoch2Collection.TRACK,
        record_key="TRK-RUNTIME",
        status="ACTIVE",
        recorded_at=_RECORDED_AT,
    )

    with pytest.raises(ValueError, match="never compacts"):
        compact_terminal_record(state_path, record=record)


def test_locate_reports_absent_for_an_unknown_key(tmp_path: Path) -> None:
    """A key in neither tier is absent, not a failure."""
    state_path = _seed_tree(tmp_path, ())

    assert (
        locate_record(state_path, collection=Epoch2Collection.TASK, record_key="EAWF-9999")
        is RecordLocation.ABSENT
    )


def test_locate_refuses_a_record_held_in_both_tiers(tmp_path: Path) -> None:
    """Two canonical copies is the state recovery exists to end."""
    task = _task("EAWF-0042", TaskStatus.FAILED)
    state_path = _seed_tree(tmp_path, (task,))
    append_ledger_record(
        ledger_path(state_path, Epoch2Collection.TASK),
        LedgerRecord(
            collection=Epoch2Collection.TASK,
            record_key="EAWF-0042",
            status="FAILED",
            recorded_at=_RECORDED_AT,
        ),
    )

    with pytest.raises(RecordInTwoPlacesError, match="task/EAWF-0042"):
        locate_record(state_path, collection=Epoch2Collection.TASK, record_key="EAWF-0042")


def test_read_document_refuses_a_json_array(tmp_path: Path) -> None:
    """A document that is not an object holds no collections."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="not a JSON object"):
        read_document(state_path)


def test_read_document_refuses_a_missing_tree(tmp_path: Path) -> None:
    """A tree with no document cannot be compacted or recovered."""
    with pytest.raises(FileNotFoundError):
        read_document(tmp_path / ".ea" / "state.json")


def test_compaction_refuses_a_collection_holding_a_json_array(tmp_path: Path) -> None:
    """A collection slot that is not an object is a defect, not empty."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"task": []}), encoding="utf-8")
    record = LedgerRecord(
        collection=Epoch2Collection.TASK,
        record_key="EAWF-0042",
        status="FAILED",
        recorded_at=_RECORDED_AT,
    )

    with pytest.raises(ValueError, match="not an object"):
        compact_terminal_record(state_path, record=record)
