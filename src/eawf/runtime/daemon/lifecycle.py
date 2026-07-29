"""Explicit start/restart orchestration for the eawfd daemon."""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from eawf.runtime.daemon.runtime_dir import runtime_dir as default_runtime_dir
from eawf.runtime.daemon.service_install import (
    ServiceInstallError,
    ServiceStatus,
    detect_supervised_agent,
    enable_service,
    evict_supervised_agent,
    restart_service,
    service_status,
)
from eawf.runtime.daemon.singleton import daemon_singleton_locked
from eawf.runtime.daemon.spawn import (
    DaemonSpawnTimeoutError,
    auto_spawn_daemon,
    daemon_pid_if_ready,
    request_daemon_shutdown,
)

logger = logging.getLogger(__name__)

DEFAULT_STOP_TIMEOUT_SECONDS: float = 30.0
STOP_POLL_INTERVAL_SECONDS: float = 0.05


class DaemonLifecycleError(RuntimeError):
    """Explicit daemon start/restart failed."""


@dataclass(frozen=True)
class DaemonLifecycleResult:
    """Structured daemon lifecycle outcome."""

    action: str
    pid: int
    previous_pid: int | None
    supervisor: str


def _service_installed() -> tuple[bool, str, bool]:
    """Return ``(installed, supervisor, loaded)`` for the current host."""
    if os.environ.get("EAWF_RUNTIME_DIR"):
        # An explicit runtime override denotes an isolated/manual daemon
        # (tests, recovery shells, side-by-side instances). Never mutate the
        # user's global launchd/systemd/SCM registration for that runtime.
        return False, "none", False
    report = detect_supervised_agent()
    if report.installed:
        return True, report.supervisor, report.loaded
    if sys.platform == "win32":
        status = service_status()
        installed = status not in {ServiceStatus.NOT_INSTALLED, ServiceStatus.UNSUPPORTED}
        return installed, "windows" if installed else "none", status == ServiceStatus.RUNNING
    return False, report.supervisor, report.loaded


def wait_for_daemon_stopped(
    runtime_dir: Path,
    *,
    previous_pid: int,
    timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS,
) -> None:
    """Wait until the old daemon releases both RPC readiness and singleton lock.

    Args:
        runtime_dir: Daemon runtime directory.
        previous_pid: PID expected to leave.
        timeout_seconds: Positive bounded wait.

    Raises:
        DaemonLifecycleError: When another daemon races the restart or the old
            process does not release its runtime before the deadline.
    """
    if timeout_seconds <= 0:
        raise DaemonLifecycleError(f"timeout_seconds must be positive, got {timeout_seconds!r}")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        ready_pid = daemon_pid_if_ready(runtime_dir)
        locked = daemon_singleton_locked(runtime_dir)
        if ready_pid is None and not locked:
            return
        if ready_pid is not None and ready_pid != previous_pid:
            raise DaemonLifecycleError(
                f"daemon restart raced another process: expected {previous_pid}, got {ready_pid}"
            )
        time.sleep(STOP_POLL_INTERVAL_SECONDS)
    raise DaemonLifecycleError(
        f"daemon did not stop within {timeout_seconds:.1f}s pid={previous_pid}"
    )


def start_daemon(*, runtime_dir: Path | None = None) -> DaemonLifecycleResult:
    """Ensure a daemon is ready, respecting an installed OS supervisor."""
    resolved = runtime_dir if runtime_dir is not None else default_runtime_dir()
    previous_pid = daemon_pid_if_ready(resolved)
    if previous_pid is not None:
        return DaemonLifecycleResult(
            action="already-running",
            pid=previous_pid,
            previous_pid=previous_pid,
            supervisor="none",
        )
    installed, supervisor, loaded = _service_installed()
    try:
        if installed:
            if loaded and supervisor != "windows":
                evict_supervised_agent()
            envelope = restart_service() if supervisor == "windows" else enable_service()
            if envelope.pid is None:
                raise DaemonLifecycleError("service start returned no daemon pid")
            return DaemonLifecycleResult(
                action="started",
                pid=envelope.pid,
                previous_pid=None,
                supervisor=supervisor,
            )
        pid = auto_spawn_daemon(resolved)
    except (DaemonSpawnTimeoutError, ServiceInstallError) as exc:
        raise DaemonLifecycleError(str(exc)) from exc
    return DaemonLifecycleResult(
        action="started",
        pid=pid,
        previous_pid=None,
        supervisor="none",
    )


def _restart_service_owned(
    runtime_dir: Path,
    *,
    previous_pid: int | None,
    supervisor: str,
    loaded: bool,
    drain: bool,
    timeout_seconds: int,
) -> int:
    """Restart a daemon whose lifecycle belongs to an OS supervisor."""
    if supervisor == "windows":
        envelope = restart_service()
    else:
        if loaded:
            evict_supervised_agent()
        elif previous_pid is not None:
            request_daemon_shutdown(
                runtime_dir,
                drain=drain,
                timeout_seconds=timeout_seconds,
            )
        if previous_pid is not None:
            wait_for_daemon_stopped(
                runtime_dir,
                previous_pid=previous_pid,
                timeout_seconds=float(timeout_seconds),
            )
        envelope = restart_service()
    if envelope.pid is None:
        raise DaemonLifecycleError("service restart returned no daemon pid")
    return envelope.pid


def _restart_unsupervised(
    runtime_dir: Path,
    *,
    previous_pid: int | None,
    drain: bool,
    timeout_seconds: int,
) -> int:
    """Restart an on-demand daemon after its old singleton fully leaves."""
    if previous_pid is not None:
        request_daemon_shutdown(
            runtime_dir,
            drain=drain,
            timeout_seconds=timeout_seconds,
        )
        wait_for_daemon_stopped(
            runtime_dir,
            previous_pid=previous_pid,
            timeout_seconds=float(timeout_seconds),
        )
    return auto_spawn_daemon(runtime_dir)


def restart_daemon(
    *,
    runtime_dir: Path | None = None,
    drain: bool = True,
    timeout_seconds: int = 30,
) -> DaemonLifecycleResult:
    """Restart daemon through its supervisor or the on-demand spawn path.

    Args:
        runtime_dir: Optional runtime override.
        drain: Whether an unsupervised daemon drains in-flight mutations.
        timeout_seconds: Shutdown and stop wait in seconds.

    Returns:
        Structured result naming old/new PIDs and lifecycle owner.

    Raises:
        DaemonLifecycleError: When shutdown, supervisor restart, or readiness
            fails.
    """
    if timeout_seconds < 1 or timeout_seconds > 600:
        raise DaemonLifecycleError(
            f"timeout_seconds must be between 1 and 600, got {timeout_seconds!r}"
        )
    resolved = runtime_dir if runtime_dir is not None else default_runtime_dir()
    previous_pid = daemon_pid_if_ready(resolved)
    installed, supervisor, loaded = _service_installed()
    try:
        if installed:
            pid = _restart_service_owned(
                resolved,
                previous_pid=previous_pid,
                supervisor=supervisor,
                loaded=loaded,
                drain=drain,
                timeout_seconds=timeout_seconds,
            )
        else:
            pid = _restart_unsupervised(
                resolved,
                previous_pid=previous_pid,
                drain=drain,
                timeout_seconds=timeout_seconds,
            )
    except (DaemonSpawnTimeoutError, ServiceInstallError, RuntimeError, ValueError) as exc:
        if isinstance(exc, DaemonLifecycleError):
            raise
        raise DaemonLifecycleError(str(exc)) from exc
    if previous_pid is not None and pid == previous_pid:
        raise DaemonLifecycleError(f"daemon restart reused old pid: {pid}")
    logger.info(f"restart_daemon supervisor={supervisor!r} previous_pid={previous_pid!r} pid={pid}")
    return DaemonLifecycleResult(
        action="restarted" if previous_pid is not None else "started",
        pid=pid,
        previous_pid=previous_pid,
        supervisor=supervisor if installed else "none",
    )


__all__ = [
    "DaemonLifecycleError",
    "DaemonLifecycleResult",
    "restart_daemon",
    "start_daemon",
    "wait_for_daemon_stopped",
]
