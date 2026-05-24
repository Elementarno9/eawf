"""Tests for :mod:`eawf.runtime.daemon.recovery` — startup WAL replay.

The replay loop is pure file-system + log scanning; tests build a
``tmp_path / "wal"`` directory by hand and assert the renames, the
``ReplayReport`` counts, and the idempotence property (running replay
twice on the same state yields the same report on the second pass).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.recovery import ReplayReport, replay_wal
from eawf.runtime.daemon.wal import (
    WalRecord,
    list_poisoned,
    list_records,
    mark_applied,
    write_pending,
)

pytestmark = pytest.mark.unit


def _envelope(env_id: str = "env-test-001") -> Envelope:
    return Envelope(
        id=env_id,
        kind=StoreKind.EVENT,
        scope_id=None,
        created_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        summary="test envelope",
        payload={"action": "noop"},
    )


def _record(record_id: str, envelope_id: str) -> WalRecord:
    return WalRecord(
        record_id=record_id,
        envelope=_envelope(envelope_id),
        idempotency_key=None,
        written_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        before_state_version="sha:before",
        after_state_version="sha:after",
    )


def _read_event_ids(event_path: Path) -> list[str]:
    if not event_path.exists():
        return []
    ids: list[str] = []
    for line in event_path.read_bytes().splitlines():
        if not line.strip():
            continue
        ids.append(orjson.loads(line)["id"])
    return ids


def test_replay_wal_with_empty_dir_returns_zero_counts(tmp_path: Path) -> None:
    report = replay_wal(
        tmp_path / "wal",
        state_path=tmp_path / "state.json",
        event_path=tmp_path / "event.jsonl",
    )
    assert report == ReplayReport()


def test_replay_wal_with_missing_wal_dir_short_circuits(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    event_path = tmp_path / "event.jsonl"
    report = replay_wal(tmp_path / "missing", state_path=state_path, event_path=event_path)
    assert report.pending_count == 0
    assert report.applied_count == 0


def test_replay_wal_pending_record_is_poisoned(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    event_path = tmp_path / "event.jsonl"
    write_pending(wal_dir, _record("rec-001", "env-001"))

    report = replay_wal(wal_dir, state_path=tmp_path / "state.json", event_path=event_path)
    assert report.pending_count == 1
    assert report.applied_count == 0
    assert report.replayed_event_count == 0
    assert report.poisoned_count == 1

    poisoned = list_poisoned(wal_dir)
    assert len(poisoned) == 1
    body = orjson.loads(poisoned[0].read_bytes())
    assert body["poison_reason"] == "daemon_crashed_pre_apply"
    # No event row was appended for a pending record.
    assert _read_event_ids(event_path) == []


def test_replay_wal_applied_with_event_already_present(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    event_path = tmp_path / "event.jsonl"
    record = _record("rec-002", "env-002")
    write_pending(wal_dir, record)
    mark_applied(wal_dir, record.record_id)
    # Seed event.jsonl with the envelope row that the WAL outcome captures.
    event_path.write_bytes(record.envelope.model_dump_json().encode("utf-8") + b"\n")

    report = replay_wal(wal_dir, state_path=tmp_path / "state.json", event_path=event_path)
    assert report.pending_count == 0
    assert report.applied_count == 1
    assert report.replayed_event_count == 0
    # Record renamed to .fsynced.
    assert (wal_dir / "rec-002.fsynced.json").exists()
    assert not (wal_dir / "rec-002.applied.json").exists()
    # Event log not duplicated.
    assert _read_event_ids(event_path) == ["env-002"]


def test_replay_wal_applied_missing_event_appends_row(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    event_path = tmp_path / "event.jsonl"
    record = _record("rec-003", "env-003")
    write_pending(wal_dir, record)
    mark_applied(wal_dir, record.record_id)
    # event.jsonl does NOT contain env-003 — replay should append it.
    assert not event_path.exists()

    report = replay_wal(wal_dir, state_path=tmp_path / "state.json", event_path=event_path)
    assert report.applied_count == 1
    assert report.replayed_event_count == 1
    assert (wal_dir / "rec-003.fsynced.json").exists()
    assert _read_event_ids(event_path) == ["env-003"]


def test_replay_wal_is_idempotent(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    event_path = tmp_path / "event.jsonl"
    state_path = tmp_path / "state.json"
    pending = _record("rec-pending", "env-pending")
    applied = _record("rec-applied", "env-applied")
    write_pending(wal_dir, pending)
    write_pending(wal_dir, applied)
    mark_applied(wal_dir, applied.record_id)

    first = replay_wal(wal_dir, state_path=state_path, event_path=event_path)
    assert first.pending_count == 1
    assert first.applied_count == 1
    assert first.replayed_event_count == 1
    assert first.poisoned_count == 1

    second = replay_wal(wal_dir, state_path=state_path, event_path=event_path)
    # No new pending or applied records exist. Poisoned count stays the
    # same; fsynced count reflects the single record promoted in run 1.
    assert second.pending_count == 0
    assert second.applied_count == 0
    assert second.replayed_event_count == 0
    assert second.poisoned_count == 1
    assert second.fsynced_count == 1
    # No double-event in the log.
    assert _read_event_ids(event_path) == ["env-applied"]


def test_replay_wal_multiple_applied_replayed_in_sorted_order(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    event_path = tmp_path / "event.jsonl"
    later = WalRecord(
        record_id="rec-later",
        envelope=_envelope("env-later"),
        idempotency_key=None,
        written_at=datetime(2026, 5, 19, 14, 0, 0, tzinfo=UTC),
        before_state_version="sha:later-before",
        after_state_version="sha:later-after",
    )
    earlier = WalRecord(
        record_id="rec-earlier",
        envelope=_envelope("env-earlier"),
        idempotency_key=None,
        written_at=datetime(2026, 5, 19, 10, 0, 0, tzinfo=UTC),
        before_state_version="sha:earlier-before",
        after_state_version="sha:earlier-after",
    )
    write_pending(wal_dir, later)
    write_pending(wal_dir, earlier)
    mark_applied(wal_dir, later.record_id)
    mark_applied(wal_dir, earlier.record_id)

    report = replay_wal(wal_dir, state_path=tmp_path / "state.json", event_path=event_path)
    assert report.applied_count == 2
    assert report.replayed_event_count == 2
    # Earlier record's envelope appears first by written_at sort order.
    assert _read_event_ids(event_path) == ["env-earlier", "env-later"]


def test_replay_wal_corrupt_applied_record_is_poisoned(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    event_path = tmp_path / "event.jsonl"
    wal_dir.mkdir()
    corrupt = wal_dir / "rec-bad.applied.json"
    corrupt.write_bytes(b"{not-json")

    report = replay_wal(wal_dir, state_path=tmp_path / "state.json", event_path=event_path)
    assert report.poisoned_count >= 1
    assert list_poisoned(wal_dir)
    # No event row materialised from a corrupt record.
    assert _read_event_ids(event_path) == []


def test_replay_wal_preserves_fsynced_records(tmp_path: Path) -> None:
    """A pre-existing ``.fsynced.json`` survives replay untouched."""
    wal_dir = tmp_path / "wal"
    event_path = tmp_path / "event.jsonl"
    record = _record("rec-done", "env-done")
    write_pending(wal_dir, record)
    mark_applied(wal_dir, record.record_id)
    # Manually rename to .fsynced.json (skipping the helper since that's
    # what a clean shutdown leaves on disk).
    from eawf.runtime.daemon.wal import mark_fsynced

    mark_fsynced(wal_dir, record.record_id)
    event_path.write_bytes(record.envelope.model_dump_json().encode("utf-8") + b"\n")

    report = replay_wal(wal_dir, state_path=tmp_path / "state.json", event_path=event_path)
    assert report.fsynced_count == 1
    assert report.applied_count == 0
    assert (wal_dir / "rec-done.fsynced.json").exists()


def test_record_id_from_path_helper_handles_well_formed_names() -> None:
    """The defensive branch in ``_record_id_from_path`` accepts canonical shapes."""
    from eawf.runtime.daemon.recovery import _record_id_from_path

    assert _record_id_from_path(Path("rec-001.pending.json")) == "rec-001"
    assert _record_id_from_path(Path("rec-001.applied.json")) == "rec-001"
    # Degenerate cases the algorithm rejects (returns ``None``) so replay
    # routes the file under ``poisoned/`` rather than crashing.
    assert _record_id_from_path(Path("no-suffix")) is None
    assert _record_id_from_path(Path("nostatus.json")) is None


def test_replay_wal_listing_after_replay(tmp_path: Path) -> None:
    """After replay, live-status listing returns only ``.fsynced.json``."""
    wal_dir = tmp_path / "wal"
    event_path = tmp_path / "event.jsonl"
    record = _record("rec-live", "env-live")
    write_pending(wal_dir, record)
    mark_applied(wal_dir, record.record_id)
    replay_wal(wal_dir, state_path=tmp_path / "state.json", event_path=event_path)
    live = list_records(wal_dir)
    assert [p.name for p in live] == ["rec-live.fsynced.json"]


def test_replay_wal_report_is_pydantic_model() -> None:
    report = ReplayReport(
        pending_count=1,
        applied_count=2,
        fsynced_count=3,
        poisoned_count=4,
        replayed_event_count=5,
    )
    assert report.model_dump() == {
        "pending_count": 1,
        "applied_count": 2,
        "fsynced_count": 3,
        "poisoned_count": 4,
        "replayed_event_count": 5,
    }
