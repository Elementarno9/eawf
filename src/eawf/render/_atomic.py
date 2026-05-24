"""Internal: atomic text-file writes for the render layer.

Mirrors the tempfile + ``fsync`` + :func:`os.replace` + parent-dir ``fsync``
idiom from :mod:`eawf.kernel.state.writer._write_payload`. Used by both the
AGENTS.md and CLAUDE.md renderers — single implementation avoids drift.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from eawf.runtime.lock import portalock

logger = logging.getLogger(__name__)


# 5.0 s matches the rest of the codebase (e.g. :mod:`eawf.kernel.state.writer`,
# :func:`eawf.render.manifest.save_atomic`). Rendering is local file I/O —
# tempfile + ``os.replace`` + a single sibling lock — so this is generous.
# If a real-world workload trips ``LockTimeout``, raise to 10 s before
# touching anything else.
LOCK_TIMEOUT: float = 5.0


def atomic_write_text(target: Path, payload: str) -> None:
    """Write *payload* to *target* atomically under a sibling portalock.

    Procedure (mirrors :func:`eawf.kernel.state.writer._write_payload`):

    1. Acquire :func:`eawf.runtime.lock.portalock.acquire` on *target* with
       :data:`LOCK_TIMEOUT`.
    2. Encode *payload* as UTF-8 and write to a sibling tempfile
       ``<target>.tmp.<hex4>``.
    3. ``flush()`` + :func:`os.fsync` so bytes hit the platter.
    4. :func:`os.replace` — atomic POSIX/Windows rename.
    5. ``fsync`` the parent directory so the rename is durable.
    6. Release the lock; clean up the tempfile on any failure.

    Args:
        target: Destination path. Parent directories are created on demand.
        payload: Text to write. Encoded as UTF-8.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = target.with_name(f"{target.name}.tmp.{suffix}")
    encoded = payload.encode("utf-8")
    try:
        with portalock.acquire(target, timeout=LOCK_TIMEOUT):
            with tmp.open("wb") as fh:
                fh.write(encoded)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            parent_fd = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        logger.info(f"render_atomic target={target} bytes={len(encoded)}")
    finally:
        tmp.unlink(missing_ok=True)
