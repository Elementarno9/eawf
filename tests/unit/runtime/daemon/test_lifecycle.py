"""Tests for explicit daemon start/restart orchestration."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.daemon import lifecycle
from eawf.runtime.daemon.lifecycle import (
    DaemonLifecycleError,
    restart_daemon,
    start_daemon,
    wait_for_daemon_exit,
    wait_for_daemon_stopped,
)
from eawf.runtime.daemon.service_install import ServiceEnvelope
from eawf.runtime.daemon.spawn import read_daemon_pid


def test_start_daemon_reuses_ready_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready process is returned without touching its service owner."""
    monkeypatch.setattr(lifecycle, "daemon_pid_if_ready", lambda _runtime: 41)
    monkeypatch.setattr(
        lifecycle,
        "_service_installed",
        lambda: (_ for _ in ()).throw(AssertionError("service probe should not run")),
    )

    result = start_daemon(runtime_dir=tmp_path)

    assert result.action == "already-running"
    assert result.pid == 41
    assert result.previous_pid == 41


def test_start_daemon_spawns_after_stale_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ready PID and no service routes through stale-safe auto-spawn."""
    monkeypatch.setattr(lifecycle, "daemon_pid_if_ready", lambda _runtime: None)
    monkeypatch.setattr(lifecycle, "_service_installed", lambda: (False, "none", False))
    monkeypatch.setattr(lifecycle, "auto_spawn_daemon", lambda _runtime: 42)

    result = start_daemon(runtime_dir=tmp_path)

    assert result.action == "started"
    assert result.pid == 42
    assert result.previous_pid is None


def test_start_daemon_loaded_service_uses_post_eviction_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loaded service is evicted once before its restart-only start path."""
    timeline: list[str] = []
    monkeypatch.setattr(lifecycle, "daemon_pid_if_ready", lambda _runtime: None)
    monkeypatch.setattr(lifecycle, "_service_installed", lambda: (True, "launchd", True))
    monkeypatch.setattr(
        lifecycle,
        "evict_supervised_agent",
        lambda: timeline.append("evict") or "target",
    )
    monkeypatch.setattr(
        lifecycle,
        "enable_service",
        lambda: (_ for _ in ()).throw(AssertionError("enable would boot out twice")),
    )
    monkeypatch.setattr(
        lifecycle,
        "restart_service",
        lambda: (
            timeline.append("restart")
            or ServiceEnvelope(
                event_type="daemon_service_enabled",
                platform="darwin",
                unit="dev.eawf.eawfd",
                pid=42,
            )
        ),
    )

    result = start_daemon(runtime_dir=tmp_path)

    assert timeline == ["evict", "restart"]
    assert result.pid == 42
    assert result.supervisor == "launchd"


def test_restart_daemon_unsupervised_orders_shutdown_wait_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupervised restart waits for old singleton departure before spawn."""
    timeline: list[str] = []
    monkeypatch.setattr(lifecycle, "daemon_pid_if_ready", lambda _runtime: 41)
    monkeypatch.setattr(lifecycle, "_service_installed", lambda: (False, "none", False))
    monkeypatch.setattr(
        lifecycle,
        "request_daemon_shutdown",
        lambda _runtime, **_kwargs: timeline.append("shutdown") or {"drained": True},
    )
    monkeypatch.setattr(
        lifecycle,
        "wait_for_daemon_stopped",
        lambda _runtime, **_kwargs: timeline.append("wait"),
    )
    monkeypatch.setattr(
        lifecycle,
        "auto_spawn_daemon",
        lambda _runtime: timeline.append("spawn") or 42,
    )

    result = restart_daemon(runtime_dir=tmp_path)

    assert timeline == ["shutdown", "wait", "spawn"]
    assert result.previous_pid == 41
    assert result.pid == 42
    assert result.action == "restarted"


def test_restart_daemon_loaded_service_evicts_before_enable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loaded launchd/systemd agent is evicted before current unit starts."""
    timeline: list[str] = []
    monkeypatch.setattr(lifecycle, "daemon_pid_if_ready", lambda _runtime: 41)
    monkeypatch.setattr(lifecycle, "_service_installed", lambda: (True, "launchd", True))
    monkeypatch.setattr(
        lifecycle,
        "evict_supervised_agent",
        lambda: timeline.append("evict") or "target",
    )
    monkeypatch.setattr(
        lifecycle,
        "request_daemon_shutdown",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("supervisor owns shutdown")),
    )
    monkeypatch.setattr(
        lifecycle,
        "wait_for_daemon_stopped",
        lambda _runtime, **_kwargs: timeline.append("wait"),
    )
    monkeypatch.setattr(
        lifecycle,
        "restart_service",
        lambda: (
            timeline.append("enable")
            or ServiceEnvelope(
                event_type="daemon_service_enabled",
                platform="darwin",
                unit="dev.eawf.eawfd",
                pid=42,
            )
        ),
    )

    result = restart_daemon(runtime_dir=tmp_path)

    assert timeline == ["evict", "wait", "enable"]
    assert result.supervisor == "launchd"
    assert result.previous_pid == 41
    assert result.pid == 42


def test_restart_daemon_rejects_reused_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returning old PID is not a successful restart."""
    monkeypatch.setattr(lifecycle, "daemon_pid_if_ready", lambda _runtime: 41)
    monkeypatch.setattr(lifecycle, "_service_installed", lambda: (False, "none", False))
    monkeypatch.setattr(
        lifecycle,
        "request_daemon_shutdown",
        lambda _runtime, **_kwargs: {"drained": True},
    )
    monkeypatch.setattr(lifecycle, "wait_for_daemon_stopped", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifecycle, "auto_spawn_daemon", lambda _runtime: 41)

    with pytest.raises(DaemonLifecycleError, match="reused old pid"):
        restart_daemon(runtime_dir=tmp_path)


@pytest.mark.parametrize("timeout_seconds", [0, 601])
def test_restart_daemon_rejects_timeout_boundary(
    tmp_path: Path,
    timeout_seconds: int,
) -> None:
    """Restart timeout is restricted to public CLI bounds."""
    with pytest.raises(DaemonLifecycleError, match="between 1 and 600"):
        restart_daemon(runtime_dir=tmp_path, timeout_seconds=timeout_seconds)


def test_wait_for_daemon_stopped_rejects_non_positive_timeout(tmp_path: Path) -> None:
    """Stop waiter rejects an unbounded/immediate-invalid window."""
    with pytest.raises(DaemonLifecycleError, match="must be positive"):
        wait_for_daemon_stopped(tmp_path, previous_pid=41, timeout_seconds=0)


def _script_lock(monkeypatch: pytest.MonkeyPatch, states: list[bool]) -> list[bool]:
    """Answer ``daemon_singleton_locked`` from *states*, repeating the last one."""
    seen: list[bool] = []

    def _locked(_runtime: Path) -> bool:
        state = states[min(len(seen), len(states) - 1)]
        seen.append(state)
        return state

    monkeypatch.setattr(lifecycle, "daemon_singleton_locked", _locked)
    return seen


def _script_pid(monkeypatch: pytest.MonkeyPatch, pid: int | None) -> None:
    monkeypatch.setattr(lifecycle, "read_daemon_pid", lambda _runtime: pid)


def test_wait_for_daemon_exit_returns_at_once_on_a_free_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _script_lock(monkeypatch, [False])
    _script_pid(monkeypatch, 41)

    wait_for_daemon_exit(tmp_path, previous_pid=41, timeout_seconds=5.0)

    assert seen == [False]


def test_wait_for_daemon_exit_free_lock_with_zero_timeout_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero window still checks the lock once before giving up."""
    _script_lock(monkeypatch, [False])
    _script_pid(monkeypatch, None)

    wait_for_daemon_exit(tmp_path, previous_pid=None, timeout_seconds=0.0)


def test_wait_for_daemon_exit_returns_once_the_lock_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _script_lock(monkeypatch, [True, True, False])
    _script_pid(monkeypatch, 41)

    wait_for_daemon_exit(tmp_path, previous_pid=41, timeout_seconds=5.0)

    assert seen == [True, True, False]


def test_wait_for_daemon_exit_waits_while_the_pid_file_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown removes the pid file before it frees the lock; keep waiting."""
    seen = _script_lock(monkeypatch, [True, False])
    _script_pid(monkeypatch, None)

    wait_for_daemon_exit(tmp_path, previous_pid=41, timeout_seconds=5.0)

    assert seen == [True, False]


def test_wait_for_daemon_exit_raises_naming_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "EXIT_WAIT_FLOOR_SECONDS", 0.0)
    seen = _script_lock(monkeypatch, [True])
    _script_pid(monkeypatch, 41)

    with pytest.raises(DaemonLifecycleError, match=r"did not exit within 0\.2s"):
        wait_for_daemon_exit(tmp_path, previous_pid=41, timeout_seconds=0.2)

    assert len(seen) >= 2


def test_wait_for_daemon_exit_zero_timeout_on_a_held_lock_raises_at_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero window still waits out the floor before it gives up."""
    monkeypatch.setattr(lifecycle, "EXIT_WAIT_FLOOR_SECONDS", 0.2)
    seen = _script_lock(monkeypatch, [True])
    _script_pid(monkeypatch, 41)

    with pytest.raises(DaemonLifecycleError, match=r"within 0\.2s"):
        wait_for_daemon_exit(tmp_path, previous_pid=41, timeout_seconds=0.0)

    assert len(seen) >= 2


def test_wait_for_daemon_exit_zero_timeout_waits_for_a_lock_released_soon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero drain window is no reason to give up on a daemon still exiting."""
    seen = _script_lock(monkeypatch, [True, True, True, False])
    _script_pid(monkeypatch, 41)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)

    wait_for_daemon_exit(tmp_path, previous_pid=41, timeout_seconds=0.0)

    assert seen == [True, True, True, False]


def test_wait_for_daemon_exit_timeout_above_the_floor_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "EXIT_WAIT_FLOOR_SECONDS", 0.1)
    _script_lock(monkeypatch, [True])
    _script_pid(monkeypatch, 41)

    with pytest.raises(DaemonLifecycleError, match=r"within 0\.3s"):
        wait_for_daemon_exit(tmp_path, previous_pid=41, timeout_seconds=0.3)


def test_wait_for_daemon_exit_polls_at_the_stop_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script_lock(monkeypatch, [True, True, False])
    _script_pid(monkeypatch, 41)
    sleeps: list[float] = []
    monkeypatch.setattr(lifecycle.time, "sleep", sleeps.append)

    wait_for_daemon_exit(tmp_path, previous_pid=41, timeout_seconds=5.0)

    assert sleeps == [lifecycle.STOP_POLL_INTERVAL_SECONDS] * 2


def test_wait_for_daemon_exit_returns_on_a_successor_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successor another client spawned holds the lock under a new pid."""
    seen = _script_lock(monkeypatch, [True])
    _script_pid(monkeypatch, 42)

    wait_for_daemon_exit(tmp_path, previous_pid=41, timeout_seconds=5.0)

    assert seen == [True]


def test_wait_for_daemon_exit_successor_without_a_previous_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script_lock(monkeypatch, [True])
    _script_pid(monkeypatch, 42)

    wait_for_daemon_exit(tmp_path, previous_pid=None, timeout_seconds=5.0)


def test_wait_for_daemon_exit_successor_check_keeps_waiting_on_the_same_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pid file still naming the stopping daemon is no successor."""
    monkeypatch.setattr(lifecycle, "EXIT_WAIT_FLOOR_SECONDS", 0.0)
    _script_lock(monkeypatch, [True])
    _script_pid(monkeypatch, 41)

    with pytest.raises(DaemonLifecycleError, match="did not exit"):
        wait_for_daemon_exit(tmp_path, previous_pid=41, timeout_seconds=0.15)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("4242\n1\n2026-01-01T00:00:00Z\n", 4242),
        ("7", 7),
        ("", None),
        ("not-a-pid\n", None),
    ],
)
def test_read_daemon_pid_parses_the_first_line(
    tmp_path: Path, content: str, expected: int | None
) -> None:
    (tmp_path / "eawfd.pid").write_text(content, encoding="utf-8")

    assert read_daemon_pid(tmp_path) == expected


def test_read_daemon_pid_without_a_pid_file_is_none(tmp_path: Path) -> None:
    assert read_daemon_pid(tmp_path) is None
