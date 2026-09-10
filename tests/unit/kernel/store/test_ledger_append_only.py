"""A committed ledger line is never rewritten, reordered or removed.

The guard is asserted against real files rather than against the helper
in isolation, because the property that matters is what a writer can
leave on disk, not what a pure function returns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.compact import compact_store
from eawf.kernel.store.ledger import (
    LedgerAppendOnlyError,
    LedgerError,
    LedgerRecord,
    LedgerTornTailError,
    append_correction,
    append_ledger_record,
    effective_records,
    guarded_ledger_write,
    line_digest,
    read_ledger_records,
    render_ledger_line,
    split_ledger_lines,
    truncate_torn_tail,
    verify_append_only,
)
from eawf.kernel.store.paths import ledger_path, store_path
from eawf.kernel.store.tiers import Epoch2Collection

pytestmark = pytest.mark.unit


def _record(key: str, *, status: str = "COMPLETED", supersedes: str | None = None) -> LedgerRecord:
    return LedgerRecord(
        collection=Epoch2Collection.TASK,
        record_key=key,
        status=status,
        recorded_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        supersedes=supersedes,
        payload={"intent": f"work on {key}"},
    )


def _ledger(tmp_path: Path) -> Path:
    return ledger_path(tmp_path / ".ea" / "state.json", Epoch2Collection.TASK)


def test_append_writes_one_newline_terminated_line(tmp_path: Path) -> None:
    """Each append adds exactly one line and returns its offset."""
    path = _ledger(tmp_path)
    first = append_ledger_record(path, _record("EAWF-0001"))
    second = append_ledger_record(path, _record("EAWF-0002"))
    content = path.read_bytes()

    assert first == 0
    assert second == len(render_ledger_line(_record("EAWF-0001")).encode()) + 1
    assert content.endswith(b"\n")
    assert len(split_ledger_lines(content)) == 2


def test_append_preserves_the_existing_prefix(tmp_path: Path) -> None:
    """What was committed is still byte-for-byte there after the next write."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    before = path.read_bytes()
    append_ledger_record(path, _record("EAWF-0002"))

    assert path.read_bytes().startswith(before)


def test_empty_ledger_reads_as_no_records(tmp_path: Path) -> None:
    """A ledger that was never written reads as empty, not as an error."""
    assert read_ledger_records(_ledger(tmp_path)) == ()
    assert split_ledger_lines(b"") == ()


def test_single_record_ledger_round_trips(tmp_path: Path) -> None:
    """The one-line boundary case reads back exactly what was written."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    records = read_ledger_records(path)

    assert len(records) == 1
    assert records[0].record_key == "EAWF-0001"
    assert records[0].payload == {"intent": "work on EAWF-0001"}


def test_verify_append_only_accepts_an_extension() -> None:
    """Adding bytes at the end is the one admitted change."""
    verify_append_only(b"a\n", b"a\nb\n")
    verify_append_only(b"", b"a\n")
    verify_append_only(b"a\n", b"a\n")


def test_verify_append_only_rejects_a_rewrite() -> None:
    """Editing a committed line diverges from the prefix."""
    with pytest.raises(LedgerAppendOnlyError, match="diverges at byte 0"):
        verify_append_only(b"a\n", b"z\n")


def test_verify_append_only_rejects_a_removal() -> None:
    """Dropping a committed line shortens the file."""
    with pytest.raises(LedgerAppendOnlyError, match="drops 2 committed bytes"):
        verify_append_only(b"a\nb\n", b"a\n")


def test_verify_append_only_rejects_a_reorder() -> None:
    """Swapping two committed lines keeps the length and breaks the prefix."""
    with pytest.raises(LedgerAppendOnlyError, match="diverges at byte 0"):
        verify_append_only(b"a\nb\n", b"b\na\n")


def test_guarded_write_rejects_a_rewritten_line(tmp_path: Path) -> None:
    """A whole-file write that edits a committed line is refused."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    committed = path.read_bytes()
    rewritten = committed.replace(b"COMPLETED", b"CANCELLED")

    with pytest.raises(LedgerAppendOnlyError):
        guarded_ledger_write(path, rewritten)
    assert path.read_bytes() == committed


def test_guarded_write_rejects_a_reordered_file(tmp_path: Path) -> None:
    """A whole-file write that swaps two committed lines is refused."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    append_ledger_record(path, _record("EAWF-0002"))
    committed = path.read_bytes()
    first, second = split_ledger_lines(committed)
    reordered = f"{second}\n{first}\n".encode()

    with pytest.raises(LedgerAppendOnlyError):
        guarded_ledger_write(path, reordered)
    assert path.read_bytes() == committed


def test_guarded_write_rejects_a_removed_line(tmp_path: Path) -> None:
    """A whole-file write that drops a committed line is refused."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    append_ledger_record(path, _record("EAWF-0002"))
    committed = path.read_bytes()
    kept = f"{split_ledger_lines(committed)[0]}\n".encode()

    with pytest.raises(LedgerAppendOnlyError):
        guarded_ledger_write(path, kept)
    assert path.read_bytes() == committed


def test_guarded_write_accepts_an_extension(tmp_path: Path) -> None:
    """Extending the file is admitted, so the guard is not vacuous."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    extended = path.read_bytes() + f"{render_ledger_line(_record('EAWF-0002'))}\n".encode()

    guarded_ledger_write(path, extended)

    assert len(read_ledger_records(path)) == 2


def test_correction_appends_a_supersedes_line(tmp_path: Path) -> None:
    """A wrong line is corrected by appending, never by editing."""
    path = _ledger(tmp_path)
    wrong = _record("EAWF-0001", status="COMPLETED")
    append_ledger_record(path, wrong)
    digest = line_digest(render_ledger_line(wrong))

    append_correction(path, _record("EAWF-0001", status="FAILED", supersedes=digest))

    records = read_ledger_records(path)
    assert len(records) == 2
    assert records[0].status == "COMPLETED"
    assert records[1].supersedes == digest

    standing = effective_records(records)
    assert [item.status for item in standing] == ["FAILED"]


def test_correction_without_a_superseded_digest_is_refused(tmp_path: Path) -> None:
    """A correction that names nothing would be an unexplained duplicate."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))

    with pytest.raises(LedgerError, match="must name the digest"):
        append_correction(path, _record("EAWF-0001", status="FAILED"))


def test_correction_naming_an_unknown_digest_is_refused(tmp_path: Path) -> None:
    """A correction pointing at nothing is refused before it lands."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    absent = f"sha256:{'0' * 64}"

    with pytest.raises(LedgerError, match="holds no line with digest"):
        append_correction(path, _record("EAWF-0001", status="FAILED", supersedes=absent))
    assert len(read_ledger_records(path)) == 1


def test_effective_records_of_an_empty_ledger_is_empty() -> None:
    """The empty boundary case has nothing standing and nothing superseded."""
    assert effective_records(()) == ()


def test_torn_tail_refuses_to_read(tmp_path: Path) -> None:
    """A half-written line is a repair job, not a record."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    with path.open("ab") as handle:
        handle.write(b'{"collection":"task","record_key":"EAWF-0002"')

    with pytest.raises(LedgerTornTailError):
        read_ledger_records(path)


def test_truncate_torn_tail_keeps_every_committed_line(tmp_path: Path) -> None:
    """Repair drops the partial bytes and nothing else."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    committed = path.read_bytes()
    with path.open("ab") as handle:
        handle.write(b'{"collection":"task"')

    dropped = truncate_torn_tail(path)

    assert dropped == 20
    assert path.read_bytes() == committed
    assert len(read_ledger_records(path)) == 1


def test_truncate_torn_tail_is_a_no_op_on_an_intact_ledger(tmp_path: Path) -> None:
    """Repair never touches a file that ends on a line boundary."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    committed = path.read_bytes()

    assert truncate_torn_tail(path) == 0
    assert path.read_bytes() == committed


def test_truncate_torn_tail_on_a_missing_file_is_a_no_op(tmp_path: Path) -> None:
    """A ledger that does not exist has no tail to repair."""
    assert truncate_torn_tail(_ledger(tmp_path)) == 0


def test_truncate_torn_tail_empties_a_single_partial_line(tmp_path: Path) -> None:
    """A crash before the first newline leaves no committed line at all."""
    path = _ledger(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"collection":"task"')

    assert truncate_torn_tail(path) == 20
    assert path.read_bytes() == b""


def test_epoch1_compactor_refuses_an_epoch2_ledger(tmp_path: Path) -> None:
    """The in-place deduplicator cannot reach an append-only ledger."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    append_ledger_record(path, _record("EAWF-0001", status="FAILED"))
    committed = path.read_bytes()

    with pytest.raises(LedgerAppendOnlyError, match="append-only ledger"):
        compact_store(path)
    assert path.read_bytes() == committed


def test_epoch1_compactor_still_serves_the_store_directory(tmp_path: Path) -> None:
    """The fence is the ledger directory, not the compactor as a whole."""
    path = store_path(tmp_path / ".ea" / "state.json", StoreKind.MEMORY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    assert compact_store(path).records_in == 0


def test_append_rejects_a_collection_that_is_not_a_ledger(tmp_path: Path) -> None:
    """A document collection has no append-only file to land in."""
    record = LedgerRecord(
        collection=Epoch2Collection.TRACK,
        record_key="TRK-RUNTIME",
        status="ACTIVE",
        recorded_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="so it has no ledger"):
        append_ledger_record(tmp_path / "track.jsonl", record)


def test_record_rejects_an_unknown_key() -> None:
    """The ledger line model forbids extras."""
    with pytest.raises(ValidationError):
        LedgerRecord.model_validate(
            {
                "collection": "task",
                "record_key": "EAWF-0001",
                "status": "COMPLETED",
                "recorded_at": "2026-09-01T12:00:00Z",
                "operator": "someone",
            }
        )


def test_record_rejects_an_empty_key() -> None:
    """An unkeyed line could never be located again."""
    with pytest.raises(ValidationError):
        LedgerRecord.model_validate(
            {
                "collection": "task",
                "record_key": "",
                "status": "COMPLETED",
                "recorded_at": "2026-09-01T12:00:00Z",
            }
        )


def test_record_rejects_a_naive_timestamp() -> None:
    """A timestamp without a zone is not a fact about when."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        LedgerRecord.model_validate(
            {
                "collection": "task",
                "record_key": "EAWF-0001",
                "status": "COMPLETED",
                "recorded_at": "2026-09-01T12:00:00",
            }
        )


def test_record_rejects_a_malformed_supersedes_digest() -> None:
    """A correction must name a digest, not a free-form reference."""
    with pytest.raises(ValidationError):
        LedgerRecord.model_validate(
            {
                "collection": "task",
                "record_key": "EAWF-0001",
                "status": "FAILED",
                "recorded_at": "2026-09-01T12:00:00Z",
                "supersedes": "the previous one",
            }
        )
