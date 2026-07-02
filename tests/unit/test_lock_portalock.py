from __future__ import annotations

import json
import logging
import os
import threading
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


def test_ticker_refreshes_heartbeat_during_hold(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    with portalock.acquire(
        target, timeout=1.0, heartbeat_interval=0.02, hold_ceiling=100.0
    ) as lock:
        first = json.loads(lock.path.read_text())["heartbeat_at"]
        time.sleep(0.2)
        # No manual heartbeat() call: the background ticker did the refresh.
        second = json.loads(lock.path.read_text())["heartbeat_at"]
    assert second > first


def test_hold_ceiling_exceeded_warning_emitted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    with (
        caplog.at_level(logging.WARNING, logger="eawf.runtime.lock.portalock"),
        portalock.acquire(target, timeout=1.0, heartbeat_interval=0.02, hold_ceiling=0.1),
    ):
        time.sleep(0.3)
    ceiling = [r for r in caplog.records if "hold_ceiling_exceeded" in r.getMessage()]
    assert ceiling, "expected a hold_ceiling_exceeded warning"
    assert all(r.levelno == logging.WARNING for r in ceiling)
    assert any("duration_s=" in r.getMessage() for r in ceiling)


def test_hold_ceiling_not_warned_for_short_hold(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    with (
        caplog.at_level(logging.WARNING, logger="eawf.runtime.lock.portalock"),
        portalock.acquire(target, timeout=1.0, heartbeat_interval=0.02, hold_ceiling=5.0),
    ):
        time.sleep(0.1)
    assert not any("hold_ceiling_exceeded" in r.getMessage() for r in caplog.records)


def test_heartbeat_ticker_does_not_outlive_hold(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    with portalock.acquire(target, timeout=1.0, heartbeat_interval=0.02) as lock:
        held = [t.name for t in threading.enumerate()]
        assert any(n.startswith("eawf-lock-heartbeat") for n in held), held
        assert lock.path.exists()
    remaining = [t for t in threading.enumerate() if t.name.startswith("eawf-lock-heartbeat")]
    assert remaining == []
