"""The document is one object to readers and two files on disk.

Task and Run rows and the canonical sequence are per-Task and per-Run
status, kept in the machine-local projection; everything else stays in
the committed ``state.json``, which a status-only write leaves
byte-identical.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.kernel.store.compaction import (
    STATUS_DOCUMENT_KEYS,
    read_document,
    recover_store_tree,
    seed_status_ledgers,
    split_status_projection,
    write_document,
)
from eawf.kernel.store.paths import ledger_path, seed_ledger_path, status_projection_path
from eawf.kernel.store.tiers import STATUS_PROJECTION_COLLECTIONS, Epoch2Collection


def _document(*, task_status: str = "RUNNING", sequence: int = 7) -> dict[str, object]:
    """Return a document carrying committed facts and status side by side."""
    return {
        "schema_version": "2",
        "milestone": {"MLS-0001": {"status": "ACTIVE"}},
        "batch": {"BAT-0001": {"status": "ACTIVE"}},
        "task": {"EAWF-0001": {"status": task_status}},
        "run": {"RUN-00000001": {"status": "RUNNING"}},
        "canonical_sequence": sequence,
    }


def _raw(path: Path) -> dict[str, object]:
    """Return a file's own decoded object, nothing merged in."""
    return json.loads(path.read_text("utf-8"))


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a generation's document path."""
    generation = tmp_path / "gen-0123456789abcdef"
    generation.mkdir()
    return generation / "state.json"


def test_status_keys_are_the_status_collections_and_the_sequence() -> None:
    assert {Epoch2Collection.TASK, Epoch2Collection.RUN} == STATUS_PROJECTION_COLLECTIONS
    assert {"task", "run", "canonical_sequence"} == STATUS_DOCUMENT_KEYS


def test_write_splits_and_read_merges(state_path: Path) -> None:
    document = _document()
    write_document(state_path, document)
    assert set(_raw(state_path)) == {"schema_version", "milestone", "batch"}
    assert set(_raw(status_projection_path(state_path))) == STATUS_DOCUMENT_KEYS
    assert read_document(state_path) == document


def test_a_status_only_write_leaves_the_committed_file_untouched(state_path: Path) -> None:
    write_document(state_path, _document())
    before = state_path.read_bytes()
    before_inode = state_path.stat().st_ino
    write_document(state_path, _document(task_status="COMPLETED", sequence=8))
    assert state_path.read_bytes() == before
    assert state_path.stat().st_ino == before_inode
    assert read_document(state_path)["task"] == {"EAWF-0001": {"status": "COMPLETED"}}


def test_a_committed_fact_rewrites_the_committed_file(state_path: Path) -> None:
    write_document(state_path, _document())
    document = _document()
    document["batch"] = {"BAT-0001": {"status": "MERGED"}}
    write_document(state_path, document)
    assert _raw(state_path)["batch"] == {"BAT-0001": {"status": "MERGED"}}


def test_a_dropped_status_key_does_not_resurrect(state_path: Path) -> None:
    write_document(state_path, _document())
    document = _document()
    del document["run"]
    write_document(state_path, document)
    assert "run" not in read_document(state_path)


def test_an_empty_document_writes_an_empty_projection(state_path: Path) -> None:
    write_document(state_path, {})
    assert _raw(state_path) == {}
    assert _raw(status_projection_path(state_path)) == {}
    assert read_document(state_path) == {}


def test_a_pre_split_document_reads_whole_until_migrated(state_path: Path) -> None:
    state_path.write_text(json.dumps(_document()), encoding="utf-8")
    assert read_document(state_path) == _document()
    assert split_status_projection(state_path) is True
    assert STATUS_DOCUMENT_KEYS.isdisjoint(_raw(state_path))
    assert read_document(state_path) == _document()
    assert split_status_projection(state_path) is False


def test_recovery_splits_a_pre_split_document(state_path: Path) -> None:
    state_path.write_text(json.dumps(_document()), encoding="utf-8")
    recover_store_tree(state_path)
    assert STATUS_DOCUMENT_KEYS.isdisjoint(_raw(state_path))
    assert read_document(state_path) == _document()


def test_the_projection_wins_over_stale_status_in_the_committed_file(state_path: Path) -> None:
    write_document(state_path, _document(task_status="COMPLETED"))
    stale = {**_raw(state_path), "task": {"EAWF-0001": {"status": "PLANNED"}}}
    state_path.write_text(json.dumps(stale), encoding="utf-8")
    assert read_document(state_path)["task"] == {"EAWF-0001": {"status": "COMPLETED"}}


def test_status_ledgers_resolve_locally_and_others_stay_committed(state_path: Path) -> None:
    generation = state_path.parent
    assert ledger_path(state_path, Epoch2Collection.TASK) == (
        generation / "local" / "ledger" / "task.jsonl"
    )
    assert seed_ledger_path(state_path, Epoch2Collection.TASK) == (
        generation / "ledger" / "task.jsonl"
    )
    assert ledger_path(state_path, Epoch2Collection.BATCH) == generation / "ledger" / "batch.jsonl"


def test_seed_copies_a_committed_status_ledger_once(state_path: Path) -> None:
    seed = seed_ledger_path(state_path, Epoch2Collection.TASK)
    seed.parent.mkdir()
    seed.write_text('{"line": 1}\n', encoding="utf-8")
    assert seed_status_ledgers(state_path) == (Epoch2Collection.TASK,)
    local = ledger_path(state_path, Epoch2Collection.TASK)
    assert local.read_text("utf-8") == '{"line": 1}\n'
    local.write_text('{"line": 1}\n{"line": 2}\n', encoding="utf-8")
    assert seed_status_ledgers(state_path) == ()
    assert seed.read_text("utf-8") == '{"line": 1}\n'


def test_seed_without_a_committed_ledger_seeds_nothing(state_path: Path) -> None:
    assert seed_status_ledgers(state_path) == ()
    assert not ledger_path(state_path, Epoch2Collection.RUN).exists()


def test_a_non_object_projection_is_refused(state_path: Path) -> None:
    write_document(state_path, _document())
    status_projection_path(state_path).write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        read_document(state_path)


def test_a_missing_document_is_refused(state_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_document(state_path)
    with pytest.raises(FileNotFoundError):
        split_status_projection(state_path)


def test_a_non_ledger_collection_has_no_seed(state_path: Path) -> None:
    with pytest.raises(ValueError, match="not ledger"):
        seed_ledger_path(state_path, Epoch2Collection.TRACK)
