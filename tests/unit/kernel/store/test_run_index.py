"""A ledger re-read validates only the lines appended since the last read."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.store import run_index
from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerSnapshotError,
    LedgerTornTailError,
    append_ledger_record,
    read_ledger_records,
    render_ledger_line,
    seal_ledger_prefix,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.run_index import indexed_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection

pytestmark = pytest.mark.unit


def _record(key: str, *, status: str = "RUNNING") -> LedgerRecord:
    return LedgerRecord(
        collection=Epoch2Collection.RUN,
        record_key=key,
        status=status,
        recorded_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
        payload={"key": key},
    )


def _ledger(tmp_path: Path) -> Path:
    return ledger_path(tmp_path / ".ea" / "state.json", Epoch2Collection.RUN)


def _fill(path: Path, count: int, *, start: int = 0) -> None:
    for number in range(start, start + count):
        append_ledger_record(path, _record(f"RUN-{number:08d}"))


@pytest.fixture
def validations(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Count every ledger line validated while the test runs."""
    seen: list[str] = []
    original: Callable[..., LedgerRecord] = LedgerRecord.model_validate_json

    def counting(data: str | bytes, *args: object, **kwargs: object) -> LedgerRecord:
        seen.append(data if isinstance(data, str) else data.decode("utf-8"))
        return original(data, *args, **kwargs)

    monkeypatch.setattr(LedgerRecord, "model_validate_json", counting)
    yield seen


@pytest.mark.parametrize("appended", [1, 2, 7])
def test_run_index_repro_rereads_validate_whole_ledger(
    tmp_path: Path, validations: list[str], appended: int
) -> None:
    """After a full read, appending k lines and re-reading validates exactly k records."""
    path = _ledger(tmp_path)
    _fill(path, 5)
    assert len(indexed_ledger_records(path)) == 5
    validations.clear()

    _fill(path, appended, start=5)
    records = indexed_ledger_records(path)

    assert len(validations) == appended
    assert records == read_ledger_records(path)


def test_a_reread_with_nothing_appended_validates_nothing(
    tmp_path: Path, validations: list[str]
) -> None:
    """The zero boundary: an unchanged ledger is answered from the index."""
    path = _ledger(tmp_path)
    _fill(path, 3)
    first = indexed_ledger_records(path)
    validations.clear()

    assert indexed_ledger_records(path) == first
    assert validations == []


def test_truncating_the_ledger_rebuilds_it_in_full(tmp_path: Path, validations: list[str]) -> None:
    """A ledger shorter than the index read is validated again from its first line."""
    path = _ledger(tmp_path)
    _fill(path, 4)
    indexed_ledger_records(path)
    lines = path.read_bytes().split(b"\n")
    with path.open("r+b") as handle:
        handle.truncate(len(b"\n".join(lines[:2])) + 1)
    validations.clear()

    records = indexed_ledger_records(path)

    assert len(validations) == 2
    assert records == read_ledger_records(path)
    assert [record.record_key for record in records] == ["RUN-00000000", "RUN-00000001"]


def test_a_changed_head_rebuilds_in_full(tmp_path: Path, validations: list[str]) -> None:
    """A first line rewritten in place, with the file grown past the offset, is not trusted."""
    path = _ledger(tmp_path)
    _fill(path, 2)
    indexed_ledger_records(path)
    rewritten = render_ledger_line(_record("RUN-00000000", status="FAILED"))
    rest = path.read_bytes().split(b"\n", 1)[1]
    with path.open("r+b") as handle:
        handle.write(rewritten.encode("utf-8") + b"\n" + rest + rest)
    validations.clear()

    records = indexed_ledger_records(path)

    assert len(validations) == 3
    assert records[0].status == "FAILED"
    assert records == read_ledger_records(path)


def test_a_replaced_file_rebuilds_in_full(tmp_path: Path, validations: list[str]) -> None:
    """A rename over the ledger, as every sanctioned rewrite makes, gives a new inode."""
    path = _ledger(tmp_path)
    _fill(path, 2)
    indexed_ledger_records(path)
    replacement = path.with_name("replacement.jsonl")
    replacement.write_bytes(path.read_bytes() + path.read_bytes())
    os.replace(replacement, path)
    validations.clear()

    records = indexed_ledger_records(path)

    assert len(validations) == 4
    assert records == read_ledger_records(path)


def test_a_missing_ledger_reads_empty_then_indexes_once_created(
    tmp_path: Path, validations: list[str]
) -> None:
    """The empty boundary: no file reads as no records, and a later file is read whole."""
    path = _ledger(tmp_path)
    assert indexed_ledger_records(path) == ()

    _fill(path, 1)
    records = indexed_ledger_records(path)

    assert len(validations) == 1
    assert records == read_ledger_records(path)


def test_an_empty_ledger_grows_one_line_at_a_time(tmp_path: Path, validations: list[str]) -> None:
    """The single boundary: from zero bytes, each append validates exactly its own line."""
    path = _ledger(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    assert indexed_ledger_records(path) == ()

    for number in range(3):
        validations.clear()
        _fill(path, 1, start=number)
        assert len(indexed_ledger_records(path)) == number + 1
        assert len(validations) == 1


def test_a_torn_tail_raises_and_leaves_the_index_where_it_was(
    tmp_path: Path, validations: list[str]
) -> None:
    """A line still being written is never trusted, and its completion is read next time."""
    path = _ledger(tmp_path)
    _fill(path, 2)
    indexed_ledger_records(path)
    line = render_ledger_line(_record("RUN-00000002")).encode("utf-8")
    with path.open("ab") as handle:
        handle.write(line[:10])

    with pytest.raises(LedgerTornTailError):
        indexed_ledger_records(path)

    with path.open("ab") as handle:
        handle.write(line[10:] + b"\n")
    validations.clear()
    records = indexed_ledger_records(path)

    assert len(validations) == 1
    assert records == read_ledger_records(path)


def test_an_invalid_appended_line_raises_and_is_retried(
    tmp_path: Path, validations: list[str]
) -> None:
    """Schema mismatch: a line that is not a ledger record is refused on every read."""
    path = _ledger(tmp_path)
    _fill(path, 1)
    indexed_ledger_records(path)
    with path.open("ab") as handle:
        handle.write(b'{"collection": "run"}\n')

    for _ in range(2):
        with pytest.raises(ValidationError):
            indexed_ledger_records(path)


def test_a_sealed_ledger_rebuilds_then_reads_appends_incrementally(
    tmp_path: Path, validations: list[str]
) -> None:
    """A seal moves the prefix to a snapshot; the snapshot's lines still read first."""
    path = _ledger(tmp_path)
    _fill(path, 3)
    indexed_ledger_records(path)
    first_line = path.read_bytes().index(b"\n") + 1
    seal_ledger_prefix(path, first_line)
    validations.clear()

    sealed = indexed_ledger_records(path)
    assert len(validations) == 3
    assert sealed == read_ledger_records(path)

    validations.clear()
    _fill(path, 1, start=3)
    records = indexed_ledger_records(path)

    assert len(validations) == 1
    assert records == read_ledger_records(path)
    assert len(records) == 4


def test_a_snapshot_that_breaks_its_digest_is_refused(tmp_path: Path) -> None:
    """A snapshot whose bytes no longer hash to its name is never read."""
    path = _ledger(tmp_path)
    _fill(path, 2)
    snapshot = seal_ledger_prefix(path, path.read_bytes().index(b"\n") + 1)
    snapshot.write_bytes(snapshot.read_bytes() + b"\n")

    with pytest.raises(LedgerSnapshotError):
        indexed_ledger_records(path)


def test_a_path_that_is_not_a_path_is_refused() -> None:
    """Invalid type: the reader takes a Path, not a string."""
    with pytest.raises(AttributeError):
        indexed_ledger_records("run.jsonl")  # type: ignore[arg-type]


def _ledgers(tmp_path: Path, count: int) -> list[Path]:
    """Return *count* ledgers of two lines each, in trees of their own."""
    paths = [
        ledger_path(tmp_path / f"tree-{n}" / "state.json", Epoch2Collection.RUN)
        for n in range(count)
    ]
    for path in paths:
        _fill(path, 2)
    return paths


@pytest.fixture
def empty_index() -> Iterator[None]:
    """Start from an index no other test has filled."""
    run_index._INDEXES.clear()
    yield
    run_index._INDEXES.clear()


@pytest.mark.usefixtures("empty_index")
def test_run_index_repro_retains_every_ledger_read(tmp_path: Path, validations: list[str]) -> None:
    """The index keeps only the most recently read ledgers; the oldest is read whole again."""
    paths = _ledgers(tmp_path, run_index._CAPACITY + 1)
    for path in paths:
        indexed_ledger_records(path)

    assert len(run_index._INDEXES) == run_index._CAPACITY
    assert paths[0] not in run_index._INDEXES
    validations.clear()
    newest = indexed_ledger_records(paths[-1])
    assert validations == []
    indexed_ledger_records(paths[0])
    assert len(validations) == 2
    assert newest == read_ledger_records(paths[-1])


@pytest.mark.usefixtures("empty_index")
def test_a_reread_keeps_a_ledger_most_recent(tmp_path: Path, validations: list[str]) -> None:
    """Off by one: re-reading the oldest ledger makes the next oldest the one evicted."""
    paths = _ledgers(tmp_path, run_index._CAPACITY + 1)
    for path in paths[:-1]:
        indexed_ledger_records(path)
    indexed_ledger_records(paths[0])
    indexed_ledger_records(paths[-1])

    assert paths[0] in run_index._INDEXES
    assert paths[1] not in run_index._INDEXES
    assert len(run_index._INDEXES) == run_index._CAPACITY


@pytest.mark.usefixtures("empty_index")
def test_a_deleted_ledger_leaves_the_index(tmp_path: Path) -> None:
    """An entry whose file is gone is dropped, and a missing file is never indexed."""
    first, second = _ledgers(tmp_path, 2)
    indexed_ledger_records(first)
    first.unlink()

    assert indexed_ledger_records(first) == ()
    assert first not in run_index._INDEXES
    indexed_ledger_records(first.with_name("absent.jsonl"))
    assert list(run_index._INDEXES) == []
    indexed_ledger_records(second)
    assert list(run_index._INDEXES) == [second]


@pytest.mark.usefixtures("empty_index")
def test_a_ledger_deleted_while_another_is_read_is_dropped(tmp_path: Path) -> None:
    """A stored entry whose file vanished is pruned on the next store, not kept to the cap."""
    first, second = _ledgers(tmp_path, 2)
    indexed_ledger_records(first)
    first.unlink()
    indexed_ledger_records(second)

    assert list(run_index._INDEXES) == [second]
