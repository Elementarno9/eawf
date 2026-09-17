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
    assert lock.path.exists()
    assert lock.path.read_text() == ""


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


def test_lockfile_inode_persists_across_contexts(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    lock_path: Path | None = None
    with portalock.acquire(target, timeout=1.0) as lock:
        lock_path = lock.path
        assert lock_path.exists()
        inode = lock_path.stat().st_ino
    assert lock_path is not None
    assert lock_path.exists()
    assert lock_path.read_text() == ""
    with portalock.acquire(target, timeout=1.0) as reacquired:
        assert reacquired.path.stat().st_ino == inode


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


def _read_heartbeat_at(path: Path) -> str:
    """Return the lockfile's ``heartbeat_at``, reading across an in-place rewrite.

    The ticker refreshes the lockfile through the held handle (truncate, then
    write) and this reader holds no lock, so a read can land in the empty
    window between the two. Only an unlocked observer can see that window;
    the writer keeps the inode the advisory lock is bound to, so it must not
    swap in a new file instead.
    """
    for _ in range(200):
        try:
            return str(json.loads(path.read_text())["heartbeat_at"])
        except json.JSONDecodeError:
            time.sleep(0.001)
    raise AssertionError(f"lockfile never parsed: {path.read_text()!r}")


def test_read_heartbeat_at_reads_across_the_empty_rewrite_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lockfile = tmp_path / "state.json.lock"
    lockfile.write_text(json.dumps({"heartbeat_at": "2026-09-16T00:00:00+00:00"}))
    real_read_text = Path.read_text
    reads: list[int] = []

    def _first_read_mid_rewrite(self: Path, *args: object, **kwargs: object) -> str:
        reads.append(1)
        if len(reads) == 1:
            return ""
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _first_read_mid_rewrite)

    assert _read_heartbeat_at(lockfile) == "2026-09-16T00:00:00+00:00"
    assert len(reads) == 2


def _poll_heartbeat_change(path: Path, previous: str, *, deadline_s: float) -> str:
    """Return the first ``heartbeat_at`` that differs from *previous*.

    A single read after a fixed sleep asks whether the ticker ran within that
    sleep, which a loaded host answers at random. Polling up to a generous
    deadline asks only whether it ran at all.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        current = _read_heartbeat_at(path)
        if current != previous:
            return current
        time.sleep(0.01)
    raise AssertionError(f"heartbeat_at stayed {previous!r} for {deadline_s}s")


def test_poll_heartbeat_change_raises_when_heartbeat_never_changes(tmp_path: Path) -> None:
    lockfile = tmp_path / "state.json.lock"
    lockfile.write_text(json.dumps({"heartbeat_at": "2026-09-16T00:00:00+00:00"}))

    with pytest.raises(AssertionError, match="stayed"):
        _poll_heartbeat_change(lockfile, "2026-09-16T00:00:00+00:00", deadline_s=0.05)


def test_ticker_refreshes_heartbeat_during_hold(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    with portalock.acquire(
        target, timeout=1.0, heartbeat_interval=0.02, hold_ceiling=100.0
    ) as lock:
        first = _read_heartbeat_at(lock.path)
        # No manual heartbeat() call: the background ticker did the refresh.
        second = _poll_heartbeat_change(lock.path, first, deadline_s=5.0)
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
