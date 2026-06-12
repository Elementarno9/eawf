"""Cross-platform durable-write primitive: parent-directory fsync.

The atomic-write idiom across the codebase is tempfile -> ``fh.flush()``
+ ``os.fsync(fh.fileno())`` -> ``os.replace`` -> *fsync the parent
directory* so the rename itself is durable across a crash. The last
step opens the directory with ``os.open(parent, os.O_DIRECTORY)`` and
``os.fsync``-es the resulting descriptor.

That last step is POSIX-only: ``os.O_DIRECTORY`` does not exist on
Windows, and a directory handle there is not ``fsync``-able. The same
nine-line block was copy-pasted into nine writers (state, store,
config, backup, WAL, render); on Windows every one raised
``AttributeError`` at ``os.O_DIRECTORY`` access, so the daemon module
graph could not even import. This module is the single home for the
guarded form so the durability semantics live in one place and the
Windows skip is decided once.

The function is import-safe on every platform: it reads
``os.O_DIRECTORY`` only behind a ``hasattr`` guard at call time, never
at import, so importing this module on Windows is a no-op.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def fsync_parent_dir(path: Path) -> None:
    """Fsync the directory containing *path* so a prior rename is durable.

    The POSIX durability contract for an atomic write is: fsync the
    file, ``os.replace`` it into place, then fsync the *containing
    directory* so the directory entry for the rename survives a crash.
    This helper performs that final directory fsync.

    On platforms without ``os.O_DIRECTORY`` (Windows) the directory
    fsync is skipped: a directory handle there cannot be opened with
    that flag nor ``fsync``-ed, and ``os.replace`` already provides an
    atomic rename, so the file content (fsynced before the replace)
    remains durable. The skip is logged at debug so the difference in
    durability guarantee is observable.

    Args:
        path: The file path whose *parent* directory should be fsynced.
            The file itself is not touched -- callers fsync the file
            descriptor before calling this.
    """
    if not hasattr(os, "O_DIRECTORY"):
        # Windows: no directory fsync. os.replace is atomic and the file
        # bytes were already fsynced by the caller before the rename.
        logger.debug(f"fsync_parent_dir skip-no-o-directory parent={path.parent}")
        return
    parent_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


__all__ = ["fsync_parent_dir"]
