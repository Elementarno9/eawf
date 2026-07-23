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
import threading
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

# Refresh cadence for the held-lock heartbeat ticker. Chosen well below
# stale.STALE_HEARTBEAT_SECONDS so several refreshes land inside the stale
# window and a live holder never ages into the steal path.
HEARTBEAT_INTERVAL_SECONDS: float = 15.0

# Hold duration past which a WARNING is logged. Observability only: no forced
# release -- escalation of a genuinely wedged holder is the watchdog's job.
HOLD_CEILING_SECONDS: float = 120.0


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


def _heartbeat_loop(
    handle: LockHandle,
    stop_event: threading.Event,
    *,
    interval: float,
    ceiling: float,
    start_monotonic: float,
) -> None:
    """Refresh *handle*'s heartbeat every *interval* seconds until *stop_event* is set.

    Runs on a daemon thread owned by :func:`acquire`. Keeping ``heartbeat_at``
    fresh is what lets a live holder outlive ``stale.STALE_HEARTBEAT_SECONDS``
    without being seen as stale by :func:`eawf.runtime.lock.stale.is_stale`.
    The first tick past *ceiling* logs one ``hold_ceiling_exceeded`` WARNING
    carrying ``duration_s`` -- observability only, no forced release.

    Args:
        handle: The active lock handle whose heartbeat is refreshed.
        stop_event: Set by :func:`acquire` on release to terminate the loop.
        interval: Seconds between heartbeat refreshes.
        ceiling: Hold-duration ceiling in seconds; the first tick past it warns.
        start_monotonic: ``time.monotonic()`` captured at acquire time.
    """
    ceiling_warned = False
    while not stop_event.wait(interval):
        try:
            handle.heartbeat()
        except Exception:
            # Surface the failure rather than swallow it silently; keep ticking
            # so a transient write error does not blind the stale detector.
            logger.warning(f"heartbeat refresh-failed path={handle.path}", exc_info=True)
        if not ceiling_warned:
            duration_s = time.monotonic() - start_monotonic
            if duration_s > ceiling:
                logger.warning(
                    f"hold_ceiling_exceeded path={handle.path} "
                    f"duration_s={duration_s:.1f} ceiling_s={ceiling:.1f}"
                )
                ceiling_warned = True


@contextmanager
def _heartbeat_ticker(
    handle: LockHandle,
    *,
    interval: float | None,
    ceiling: float | None,
) -> Iterator[None]:
    """Run the heartbeat ticker for the duration of the ``with`` block.

    Starts a daemon thread on enter and stops + joins it on exit, so the ticker
    can never outlive the lock hold or write through a closed descriptor.
    ``stop_event`` wakes the loop immediately regardless of *interval*, so the
    bounded join returns promptly.

    Args:
        handle: The active lock handle whose heartbeat is refreshed.
        interval: Seconds between refreshes; ``None`` uses the module default.
        ceiling: Hold-duration ceiling; ``None`` uses the module default.
    """
    effective_interval = interval if interval is not None else HEARTBEAT_INTERVAL_SECONDS
    effective_ceiling = ceiling if ceiling is not None else HOLD_CEILING_SECONDS
    stop_event = threading.Event()
    ticker = threading.Thread(
        target=_heartbeat_loop,
        args=(handle, stop_event),
        kwargs={
            "interval": effective_interval,
            "ceiling": effective_ceiling,
            "start_monotonic": time.monotonic(),
        },
        name=f"eawf-lock-heartbeat-{handle.path.name}",
        daemon=True,
    )
    ticker.start()
    try:
        yield
    finally:
        stop_event.set()
        ticker.join(timeout=5.0)
        if ticker.is_alive():
            logger.warning(f"acquire heartbeat-ticker-still-alive path={handle.path}")


@contextmanager
def acquire(
    target: Path,
    *,
    timeout: float | None = None,
    on_event: Callable[[dict[str, object]], None] | None = None,
    heartbeat_interval: float | None = None,
    hold_ceiling: float | None = None,
) -> Iterator[LockHandle]:
    """Acquire an exclusive advisory lock for *target*.

    The lockfile is placed at ``<target>.lock`` (sibling resolution).
    The context manager yields a :class:`LockHandle`; the lock is released
    on exit regardless of exceptions.

    While the lock is held a daemon thread refreshes ``heartbeat_at`` every
    *heartbeat_interval* seconds so a live holder never ages into the stale
    steal path, and logs a ``hold_ceiling_exceeded`` WARNING once a hold
    passes *hold_ceiling*. The ticker is stopped and joined before the handle
    is released, so it can never outlive the hold.

    Args:
        target: The file being protected (e.g. ``state.json``).
        timeout: Seconds to wait before raising :exc:`LockTimeout`.
                 Defaults to ``EA_LOCK_TIMEOUT`` env variable (default 5.0 s).
        on_event: Optional callback invoked with event dicts on notable
                  occurrences such as ``"lock_stolen"``.
        heartbeat_interval: Seconds between heartbeat refreshes while held.
                 Defaults to :data:`HEARTBEAT_INTERVAL_SECONDS`.
        hold_ceiling: Hold duration in seconds past which a WARNING is logged.
                 Defaults to :data:`HOLD_CEILING_SECONDS`.

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

    # The advisory lock is bound to this inode. Never unlink the path while a
    # contender may already have opened it: unlink/recreate lets two processes
    # lock different inodes under the same pathname. Inspect stale metadata only
    # after acquiring the stable inode, then overwrite it in place.
    fh.seek(0)
    previous_raw = fh.read()
    if previous_raw and stale.is_stale(lock_path):
        prev_holder: object = None
        with contextlib.suppress(Exception):
            prev_holder = json.loads(previous_raw)
        if on_event is not None:
            on_event(
                {
                    "event_type": "lock_stolen",
                    "lock_path": str(lock_path),
                    "stolen_from": prev_holder,
                    "occurred_at": datetime.now(UTC).isoformat(),
                }
            )
        logger.info(f"acquire stale-lock-recovered path={lock_path}")

    _write_holder(fh, lock_path)
    handle = LockHandle(
        target=target,
        path=lock_path,
        fh=fh,
        on_event=on_event,
    )

    try:
        # The ticker refreshes heartbeat_at while held and is stopped + joined
        # on exit, before the file handle is touched, so a live holder stays
        # un-stealable without the thread ever outliving the hold.
        with _heartbeat_ticker(handle, interval=heartbeat_interval, ceiling=hold_ceiling):
            yield handle
    finally:
        # Clear holder metadata while the lock is still held, but preserve the
        # inode permanently. The next contender opens and locks this same file.
        with contextlib.suppress(Exception):
            fh.seek(0)
            fh.truncate()
            fh.flush()
            os.fsync(fh.fileno())
        try:
            portalocker.unlock(fh)
        except Exception:
            logger.warning(f"acquire unlock-failed path={lock_path}", exc_info=True)
        with contextlib.suppress(Exception):
            fh.close()
