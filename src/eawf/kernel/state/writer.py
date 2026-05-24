"""Atomic JSON writer with sibling-lock protection.

Public API:
    atomic_write_json(target, data) -> None
        Acquires the sibling lock around the write — convenient when the
        caller is *not* already holding the lock.
    atomic_write_json_locked(target, data) -> None
        Skips lock acquisition. The caller must already hold the lock for
        ``target`` (e.g. a CLI handler doing read-modify-write under
        ``with portalock.acquire(target):``). Required because ``flock`` is
        not re-entrant across different open file descriptors in the same
        process — calling :func:`atomic_write_json` from inside an outer
        ``acquire`` deadlocks until the timeout fires.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import orjson

from eawf.runtime.lock import portalock

logger = logging.getLogger(__name__)


def _write_payload(target: Path, payload: bytes) -> None:
    """Tempfile + ``os.replace`` + parent-dir fsync. Lock-agnostic."""
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = target.with_name(f"{target.name}.tmp.{suffix}")
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
        logger.info(f"atomic_write_json wrote target={target} bytes={len(payload)}")
    finally:
        tmp.unlink(missing_ok=True)


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
    payload = orjson.dumps(dict(data), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n"
    with portalock.acquire(target, timeout=5.0):
        _write_payload(target, payload)


def atomic_write_json_locked(target: Path, data: Mapping[str, Any]) -> None:
    """Write *data* to *target* atomically *without* re-acquiring the sibling lock.

    Use from inside a ``with portalock.acquire(target):`` block when the
    caller already holds the lock for read-modify-write semantics.
    """
    target = Path(target)
    payload = orjson.dumps(dict(data), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n"
    _write_payload(target, payload)
