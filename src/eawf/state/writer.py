"""Atomic JSON writer with sibling-lock protection.

Public API:
    atomic_write_json(target, data) -> None
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import orjson

from eawf.lock import portalock

logger = logging.getLogger(__name__)


def atomic_write_json(target: Path, data: Mapping[str, Any]) -> None:
    """Write *data* to *target* atomically under an exclusive sibling lock.

    Procedure:
    1. Acquire sibling lock on *target* (timeout 5 s).
    2. Serialise *data* to a temp file ``<target>.tmp.<hex4>`` in the same dir.
    3. ``fh.flush()`` + ``os.fsync(fh.fileno())`` to durably write to disk.
    4. ``os.replace(tmp, target)`` — atomic rename on POSIX/Windows.
    5. Release lock via context-manager exit; clean up temp on any failure.

    Args:
        target: Destination path (need not exist; parent dirs are created).
        data:   JSON-serialisable mapping to persist.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = target.with_name(f"{target.name}.tmp.{suffix}")
    payload = orjson.dumps(dict(data), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    with portalock.acquire(target, timeout=None):
        try:
            with tmp.open("wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            parent_fd = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            logger.info(f"atomic_write_json wrote {target} bytes={len(payload)}")
        finally:
            tmp.unlink(missing_ok=True)
