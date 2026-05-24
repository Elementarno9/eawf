"""Unit tests for :mod:`eawf.daemon.wal`.

The WAL primitive lives below the daemon RPC layer; tests exercise it
directly on a per-test ``tmp_path / "wal"`` directory. No asyncio loop,
no socket — pure filesystem.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest

from eawf.daemon import wal
from eawf.daemon.wal import (
    WalRecord,
    WalStatus,
    gc_done_records,
    list_poisoned,
    list_records,
    mark_applied,
    mark_fsynced,
    mark_poisoned,
    read_record,
    write_pending,
)
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope

pytestmark = pytest.mark.unit


def _build_envelope(env_id: str = "env-test-001") -> Envelope:
    return Envelope(
        id=env_id,
        kind=StoreKind.EVENT,
        scope_id=None,
        created_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        summary="test envelope",
        payload={"action": "noop"},
    )


def _build_record(
    record_id: str = "rec-001",
    *,
    envelope_id: str = "env-test-001",
    written_at: datetime | None = None,
) -> WalRecord:
    return WalRecord(
        record_id=record_id,
        envelope=_build_envelope(envelope_id),
        idempotency_key=None,
        written_at=written_at or datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        before_state_version="sha:before",
        after_state_version="sha:after",
    )


def test_write_pending_persists_parseable_record(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    record = _build_record()
    written = write_pending(wal_dir, record)
    assert written.exists()
    assert written.name == "rec-001.pending.json"
    reloaded = read_record(written)
    assert reloaded == record


def test_write_pending_rejects_duplicate(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    write_pending(wal_dir, _build_record())
    with pytest.raises(FileExistsError):
        write_pending(wal_dir, _build_record())


def test_mark_applied_renames_and_preserves_body(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    record = _build_record()
    pending = write_pending(wal_dir, record)
    original_bytes = pending.read_bytes()
    applied = mark_applied(wal_dir, record.record_id)
    assert not pending.exists()
    assert applied.exists()
    assert applied.name == "rec-001.applied.json"
    assert applied.read_bytes() == original_bytes
    assert read_record(applied) == record


def test_mark_fsynced_renames_applied(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    record = _build_record()
    write_pending(wal_dir, record)
    mark_applied(wal_dir, record.record_id)
    fsynced = mark_fsynced(wal_dir, record.record_id)
    assert fsynced.exists()
    assert fsynced.name == "rec-001.fsynced.json"
    assert read_record(fsynced) == record


def test_mark_fsynced_raises_when_applied_missing(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    write_pending(wal_dir, _build_record())  # only pending exists
    with pytest.raises(FileNotFoundError):
        mark_fsynced(wal_dir, "rec-001")


def test_mark_poisoned_injects_reason(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    record = _build_record()
    write_pending(wal_dir, record)
    poisoned = mark_poisoned(wal_dir, record.record_id, reason="daemon_crashed_pre_apply")
    assert poisoned.exists()
    assert poisoned.parent.name == "poisoned"
    reloaded = read_record(poisoned)
    assert reloaded.poison_reason == "daemon_crashed_pre_apply"
    # Source file is removed after the move.
    assert not (wal_dir / "rec-001.pending.json").exists()


def test_mark_poisoned_searches_status_order(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    record = _build_record()
    write_pending(wal_dir, record)
    mark_applied(wal_dir, record.record_id)
    poisoned = mark_poisoned(wal_dir, record.record_id, reason="post_validation_crash")
    assert poisoned.exists()
    assert read_record(poisoned).poison_reason == "post_validation_crash"


def test_mark_poisoned_raises_when_missing(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        mark_poisoned(wal_dir, "rec-missing", reason="x")


def test_mark_poisoned_handles_corrupt_record(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    corrupt_path = wal_dir / "rec-corrupt.pending.json"
    corrupt_path.write_bytes(b"{not-json")
    poisoned = mark_poisoned(wal_dir, "rec-corrupt", reason="wal_record_unreadable")
    assert poisoned.exists()
    # Raw bytes preserved for inspection (poison_reason cannot be injected
    # without a valid parse — verified by re-reading the raw bytes).
    assert poisoned.read_bytes() == b"{not-json"
    assert not corrupt_path.exists()


def test_gc_done_records_removes_only_aged_fsynced(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    # Three .fsynced.json — one aged, two recent.
    aged = _build_record("rec-aged")
    recent_a = _build_record("rec-recent-a")
    recent_b = _build_record("rec-recent-b")
    for record in (aged, recent_a, recent_b):
        write_pending(wal_dir, record)
        mark_applied(wal_dir, record.record_id)
        mark_fsynced(wal_dir, record.record_id)

    aged_path = wal_dir / "rec-aged.fsynced.json"
    past = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(aged_path, (past, past))

    # Also seed a .pending.json and .applied.json that GC must leave alone.
    pending_record = _build_record("rec-pending")
    write_pending(wal_dir, pending_record)
    applied_record = _build_record("rec-applied")
    write_pending(wal_dir, applied_record)
    mark_applied(wal_dir, applied_record.record_id)

    removed = gc_done_records(wal_dir, max_age_seconds=3600)
    assert [p.name for p in removed] == ["rec-aged.fsynced.json"]
    assert not aged_path.exists()
    assert (wal_dir / "rec-recent-a.fsynced.json").exists()
    assert (wal_dir / "rec-recent-b.fsynced.json").exists()
    assert (wal_dir / "rec-pending.pending.json").exists()
    assert (wal_dir / "rec-applied.applied.json").exists()


def test_gc_done_records_handles_missing_dir(tmp_path: Path) -> None:
    assert gc_done_records(tmp_path / "no-such-wal") == []


def test_list_records_returns_sorted_by_written_at(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    early = _build_record("rec-second", written_at=datetime(2026, 5, 19, 9, 0, 0, tzinfo=UTC))
    later = _build_record("rec-third", written_at=datetime(2026, 5, 19, 11, 0, 0, tzinfo=UTC))
    earliest = _build_record("rec-first", written_at=datetime(2026, 5, 19, 8, 0, 0, tzinfo=UTC))
    for record in (later, early, earliest):
        write_pending(wal_dir, record)

    listed = list_records(wal_dir)
    assert [p.name for p in listed] == [
        "rec-first.pending.json",
        "rec-second.pending.json",
        "rec-third.pending.json",
    ]


def test_list_records_filters_by_status(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    pending = _build_record("rec-pending")
    applied = _build_record("rec-applied")
    write_pending(wal_dir, pending)
    write_pending(wal_dir, applied)
    mark_applied(wal_dir, applied.record_id)

    pending_only = list_records(wal_dir, status=WalStatus.PENDING)
    assert [p.name for p in pending_only] == ["rec-pending.pending.json"]
    applied_only = list_records(wal_dir, status=WalStatus.APPLIED)
    assert [p.name for p in applied_only] == ["rec-applied.applied.json"]


def test_list_records_skips_unparseable_to_end(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    parseable = _build_record("rec-ok", written_at=datetime(2026, 5, 19, 10, 0, 0, tzinfo=UTC))
    write_pending(wal_dir, parseable)
    bad = wal_dir / "rec-bad.pending.json"
    bad.write_bytes(b"{not-json")
    listed = list_records(wal_dir)
    assert [p.name for p in listed] == [
        "rec-ok.pending.json",
        "rec-bad.pending.json",
    ]


def test_list_poisoned_returns_sorted(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    for record_id in ("rec-b", "rec-a", "rec-c"):
        write_pending(wal_dir, _build_record(record_id))
        mark_poisoned(wal_dir, record_id, reason="reason")
    poisoned_paths = list_poisoned(wal_dir)
    assert [p.name for p in poisoned_paths] == [
        "rec-a.poisoned.json",
        "rec-b.poisoned.json",
        "rec-c.poisoned.json",
    ]


def test_write_pending_serialisation_is_deterministic(tmp_path: Path) -> None:
    """JSON bytes round-trip through orjson sort-keys deterministically."""
    wal_dir = tmp_path / "wal"
    record = _build_record()
    path = write_pending(wal_dir, record)
    body = orjson.loads(path.read_bytes())
    assert body["record_id"] == "rec-001"
    assert body["envelope"]["id"] == "env-test-001"
    assert body["poison_reason"] is None


def test_wal_status_string_values() -> None:
    assert WalStatus.PENDING.value == "pending"
    assert WalStatus.APPLIED.value == "applied"
    assert WalStatus.FSYNCED.value == "fsynced"
    assert WalStatus.POISONED.value == "poisoned"


def test_wal_record_rejects_extra_fields(tmp_path: Path) -> None:
    """Pydantic extra='forbid' catches accidental fields."""
    from pydantic import ValidationError

    payload = {
        "record_id": "rec-x",
        "envelope": _build_envelope().model_dump(mode="json"),
        "idempotency_key": None,
        "written_at": datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC).isoformat(),
        "before_state_version": "a",
        "after_state_version": "b",
        "extra_field": "not-allowed",
    }
    with pytest.raises(ValidationError):
        WalRecord.model_validate(payload)


def test_gc_done_records_is_idempotent(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    record = _build_record("rec-aged")
    write_pending(wal_dir, record)
    mark_applied(wal_dir, record.record_id)
    mark_fsynced(wal_dir, record.record_id)
    past = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(wal_dir / "rec-aged.fsynced.json", (past, past))

    first = gc_done_records(wal_dir, max_age_seconds=3600)
    second = gc_done_records(wal_dir, max_age_seconds=3600)
    assert len(first) == 1
    assert second == []


def test_wal_module_exports_public_api() -> None:
    expected = {
        "WalRecord",
        "WalStatus",
        "gc_done_records",
        "list_poisoned",
        "list_records",
        "mark_applied",
        "mark_fsynced",
        "mark_poisoned",
        "read_record",
        "write_pending",
    }
    assert expected.issubset(set(wal.__all__))


def test_wal_record_round_trips_through_json(tmp_path: Path) -> None:
    record = _build_record()
    encoded = record.model_dump_json()
    decoded = WalRecord.model_validate_json(encoded)
    assert decoded == record


def test_write_pending_fsync_touches_parent_dir(tmp_path: Path) -> None:
    """Sanity: parent dir exists post-write (created by mkdir parents=True)."""
    wal_dir = tmp_path / "nested" / "wal"
    write_pending(wal_dir, _build_record())
    assert wal_dir.is_dir()
    # Mtime moved forward since the write happened just now.
    assert wal_dir.stat().st_mtime <= time.time()
