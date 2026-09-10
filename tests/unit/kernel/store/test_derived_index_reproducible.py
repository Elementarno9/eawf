"""Deleting the whole index directory costs a rebuild and nothing else.

The claim under test is byte-identity, not merely equality of the parsed
objects: an index that regenerates with reordered keys or a re-rolled
timestamp would show up as a diff on every rebuild, and the tier would
stop being disposable.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.store.index import (
    LedgerIndex,
    build_ledger_index,
    regenerate_indexes,
    render_index,
    write_ledger_index,
)
from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerTornTailError,
    append_correction,
    append_ledger_record,
    content_digest,
    line_digest,
    render_ledger_line,
)
from eawf.kernel.store.paths import index_dir, index_path, ledger_path
from eawf.kernel.store.tiers import Epoch2Collection

pytestmark = pytest.mark.unit


def _state_path(tmp_path: Path) -> Path:
    return tmp_path / ".ea" / "state.json"


def _record(key: str, *, status: str = "COMPLETED", supersedes: str | None = None) -> LedgerRecord:
    return LedgerRecord(
        collection=Epoch2Collection.TASK,
        record_key=key,
        status=status,
        recorded_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        supersedes=supersedes,
        payload={"intent": f"work on {key}"},
    )


def _seed_ledger(tmp_path: Path, keys: tuple[str, ...]) -> Path:
    path = ledger_path(_state_path(tmp_path), Epoch2Collection.TASK)
    for key in keys:
        append_ledger_record(path, _record(key))
    return path


def _index_bytes(tmp_path: Path) -> dict[str, bytes]:
    return {
        item.name: item.read_bytes() for item in sorted(index_dir(_state_path(tmp_path)).iterdir())
    }


def test_index_entries_locate_each_line(tmp_path: Path) -> None:
    """Every entry points at the exact bytes of its own line."""
    path = _seed_ledger(tmp_path, ("EAWF-0001", "EAWF-0002", "EAWF-0003"))
    content = path.read_bytes()
    index = build_ledger_index(Epoch2Collection.TASK, content)

    assert index.line_count == 3
    assert index.ledger_digest == content_digest(content)
    for entry in index.entries:
        line = content[entry.offset : entry.offset + entry.length].decode("utf-8")
        assert line_digest(line) == entry.digest
        assert entry.record_key in line
        assert content[entry.offset + entry.length : entry.offset + entry.length + 1] == b"\n"


def test_index_of_an_empty_ledger_has_no_entries(tmp_path: Path) -> None:
    """The empty boundary case is an index, not a failure."""
    index = build_ledger_index(Epoch2Collection.TASK, b"")

    assert index.line_count == 0
    assert index.entries == ()
    assert index.ledger_digest == content_digest(b"")


def test_index_of_a_single_line_starts_at_zero(tmp_path: Path) -> None:
    """The one-line boundary case indexes offset zero."""
    path = _seed_ledger(tmp_path, ("EAWF-0001",))
    index = build_ledger_index(Epoch2Collection.TASK, path.read_bytes())

    assert index.line_count == 1
    assert index.entries[0].offset == 0
    assert index.entries[0].length == len(render_ledger_line(_record("EAWF-0001")).encode())


def test_index_marks_a_superseded_line(tmp_path: Path) -> None:
    """A corrected line stays indexed and is flagged rather than dropped."""
    path = _seed_ledger(tmp_path, ("EAWF-0001",))
    digest = line_digest(render_ledger_line(_record("EAWF-0001")))
    append_correction(path, _record("EAWF-0001", status="FAILED", supersedes=digest))

    index = build_ledger_index(Epoch2Collection.TASK, path.read_bytes())

    assert [entry.superseded for entry in index.entries] == [True, False]


def test_index_is_the_same_bytes_on_every_build(tmp_path: Path) -> None:
    """Two builds over one ledger produce one byte string."""
    path = _seed_ledger(tmp_path, ("EAWF-0001", "EAWF-0002"))
    content = path.read_bytes()

    first = render_index(build_ledger_index(Epoch2Collection.TASK, content))
    second = render_index(build_ledger_index(Epoch2Collection.TASK, content))

    assert first == second


def test_deleting_the_index_directory_regenerates_identical_bytes(tmp_path: Path) -> None:
    """The regeneration claim, asserted over the whole directory."""
    _seed_ledger(tmp_path, ("EAWF-0001", "EAWF-0002", "EAWF-0003"))
    state_path = _state_path(tmp_path)
    regenerate_indexes(state_path)
    before = _index_bytes(tmp_path)

    shutil.rmtree(index_dir(state_path))
    assert not index_dir(state_path).exists()
    regenerate_indexes(state_path)

    assert _index_bytes(tmp_path) == before
    assert before


def test_regeneration_is_idempotent_without_deleting(tmp_path: Path) -> None:
    """Rebuilding over an existing index leaves the same bytes."""
    _seed_ledger(tmp_path, ("EAWF-0001",))
    state_path = _state_path(tmp_path)
    regenerate_indexes(state_path)
    before = _index_bytes(tmp_path)

    regenerate_indexes(state_path)

    assert _index_bytes(tmp_path) == before


def test_regeneration_covers_only_the_ledgers_that_exist(tmp_path: Path) -> None:
    """A tree with one ledger gets one index, not thirty-eight."""
    _seed_ledger(tmp_path, ("EAWF-0001",))

    written = regenerate_indexes(_state_path(tmp_path))

    assert written == (Epoch2Collection.TASK,)


def test_regeneration_of_an_empty_tree_writes_nothing(tmp_path: Path) -> None:
    """A tree with no ledgers has no indexes to write."""
    assert regenerate_indexes(_state_path(tmp_path)) == ()
    assert not index_dir(_state_path(tmp_path)).exists()


def test_written_index_is_the_rendered_form_of_what_was_built(tmp_path: Path) -> None:
    """What is written is exactly what the renderer produced."""
    _seed_ledger(tmp_path, ("EAWF-0001", "EAWF-0002"))
    state_path = _state_path(tmp_path)

    written = write_ledger_index(state_path, Epoch2Collection.TASK)

    stored = index_path(state_path, Epoch2Collection.TASK).read_bytes()
    assert stored == render_index(written)
    assert LedgerIndex.model_validate_json(stored) == written


def test_index_of_a_torn_ledger_is_refused(tmp_path: Path) -> None:
    """Indexing around a half-written line would publish doomed offsets."""
    path = _seed_ledger(tmp_path, ("EAWF-0001",))
    with path.open("ab") as handle:
        handle.write(b'{"collection":"task"')

    with pytest.raises(LedgerTornTailError):
        write_ledger_index(_state_path(tmp_path), Epoch2Collection.TASK)


def test_write_index_rejects_a_non_ledger_collection(tmp_path: Path) -> None:
    """Only a ledger has offsets worth indexing."""
    with pytest.raises(ValueError, match="not ledger"):
        write_ledger_index(_state_path(tmp_path), Epoch2Collection.TRACK)


def test_index_file_is_absent_before_generation(tmp_path: Path) -> None:
    """A missing index is an absent file, not an empty index."""
    with pytest.raises(FileNotFoundError):
        index_path(_state_path(tmp_path), Epoch2Collection.TASK).read_bytes()


def test_index_rejects_an_unknown_key() -> None:
    """The index model forbids extras like every other strict model."""
    with pytest.raises(ValidationError):
        LedgerIndex.model_validate(
            {
                "collection": "task",
                "line_count": 0,
                "ledger_digest": content_digest(b""),
                "generated_at": "2026-09-01T12:00:00Z",
            }
        )


def test_index_rejects_a_negative_line_count() -> None:
    """A count below zero is not a count."""
    with pytest.raises(ValidationError):
        LedgerIndex.model_validate(
            {"collection": "task", "line_count": -1, "ledger_digest": content_digest(b"")}
        )


def test_index_path_names_the_collection(tmp_path: Path) -> None:
    """One ledger, one index file, named after it."""
    assert index_path(_state_path(tmp_path), Epoch2Collection.AUDIT).name == "audit.index.json"
