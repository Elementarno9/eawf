from __future__ import annotations

import json
import os
import time
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


def test_acquire_steals_stale_lock_and_emits_event(tmp_path: Path) -> None:
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
    with portalock.acquire(target, timeout=1.0, on_event=events.append) as lock:
        body = json.loads(lock.path.read_text())
        assert body["pid"] == os.getpid()
    assert any(e.get("event_type") == "lock_stolen" for e in events)


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


def test_live_holder_past_stale_window_stays_unstealable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Compress the stale window so a short hold outlives it. The background
    # ticker owned by acquire refreshes heartbeat_at faster than the window,
    # so a live holder is never seen as stale even past STALE_HEARTBEAT_SECONDS.
    monkeypatch.setattr(stale, "STALE_HEARTBEAT_SECONDS", 0.5)
    target = tmp_path / "state.json"
    target.write_text("{}")
    with portalock.acquire(
        target, timeout=1.0, heartbeat_interval=0.05, hold_ceiling=100.0
    ) as lock:
        time.sleep(0.8)
        assert not stale.is_stale(lock.path)
