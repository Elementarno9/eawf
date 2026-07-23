from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.runtime.lock import portalock, stale


def test_dead_pid_detected_as_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.json.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,
                "hostname": "test-host",
                "started_at": datetime.now(UTC).isoformat(),
                "heartbeat_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    assert stale.is_stale(lock_path)


def test_heartbeat_too_old_detected_as_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.json.lock"
    long_ago = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": "test-host",
                "started_at": long_ago,
                "heartbeat_at": long_ago,
            }
        )
    )
    assert stale.is_stale(lock_path)


@pytest.mark.parametrize("with_callback", [False, True])
def test_acquire_recovers_stale_lock_with_optional_event(
    tmp_path: Path, *, with_callback: bool
) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    lock_path = tmp_path / "state.json.lock"
    long_ago = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,
                "hostname": "ghost",
                "started_at": long_ago,
                "heartbeat_at": long_ago,
            }
        )
    )
    events: list[dict[str, object]] = []
    on_event = events.append if with_callback else None
    with portalock.acquire(target, timeout=1.0, on_event=on_event) as lock:
        body = json.loads(lock.path.read_text())
        assert body["pid"] == os.getpid()
    if with_callback:
        assert [event["event_type"] for event in events] == ["lock_stolen"]
    else:
        assert events == []


def test_missing_lockfile_is_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "nonexistent.lock"
    assert stale.is_stale(lock_path)


def test_malformed_lockfile_is_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.json.lock"
    lock_path.write_text("not valid json {{{{")
    assert stale.is_stale(lock_path)


def test_fresh_lock_from_current_process_is_not_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.json.lock"
    now = datetime.now(UTC).isoformat()
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": "test-host",
                "started_at": now,
                "heartbeat_at": now,
            }
        )
    )
    assert not stale.is_stale(lock_path)


def test_pid_zero_treated_as_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.json.lock"
    now = datetime.now(UTC).isoformat()
    lock_path.write_text(
        json.dumps(
            {
                "pid": 0,
                "hostname": "test-host",
                "started_at": now,
                "heartbeat_at": now,
            }
        )
    )
    assert stale.is_stale(lock_path)


def test_pid_missing_treated_as_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.json.lock"
    now = datetime.now(UTC).isoformat()
    lock_path.write_text(
        json.dumps(
            {
                "hostname": "test-host",
                "started_at": now,
                "heartbeat_at": now,
            }
        )
    )
    assert stale.is_stale(lock_path)


def test_live_holder_with_stale_metadata_stays_unstealable(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    with portalock.acquire(
        target, timeout=1.0, heartbeat_interval=3_600.0, hold_ceiling=7_200.0
    ) as lock:
        body = json.loads(lock.path.read_text())
        body["heartbeat_at"] = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        lock.fh.seek(0)
        lock.fh.truncate()
        lock.fh.write(json.dumps(body))
        lock.fh.flush()
        os.fsync(lock.fh.fileno())
        assert stale.is_stale(lock.path)

        with (
            pytest.raises(portalock.LockTimeout),
            portalock.acquire(target, timeout=0.1),
        ):
            pass
