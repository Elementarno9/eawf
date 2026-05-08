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
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STALE_HEARTBEAT_SECONDS: float = 60.0


def is_stale(lock_path: Path) -> bool:
    """Return *True* when the lockfile at *lock_path* should be stolen.

    Missing or unparseable lockfiles are treated as stale (safe recovery).
    """
    if not lock_path.exists():
        return True

    try:
        raw = lock_path.read_text(encoding="utf-8")
        body: dict[str, object] = json.loads(raw)
    except Exception:
        logger.debug(f"Lock file {lock_path} is malformed; treating as stale")
        return True

    # 1. Check heartbeat age.
    try:
        heartbeat_at = datetime.fromisoformat(str(body.get("heartbeat_at", "")))
        age = (datetime.now(UTC) - heartbeat_at).total_seconds()
        if age > STALE_HEARTBEAT_SECONDS:
            logger.debug(f"Lock {lock_path} heartbeat {age:.1f}s old; treating as stale")
            return True
    except ValueError, TypeError:
        logger.debug(f"Lock {lock_path} has invalid heartbeat_at; treating as stale")
        return True

    # 2. Check whether the holder PID is alive.
    try:
        pid = int(str(body.get("pid", 0)))
    except ValueError, TypeError:
        return True

    if pid == 0:
        # pid==0 means "the process group" to os.kill; treat as stale.
        logger.debug(f"Lock {lock_path} has pid=0; treating as stale")
        return True

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # PID does not exist.
        logger.debug(f"Lock {lock_path} held by dead PID {pid}; treating as stale")
        return True
    except PermissionError:
        # Process exists but belongs to another user - not stale.
        return False
    except OSError:
        # Other OS error: assume alive.
        return False

    return False
