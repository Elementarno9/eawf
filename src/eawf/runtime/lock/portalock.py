"""portalocker-backed exclusive advisory lock with stale-lock recovery.

Public API:
    acquire(target, *, timeout, on_event) -> LockHandle
    LockTimeout
    LockHandle
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import portalocker

from eawf.runtime.lock import sibling, stale

logger = logging.getLogger(__name__)


class LockTimeout(Exception):  # noqa: N818
    """Raised when the advisory lock cannot be acquired within the timeout."""


@dataclass
class LockHandle:
    """Active lock handle returned by :func:`acquire`."""

    target: Path
    path: Path
    fh: IO[str]
    on_event: Callable[[dict[str, object]], None] | None = field(default=None)

    def heartbeat(self) -> None:
        """Refresh *heartbeat_at* in the lockfile body, preserving identity fields."""
        try:
            raw = self.path.read_text(encoding="utf-8")
            body: dict[str, object] = json.loads(raw)
        except Exception:
            body = {}

        body["heartbeat_at"] = datetime.now(UTC).isoformat()
        new_raw = json.dumps(body)

        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(new_raw)
        self.fh.flush()
        os.fsync(self.fh.fileno())
        logger.debug(f"heartbeat updated path={self.path}")


def _write_holder(fh: IO[str], path: Path) -> None:
    """Write initial holder metadata to *fh* / *path*."""
    body = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": datetime.now(UTC).isoformat(),
        "heartbeat_at": datetime.now(UTC).isoformat(),
    }
    raw = json.dumps(body)
    fh.seek(0)
    fh.truncate()
    fh.write(raw)
    fh.flush()
    os.fsync(fh.fileno())


@contextmanager
def acquire(
    target: Path,
    *,
    timeout: float | None = None,
    on_event: Callable[[dict[str, object]], None] | None = None,
) -> Iterator[LockHandle]:
    """Acquire an exclusive advisory lock for *target*.

    The lockfile is placed at ``<target>.lock`` (sibling resolution).
    The context manager yields a :class:`LockHandle`; the lock is released
    on exit regardless of exceptions.

    Args:
        target: The file being protected (e.g. ``state.json``).
        timeout: Seconds to wait before raising :exc:`LockTimeout`.
                 Defaults to ``EA_LOCK_TIMEOUT`` env variable (default 5.0 s).
        on_event: Optional callback invoked with event dicts on notable
                  occurrences such as ``"lock_stolen"``.

    Raises:
        LockTimeout: When the lock cannot be acquired within *timeout*.
    """
    target = Path(target)
    lock_path = sibling.lock_path(target)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    effective_timeout = (
        timeout if timeout is not None else float(os.environ.get("EA_LOCK_TIMEOUT", "5.0"))
    )
    deadline = time.monotonic() + effective_timeout

    fh: IO[str] | None = None

    while True:
        # Stale-lock detection: steal if the existing lock is dead.
        if lock_path.exists() and stale.is_stale(lock_path):
            prev_holder: object = None
            with contextlib.suppress(Exception):
                prev_holder = json.loads(lock_path.read_text(encoding="utf-8"))

            if on_event is not None:
                on_event(
                    {
                        "event_type": "lock_stolen",
                        "lock_path": str(lock_path),
                        "stolen_from": prev_holder,
                        "occurred_at": datetime.now(UTC).isoformat(),
                    }
                )
            lock_path.unlink(missing_ok=True)
            logger.info(f"acquire stale-lock-stolen path={lock_path}")

        try:
            fh = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
            fh.seek(0)
            portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
            # Acquired — break out of retry loop.
            break
        except portalocker.LockException:
            if fh is not None:
                fh.close()
                fh = None
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"Could not acquire lock {lock_path} within {effective_timeout}s"
                ) from None
            time.sleep(0.05)
        except Exception:
            if fh is not None:
                fh.close()
                fh = None
            raise

    assert fh is not None  # guaranteed by loop logic above

    _write_holder(fh, lock_path)
    handle = LockHandle(
        target=target,
        path=lock_path,
        fh=fh,
        on_event=on_event,
    )

    try:
        yield handle
    finally:
        try:
            portalocker.unlock(fh)
        except Exception:
            logger.warning(f"acquire unlock-failed path={lock_path}", exc_info=True)
        with contextlib.suppress(Exception):
            fh.close()
        lock_path.unlink(missing_ok=True)
