from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from eawf.runtime.lock import portalock


def test_acquire_writes_holder_metadata(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    with portalock.acquire(target, timeout=1.0) as lock:
        body = json.loads(lock.path.read_text())
        assert body["pid"] == os.getpid()
        assert body["hostname"]
        assert "started_at" in body
        assert "heartbeat_at" in body
    assert not lock.path.exists()


def test_acquire_timeout_raises(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    with portalock.acquire(target, timeout=0.5):  # noqa: SIM117
        with pytest.raises(portalock.LockTimeout):
            with portalock.acquire(target, timeout=0.1):
                pass


def test_heartbeat_updates_during_long_hold(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    with portalock.acquire(target, timeout=1.0) as lock:
        first = json.loads(lock.path.read_text())["heartbeat_at"]
        time.sleep(0.3)
        lock.heartbeat()
        second = json.loads(lock.path.read_text())["heartbeat_at"]
        assert second > first


def test_lockfile_removed_after_context_exit(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    lock_path: Path | None = None
    with portalock.acquire(target, timeout=1.0) as lock:
        lock_path = lock.path
        assert lock_path.exists()
    assert lock_path is not None
    assert not lock_path.exists()


def test_on_event_callback_not_called_on_clean_acquire(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    events: list[dict[str, object]] = []
    with portalock.acquire(target, timeout=1.0, on_event=events.append):
        pass
    assert events == []


def test_lock_handle_has_expected_paths(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    with portalock.acquire(target, timeout=1.0) as lock:
        assert lock.target == target
        assert lock.path == tmp_path / "state.json.lock"


def test_heartbeat_preserves_identity_fields(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    with portalock.acquire(target, timeout=1.0) as lock:
        before = json.loads(lock.path.read_text())
        time.sleep(0.05)
        lock.heartbeat()
        after = json.loads(lock.path.read_text())
    assert after["pid"] == before["pid"]
    assert after["hostname"] == before["hostname"]
    assert after["started_at"] == before["started_at"]
    assert after["heartbeat_at"] >= before["heartbeat_at"]


def test_acquire_reads_env_lock_timeout_at_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    monkeypatch.setenv("EA_LOCK_TIMEOUT", "0.1")
    with portalock.acquire(target):  # noqa: SIM117
        with pytest.raises(portalock.LockTimeout):
            with portalock.acquire(target):
                pass
