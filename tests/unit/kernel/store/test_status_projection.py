"""The document is one object to readers and two files on disk.

Task definitions, planning and terminal status and the sequence are
committed in ``state.json``; Run rows and an in-flight Task's status
fields sit in the machine-local projection, so a claim leaves the
committed file byte-identical and a fresh clone still holds the backlog.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.io import state_version
from eawf.kernel.store import compaction
from eawf.kernel.store.compaction import (
    IN_FLIGHT_TASK_FIELDS,
    IN_FLIGHT_TASK_STATUSES,
    migrate_status_projection,
    read_document,
    recover_store_tree,
    settle_status_ledgers,
    write_document,
)
from eawf.kernel.store.ledger import LedgerAppendOnlyError
from eawf.kernel.store.paths import ledger_path, seed_ledger_path, status_projection_path
from eawf.kernel.store.tiers import STATUS_PROJECTION_COLLECTIONS, Epoch2Collection

PLANNED_ROW: dict[str, Any] = {
    "key": "EAWF-0001",
    "batch_ref": "urn:eawf:batch:BAT-0001",
    "intent": "ship the thing",
    "criteria": [{"id": "CR-01"}],
    "status": "PLANNED",
    "active_run_ref": None,
    "integrated_binding": None,
    "revision": 2,
    "updated_at": "2026-10-01T00:00:00Z",
}


def _claimed(row: dict[str, Any], *, status: str = "RUNNING") -> dict[str, Any]:
    """Return *row* claimed and in flight."""
    return row | {
        "status": status,
        "active_run_ref": "urn:eawf:run:RUN-00000001",
        "claimed_by": "agent-a",
        "first_claimed_at": "2026-10-01T01:00:00Z",
        "revision": row["revision"] + 2,
        "updated_at": "2026-10-01T02:00:00Z",
    }


def _document(task: dict[str, Any] | None = None, *, sequence: int = 7) -> dict[str, Any]:
    """Return a document carrying committed facts and status side by side."""
    return {
        "schema_version": "2",
        "milestone": {"MLS-0001": {"status": "ACTIVE"}},
        "batch": {"BAT-0001": {"status": "ACTIVE", "task_refs": ["EAWF-0001", "EAWF-0002"]}},
        "task": {
            "EAWF-0001": PLANNED_ROW if task is None else task,
            "EAWF-0002": {"key": "EAWF-0002", "status": "DRAFT", "revision": 1},
        },
        "run": {"RUN-00000001": {"status": "RUNNING"}},
        "canonical_sequence": sequence,
    }


def _raw(path: Path) -> dict[str, Any]:
    """Return a file's own decoded object, nothing merged in."""
    return json.loads(path.read_text("utf-8"))


def _fresh_clone(state_path: Path) -> None:
    """Drop everything a clone would not check out."""
    status_projection_path(state_path).unlink()


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a generation's document path."""
    generation = tmp_path / "gen-0123456789abcdef"
    generation.mkdir()
    return generation / "state.json"


def test_only_runs_are_wholly_local_and_in_flight_is_the_claimed_span() -> None:
    assert {Epoch2Collection.RUN} == STATUS_PROJECTION_COLLECTIONS
    assert {status.value for status in IN_FLIGHT_TASK_STATUSES} == {
        "CLAIMED",
        "RUNNING",
        "READY_TO_INTEGRATE",
    }
    assert "intent" not in IN_FLIGHT_TASK_FIELDS
    assert {"status", "claimed_by", "active_run_ref"} <= IN_FLIGHT_TASK_FIELDS


def test_write_splits_and_read_merges(state_path: Path) -> None:
    document = _document(_claimed(PLANNED_ROW))
    write_document(state_path, document)
    committed = _raw(state_path)
    assert set(committed) == {"schema_version", "milestone", "batch", "task", "canonical_sequence"}
    assert committed["task"]["EAWF-0001"]["status"] == "PLANNED"
    assert "claimed_by" not in committed["task"]["EAWF-0001"]
    local = _raw(status_projection_path(state_path))["status"]
    assert set(local) == {"run", "task", "canonical_sequence"}
    assert set(local["task"]) == {"EAWF-0001"}
    assert read_document(state_path) == document


def test_a_claim_leaves_the_committed_file_untouched(state_path: Path) -> None:
    write_document(state_path, _document())
    before, inode = state_path.read_bytes(), state_path.stat().st_ino
    for status in ("CLAIMED", "RUNNING", "READY_TO_INTEGRATE"):
        document = _document(_claimed(PLANNED_ROW, status=status), sequence=9)
        write_document(state_path, document)
        assert state_path.read_bytes() == before
        assert state_path.stat().st_ino == inode
        assert read_document(state_path) == document


@pytest.mark.parametrize("terminal", ["COMPLETED", "CANCELLED", "FAILED"])
def test_a_terminal_status_is_committed_and_leaves_no_local_entry(
    state_path: Path, terminal: str
) -> None:
    write_document(state_path, _document(_claimed(PLANNED_ROW)))
    done = _claimed(PLANNED_ROW) | {"status": terminal}
    write_document(state_path, _document(done, sequence=8))
    assert _raw(state_path)["task"]["EAWF-0001"] == done
    assert _raw(state_path)["canonical_sequence"] == 8
    assert "task" not in _raw(status_projection_path(state_path))["status"]


def test_a_definition_edit_in_flight_is_committed_under_the_planning_status(
    state_path: Path,
) -> None:
    write_document(state_path, _document())
    edited = _claimed(PLANNED_ROW) | {"intent": "ship it better"}
    write_document(state_path, _document(edited))
    assert _raw(state_path)["task"]["EAWF-0001"] == PLANNED_ROW | {"intent": "ship it better"}
    assert read_document(state_path)["task"]["EAWF-0001"] == edited


def test_a_task_claimed_with_no_committed_base_commits_as_planned(state_path: Path) -> None:
    write_document(state_path, _document(_claimed(PLANNED_ROW)))
    base = _raw(state_path)["task"]["EAWF-0001"]
    assert base["status"] == "PLANNED"
    assert base["active_run_ref"] is None
    assert "claimed_by" not in base


def test_a_fresh_clone_keeps_the_backlog_batches_and_sequence(state_path: Path) -> None:
    write_document(state_path, _document(sequence=7))
    write_document(state_path, _document(_claimed(PLANNED_ROW), sequence=12))
    _fresh_clone(state_path)
    document = read_document(state_path)
    tasks = document["task"]
    assert tasks["EAWF-0002"]["status"] == "DRAFT"
    assert tasks["EAWF-0001"] == PLANNED_ROW
    for batch in document["batch"].values():
        assert set(batch["task_refs"]) <= set(tasks)
    assert document["canonical_sequence"] == 7
    assert "run" not in document


def test_the_overlay_wins_over_the_committed_status_fields(state_path: Path) -> None:
    running = _claimed(PLANNED_ROW)
    write_document(state_path, _document(running))
    assert _raw(state_path)["task"]["EAWF-0001"]["status"] == "PLANNED"
    assert read_document(state_path)["task"]["EAWF-0001"] == running


def test_the_larger_sequence_wins(state_path: Path) -> None:
    write_document(state_path, _document(sequence=7))
    write_document(state_path, _document(_claimed(PLANNED_ROW), sequence=9))
    assert _raw(state_path)["canonical_sequence"] == 7
    assert read_document(state_path)["canonical_sequence"] == 9
    pulled = _raw(state_path) | {"canonical_sequence": 40}
    state_path.write_text(json.dumps(pulled), encoding="utf-8")
    assert read_document(state_path)["canonical_sequence"] == 40


def test_a_lower_sequence_rewrites_the_committed_copy(state_path: Path) -> None:
    write_document(state_path, _document(sequence=9))
    write_document(state_path, _document(sequence=3))
    assert _raw(state_path)["canonical_sequence"] == 3
    assert read_document(state_path)["canonical_sequence"] == 3


def test_a_committed_file_moved_from_outside_keeps_its_terminal_rows(state_path: Path) -> None:
    write_document(state_path, _document(_claimed(PLANNED_ROW)))
    pulled = _raw(state_path)
    pulled["task"]["EAWF-0001"] = _claimed(PLANNED_ROW) | {"status": "COMPLETED"}
    pulled["batch"]["BAT-0001"]["status"] = "COMPLETED"
    state_path.write_text(json.dumps(pulled), encoding="utf-8")
    document = read_document(state_path)
    assert document["task"]["EAWF-0001"]["status"] == "COMPLETED"
    assert document["run"] == {"RUN-00000001": {"status": "RUNNING"}}


def test_a_moved_committed_file_drops_entries_whose_row_is_gone(state_path: Path) -> None:
    write_document(state_path, _document(_claimed(PLANNED_ROW)))
    pulled = _raw(state_path)
    del pulled["task"]["EAWF-0001"]
    state_path.write_text(json.dumps(pulled), encoding="utf-8")
    assert "EAWF-0001" not in read_document(state_path)["task"]


def test_a_dropped_run_collection_does_not_resurrect(state_path: Path) -> None:
    write_document(state_path, _document())
    document = _document()
    del document["run"]
    write_document(state_path, document)
    assert "run" not in read_document(state_path)


def test_an_empty_document_round_trips(state_path: Path) -> None:
    write_document(state_path, {})
    assert _raw(state_path) == {}
    assert _raw(status_projection_path(state_path))["status"] == {}
    assert read_document(state_path) == {}


_WRITES = ("projection", "committed")


@pytest.mark.parametrize("crash_after", [0, 1, 2], ids=["none", "projection", "committed"])
@pytest.mark.parametrize(
    "after_task",
    [
        _claimed(PLANNED_ROW) | {"status": "COMPLETED"},
        _claimed(PLANNED_ROW) | {"intent": "edited in flight"},
        PLANNED_ROW | {"revision": 5},
    ],
    ids=["completion", "definition-edit", "release"],
)
def test_a_crash_at_each_write_reads_as_before_or_after(
    state_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after: int,
    after_task: dict[str, Any],
) -> None:
    before = _document(_claimed(PLANNED_ROW), sequence=7)
    write_document(state_path, before)
    after = _document(after_task, sequence=8)
    del after["run"]["RUN-00000001"]
    real_write = compaction._write_object
    calls: list[Path] = []

    def dying_write(path: Path, payload: bytes) -> None:
        if len(calls) == crash_after:
            raise OSError(f"injected crash before write {len(calls) + 1}")
        calls.append(path)
        real_write(path, payload)

    monkeypatch.setattr(compaction, "_write_object", dying_write)
    if crash_after < len(_WRITES):
        with pytest.raises(OSError, match="injected crash"):
            write_document(state_path, copy.deepcopy(after))
        expected = before
    else:
        write_document(state_path, copy.deepcopy(after))
        expected = after
    assert [path.name for path in calls] == ["status.json", "state.json"][:crash_after]
    monkeypatch.setattr(compaction, "_write_object", real_write)
    assert state_version(read_document(state_path)) == state_version(expected)
    recover_store_tree(state_path)
    assert state_version(read_document(state_path)) == state_version(expected)
    write_document(state_path, after)
    assert read_document(state_path) == after


def test_a_crash_on_the_first_write_of_a_new_tree_leaves_no_document(
    state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = compaction._write_object

    def dying_write(path: Path, payload: bytes) -> None:
        if path == state_path:
            raise OSError("injected crash")
        real_write(path, payload)

    monkeypatch.setattr(compaction, "_write_object", dying_write)
    with pytest.raises(OSError, match="injected crash"):
        write_document(state_path, _document())
    with pytest.raises(FileNotFoundError):
        read_document(state_path)


def _pre_definition_tree(state_path: Path, document: dict[str, Any]) -> None:
    """Lay *document* out the way the split before this one wrote it."""
    keys = {"task", "run", "canonical_sequence"}
    committed = {key: value for key, value in document.items() if key not in keys}
    status = {key: value for key, value in document.items() if key in keys}
    state_path.write_text(json.dumps(committed), encoding="utf-8")
    projection = status_projection_path(state_path)
    projection.parent.mkdir(parents=True)
    projection.write_text(json.dumps(status), encoding="utf-8")


def test_a_pre_definition_tree_reads_whole_and_migrates_once(state_path: Path) -> None:
    document = _document(_claimed(PLANNED_ROW))
    _pre_definition_tree(state_path, document)
    assert read_document(state_path) == document
    assert migrate_status_projection(state_path) is True
    assert set(_raw(state_path)["task"]) == {"EAWF-0001", "EAWF-0002"}
    assert _raw(state_path)["canonical_sequence"] == 7
    assert read_document(state_path) == document
    before = state_path.read_bytes()
    assert migrate_status_projection(state_path) is False
    assert state_path.read_bytes() == before


def test_a_crash_mid_migration_reads_as_before_or_after(
    state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _document(_claimed(PLANNED_ROW))
    _pre_definition_tree(state_path, document)

    real_write = compaction._write_object

    def second_dies(path: Path, payload: bytes) -> None:
        if path == state_path:
            raise OSError("injected crash")
        real_write(path, payload)

    monkeypatch.setattr(compaction, "_write_object", second_dies)
    with pytest.raises(OSError, match="injected crash"):
        migrate_status_projection(state_path)
    monkeypatch.setattr(compaction, "_write_object", real_write)
    assert read_document(state_path) == document
    assert migrate_status_projection(state_path) is True
    assert read_document(state_path) == document


def test_a_pre_split_document_migrates(state_path: Path) -> None:
    document = _document(_claimed(PLANNED_ROW))
    state_path.write_text(json.dumps(document), encoding="utf-8")
    assert read_document(state_path) == document
    recover_store_tree(state_path)
    assert "run" not in _raw(state_path)
    assert _raw(state_path)["task"]["EAWF-0001"]["status"] == "PLANNED"
    assert read_document(state_path) == document


def test_a_stranded_local_task_ledger_moves_to_the_committed_tier(state_path: Path) -> None:
    committed = ledger_path(state_path, Epoch2Collection.TASK)
    assert committed == state_path.parent / "ledger" / "task.jsonl"
    committed.parent.mkdir()
    committed.write_text('{"line": 1}\n', encoding="utf-8")
    stranded = state_path.parent / "local" / "ledger" / "task.jsonl"
    stranded.parent.mkdir(parents=True)
    stranded.write_text('{"line": 1}\n{"line": 2}\n', encoding="utf-8")
    assert settle_status_ledgers(state_path) == (Epoch2Collection.TASK,)
    assert committed.read_text("utf-8") == '{"line": 1}\n{"line": 2}\n'
    assert not stranded.exists()
    assert settle_status_ledgers(state_path) == ()


def test_a_stranded_task_ledger_that_rewrites_history_is_refused(state_path: Path) -> None:
    committed = ledger_path(state_path, Epoch2Collection.TASK)
    committed.parent.mkdir()
    committed.write_text('{"line": 1}\n', encoding="utf-8")
    stranded = state_path.parent / "local" / "ledger" / "task.jsonl"
    stranded.parent.mkdir(parents=True)
    stranded.write_text('{"line": 9}\n', encoding="utf-8")
    with pytest.raises(LedgerAppendOnlyError):
        settle_status_ledgers(state_path)
    assert stranded.exists()


def test_the_run_ledger_resolves_locally_and_others_stay_committed(state_path: Path) -> None:
    generation = state_path.parent
    assert ledger_path(state_path, Epoch2Collection.RUN) == (
        generation / "local" / "ledger" / "run.jsonl"
    )
    assert seed_ledger_path(state_path, Epoch2Collection.RUN) == generation / "ledger" / "run.jsonl"
    assert ledger_path(state_path, Epoch2Collection.BATCH) == generation / "ledger" / "batch.jsonl"


def test_a_committed_run_ledger_seeds_the_local_one_once(state_path: Path) -> None:
    seed = seed_ledger_path(state_path, Epoch2Collection.RUN)
    seed.parent.mkdir()
    seed.write_text('{"line": 1}\n', encoding="utf-8")
    assert settle_status_ledgers(state_path) == (Epoch2Collection.RUN,)
    local = ledger_path(state_path, Epoch2Collection.RUN)
    assert local.read_text("utf-8") == '{"line": 1}\n'
    local.write_text('{"line": 1}\n{"line": 2}\n', encoding="utf-8")
    assert settle_status_ledgers(state_path) == ()
    assert seed.read_text("utf-8") == '{"line": 1}\n'


def test_settling_an_empty_tree_does_nothing(state_path: Path) -> None:
    assert settle_status_ledgers(state_path) == ()
    assert not ledger_path(state_path, Epoch2Collection.RUN).exists()


def test_a_non_object_projection_is_refused(state_path: Path) -> None:
    write_document(state_path, _document())
    status_projection_path(state_path).write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        read_document(state_path)


def test_a_malformed_projection_is_refused(state_path: Path) -> None:
    write_document(state_path, _document())
    status_projection_path(state_path).write_text(
        json.dumps({"base": "not-a-digest", "status": {}}), encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        read_document(state_path)


def test_a_missing_document_is_refused(state_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_document(state_path)
    with pytest.raises(FileNotFoundError):
        migrate_status_projection(state_path)


def test_a_non_ledger_collection_has_no_seed(state_path: Path) -> None:
    with pytest.raises(ValueError, match="not ledger"):
        seed_ledger_path(state_path, Epoch2Collection.TRACK)
