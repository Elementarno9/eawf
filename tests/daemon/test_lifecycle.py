"""Tests for explicit daemon start/restart orchestration."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.daemon import lifecycle
from eawf.runtime.daemon.lifecycle import (
    DaemonLifecycleError,
    restart_daemon,
    start_daemon,
    wait_for_daemon_stopped,
)
from eawf.runtime.daemon.service_install import ServiceEnvelope


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
