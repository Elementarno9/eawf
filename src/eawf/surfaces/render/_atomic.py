"""Internal: atomic text-file writes for the render layer.

Mirrors the tempfile + ``fsync`` + :func:`os.replace` + parent-dir ``fsync``
idiom from :mod:`eawf.kernel.state.writer._write_payload`. Used by both the
AGENTS.md and CLAUDE.md renderers — single implementation avoids drift.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import tempfile
from pathlib import Path

from eawf.kernel.fsync import fsync_parent_dir
from eawf.runtime.lock import portalock

logger = logging.getLogger(__name__)


# 5.0 s matches the rest of the codebase (e.g. :mod:`eawf.kernel.state.writer`,
# :func:`eawf.surfaces.render.manifest.save_atomic`). Rendering is local file I/O —
# tempfile + ``os.replace`` + a single sibling lock — so this is generous.
# If a real-world workload trips ``LockTimeout``, raise to 10 s before
# touching anything else.
LOCK_TIMEOUT: float = 5.0


def _render_lock_target(target: Path) -> Path:
    """Return the deterministic control-plane lock target for *target*.

    Rendered files often live in runtime-discovered or published trees
    (``.claude/``, ``.opencode/``, and packaged plugin roots). The canonical
    portalock deliberately preserves its lock inode after release, so using the
    rendered file itself as the lock target would leak ``*.lock`` artifacts
    into those trees.

    Keep the stable inode in a user-scoped temporary control-plane namespace
    instead. Both the user scope and canonical target path are hashed: callers
    targeting the same file resolve the same lock across processes without
    exposing a local path in the lock filename.

    Args:
        target: Rendered destination whose write must be serialised.

    Returns:
        A control-plane target outside the rendered tree. The portalock helper
        appends its own ``.lock`` suffix to this path.
    """
    if hasattr(os, "getuid"):
        user_scope = f"uid-{os.getuid()}"
    else:
        home_bytes = os.fsencode(str(Path.home().resolve()))
        user_scope = hashlib.sha256(home_bytes).hexdigest()[:16]
    namespace = Path(tempfile.gettempdir()) / "eawf-render-locks" / user_scope
    namespace.mkdir(mode=0o700, parents=True, exist_ok=True)

    canonical_target = os.fsencode(str(target.expanduser().resolve()))
    target_key = hashlib.sha256(canonical_target).hexdigest()
    return namespace / target_key


def atomic_write_text(target: Path, payload: str) -> None:
    """Write *payload* to *target* atomically under a control-plane portalock.

    Procedure (mirrors :func:`eawf.kernel.state.writer._write_payload`):

    1. Resolve a deterministic lock target outside the rendered tree, then
       acquire :func:`eawf.runtime.lock.portalock.acquire` with
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
    lock_target = _render_lock_target(target)
    try:
        with portalock.acquire(lock_target, timeout=LOCK_TIMEOUT):
            with tmp.open("wb") as fh:
                fh.write(encoded)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            fsync_parent_dir(target)
        logger.info(f"render_atomic target={target} bytes={len(encoded)}")
    finally:
        tmp.unlink(missing_ok=True)
