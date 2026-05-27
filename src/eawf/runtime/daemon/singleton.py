"""Runtime singleton locks for the eawfd daemon."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import portalocker

logger = logging.getLogger(__name__)

DAEMON_SINGLETON_LOCK_NAME = "eawfd.lock"
SPAWN_LOCK_NAME = "eawfd.spawn.lock"
SPAWN_LOCK_POLL_INTERVAL_SECONDS = 0.05


class DaemonAlreadyRunningError(RuntimeError):
    """Raised when another daemon process already owns the runtime."""


class DaemonSpawnLockTimeoutError(RuntimeError):
    """Raised when a CLI cannot enter the spawn critical section."""


@dataclass(frozen=True)
class RuntimeLockHandle:
    """Held runtime lock handle."""

    path: Path
    fh: IO[str]


def daemon_singleton_lock_path(runtime_dir: Path) -> Path:
    """Return the lifetime daemon singleton lock path."""
    return runtime_dir / DAEMON_SINGLETON_LOCK_NAME


def spawn_lock_path(runtime_dir: Path) -> Path:
    """Return the short-lived auto-spawn coordination lock path."""
    return runtime_dir / SPAWN_LOCK_NAME


def _write_holder(fh: IO[str], *, lock_kind: str) -> None:
    """Write holder metadata into an acquired lock file."""
    body = {
        "pid": os.getpid(),
        "lock_kind": lock_kind,
        "started_at": datetime.now(UTC).isoformat(),
    }
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps(body))
    fh.flush()
    os.fsync(fh.fileno())


@contextmanager
def acquire_daemon_singleton(runtime_dir: Path) -> Iterator[RuntimeLockHandle]:
    """Acquire the lifetime daemon singleton lock for *runtime_dir*.

    Args:
        runtime_dir: Materialised daemon runtime directory.

    Raises:
        DaemonAlreadyRunningError: When another live process holds the
            singleton lock.
    """
    lock_path = daemon_singleton_lock_path(runtime_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh: IO[str] | None = None
    try:
        fh = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
        try:
            portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.LockException as exc:
            raise DaemonAlreadyRunningError(
                f"daemon runtime already locked: {runtime_dir.name!r}"
            ) from exc
        _write_holder(fh, lock_kind="daemon")
        yield RuntimeLockHandle(path=lock_path, fh=fh)
    finally:
        if fh is not None:
            with contextlib.suppress(Exception):
                portalocker.unlock(fh)
            with contextlib.suppress(Exception):
                fh.close()


def daemon_singleton_locked(runtime_dir: Path) -> bool:
    """Return True when another process holds the daemon singleton lock."""
    lock_path = daemon_singleton_lock_path(runtime_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh: IO[str] | None = None
    try:
        fh = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
        try:
            portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.LockException:
            return True
        return False
    finally:
        if fh is not None:
            with contextlib.suppress(Exception):
                portalocker.unlock(fh)
            with contextlib.suppress(Exception):
                fh.close()


@contextmanager
def acquire_spawn_lock(
    runtime_dir: Path,
    *,
    timeout_seconds: float,
) -> Iterator[RuntimeLockHandle]:
    """Acquire the short-lived auto-spawn coordination lock.

    Args:
        runtime_dir: Materialised daemon runtime directory.
        timeout_seconds: Max seconds to wait for the spawn lock.

    Raises:
        DaemonSpawnLockTimeoutError: When the spawn lock stays held past
            *timeout_seconds*.
    """
    if timeout_seconds <= 0:
        raise DaemonSpawnLockTimeoutError(f"timeout must be positive, got {timeout_seconds!r}")
    lock_path = spawn_lock_path(runtime_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    fh: IO[str] | None = None
    try:
        while True:
            fh = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
            try:
                portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
                break
            except portalocker.LockException:
                fh.close()
                fh = None
                if time.monotonic() >= deadline:
                    raise DaemonSpawnLockTimeoutError(
                        f"could not acquire daemon spawn lock within {timeout_seconds:.1f}s"
                    ) from None
                time.sleep(SPAWN_LOCK_POLL_INTERVAL_SECONDS)
        _write_holder(fh, lock_kind="spawn")
        yield RuntimeLockHandle(path=lock_path, fh=fh)
    finally:
        if fh is not None:
            with contextlib.suppress(Exception):
                portalocker.unlock(fh)
            with contextlib.suppress(Exception):
                fh.close()
