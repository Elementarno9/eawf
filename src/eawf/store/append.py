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

``LockTimeout`` is mapped to :class:`eawf.cli.errors.LockConflict` so the
CLI surfaces the canonical exit code.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from eawf.cli.errors import StateConflict
from eawf.lock import portalock
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


def append_envelope(path: Path, envelope: Envelope, *, timeout: float = 5.0) -> None:
    """Append *envelope* to the JSONL file at *path* under portalock + fsync.

    Creates the parent directory if missing. Acquires
    :func:`eawf.lock.portalock.acquire` on the JSONL file (sibling
    lockfile), appends one line, calls ``os.fsync`` on the file, releases.

    Raises:
        LockConflict: When the sibling lock cannot be acquired within
            *timeout* seconds.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = envelope.model_dump_json() + "\n"
    try:
        with portalock.acquire(path, timeout=timeout), path.open("ab") as fh:
            fh.write(line.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
    except portalock.LockTimeout as exc:
        raise StateConflict(
            f"could not acquire append lock for {path}: {exc}", kind="LockConflict"
        ) from exc
