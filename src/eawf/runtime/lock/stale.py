"""Stale-lock detection for eawf advisory locks.

A lockfile is considered stale when:
- The file is missing or unparseable (treated as stale to allow recovery).
- The holder PID is no longer alive (ProcessLookupError on os.kill(pid, 0)).
- The ``heartbeat_at`` timestamp is older than STALE_HEARTBEAT_SECONDS.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from eawf.platform.process_liveness import pid_alive

logger = logging.getLogger(__name__)

STALE_HEARTBEAT_SECONDS: float = 60.0


def is_stale(lock_path: Path, *, raw: str | None = None) -> bool:
    """Return *True* when the lockfile at *lock_path* should be stolen.

    Missing or unparseable lockfiles are treated as stale (safe recovery).

    Args:
        lock_path: The lockfile to judge.
        raw: The body already read through a held handle. A caller holding
            the lock must pass it: on win32 the byte-range lock refuses a
            by-path read, which would make every held lock look malformed.
    """
    if raw is None and not lock_path.exists():
        return True

    try:
        if raw is None:
            raw = lock_path.read_text(encoding="utf-8")
        body: dict[str, object] = json.loads(raw)
    except Exception:
        logger.debug(f"is_stale lock-malformed path={lock_path}; treating as stale")
        return True

    # 1. Check heartbeat age.
    try:
        heartbeat_at = datetime.fromisoformat(str(body.get("heartbeat_at", "")))
        age = (datetime.now(UTC) - heartbeat_at).total_seconds()
        if age > STALE_HEARTBEAT_SECONDS:
            logger.debug(
                f"is_stale heartbeat-expired path={lock_path} age_s={age:.1f}; treating as stale"
            )
            return True
    except ValueError, TypeError:
        logger.debug(f"is_stale invalid-heartbeat path={lock_path}; treating as stale")
        return True

    # 2. Check whether the holder PID is alive.
    try:
        pid = int(str(body.get("pid", 0)))
    except ValueError, TypeError:
        return True

    if pid == 0:
        # pid==0 means "the process group" to os.kill; treat as stale.
        logger.debug(f"is_stale pid-zero path={lock_path}; treating as stale")
        return True

    if _holder_dead(pid):
        logger.debug(f"is_stale dead-pid path={lock_path} pid={pid}; treating as stale")
        return True
    return False


def _holder_dead(pid: int) -> bool:
    """Return *True* only when the lock holder *pid* is known to be gone."""
    if sys.platform == "win32":
        # ``os.kill(pid, 0)`` is a console Ctrl+C on win32 (CTRL_C_EVENT == 0).
        return not pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # PID does not exist.
        return True
    except PermissionError:
        # Process exists but belongs to another user - not stale.
        return False
    except OSError:
        # Other OS error: assume alive.
        return False
    return False
