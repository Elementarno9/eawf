"""Atomic JSONL append under per-file portalock with fsync.

Single canonical helper used by every Eä JSONL writer. Replaces the
five surface-level duplicates that previously diverged across
``evidence/_io.py``, ``cli/commands/estimation.py``,
``cli/commands/lifecycle.py``, ``memory/store.py`` and
``session/store.py``.

The semantics mirror the original ``evidence/_io.py::append_jsonl`` (commit
``18ee287`` introduced the per-file portalock + fsync ordering): the
sibling lock for *path* is acquired with the canonical 5 s timeout so
concurrent appenders across processes serialise. The state.json
transaction (if any) holds a different sibling lock - there is no
deadlock risk because the two locks are on distinct files.

``LockTimeout`` is mapped to :class:`eawf.surfaces.cli.errors.StateConflict`
(``kind="LockConflict"``) so the CLI surfaces the canonical exit code.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from eawf.kernel.store.envelope import Envelope
from eawf.runtime.lock import portalock
from eawf.surfaces.cli.errors import StateConflict

logger = logging.getLogger(__name__)


def append_json_line(path: Path, line: str, *, timeout: float = 5.0) -> int:
    """Append *line* to the JSONL file at *path* under portalock + fsync.

    Creates the parent directory if missing. Acquires
    :func:`eawf.runtime.lock.portalock.acquire` on the JSONL file (sibling
    lockfile), appends one newline-terminated line, calls ``os.fsync`` on
    the file, releases.

    Args:
        path: The JSONL file to extend.
        line: One record, already serialized and without its newline.
        timeout: Seconds to wait for the sibling lock.

    Returns:
        The byte offset the line was written at, measured under the lock
        so a concurrent appender cannot make it stale.

    Raises:
        StateConflict: When the sibling lock cannot be acquired within
            *timeout* seconds (``kind="LockConflict"``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalock.acquire(path, timeout=timeout):
            return _write_line(path, line)
    except portalock.LockTimeout as exc:
        raise StateConflict(
            f"could not acquire append lock for {path}: {exc}", kind="LockConflict"
        ) from exc


def append_json_line_unless(
    path: Path, line: str, held: Callable[[], bool], *, timeout: float = 5.0
) -> bool:
    """Append *line* to the JSONL file at *path* unless *held* answers ``True``.

    *held* is asked under the same sibling lock the append takes, so no
    appender can land a line between the check and the write.

    Args:
        path: The JSONL file to extend.
        line: One record, already serialized and without its newline.
        held: Reads the file and answers whether *line* is already said.
        timeout: Seconds to wait for the sibling lock.

    Returns:
        Whether the line was appended.

    Raises:
        StateConflict: When the sibling lock cannot be acquired within
            *timeout* seconds (``kind="LockConflict"``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalock.acquire(path, timeout=timeout):
            if held():
                return False
            _write_line(path, line)
    except portalock.LockTimeout as exc:
        raise StateConflict(
            f"could not acquire append lock for {path}: {exc}", kind="LockConflict"
        ) from exc
    return True


def _write_line(path: Path, line: str) -> int:
    """Append *line* and its newline, fsynced; return the offset it starts at."""
    with path.open("ab") as fh:
        offset = fh.seek(0, os.SEEK_END)
        fh.write((line + "\n").encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    return offset


def append_envelope(path: Path, envelope: Envelope, *, timeout: float = 5.0) -> None:
    """Append *envelope* to the JSONL file at *path* under portalock + fsync.

    Raises:
        StateConflict: When the sibling lock cannot be acquired within
            *timeout* seconds (``kind="LockConflict"``).
    """
    append_json_line(path, envelope.model_dump_json(), timeout=timeout)
