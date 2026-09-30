"""Resolve the daemon runtime directory.

Locates per-user storage for the daemon's PID file, Unix domain socket, log
file, and write-ahead log entries. Pure helpers — no filesystem side effects;
callers materialise the directory when they need it.

Resolution rules:

- ``EAWF_RUNTIME_DIR`` names the directory verbatim.
- Otherwise the directory is ``<base>/trees/<key>/``, one per tree, where
  ``<key>`` digests the ``state.json`` the daemon would bind to, so a daemon
  answers only the clients of the tree it serves.
- ``<base>`` is ``$XDG_RUNTIME_DIR/eawfd/`` on Linux when
  ``XDG_RUNTIME_DIR`` is set, and ``~/.eawfd/`` everywhere else. On Windows
  the listener itself is a named pipe at ``\\\\.\\pipe\\eawfd-<user>``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path

from eawf.kernel.state.resolve import resolve_with_reason

logger = logging.getLogger(__name__)

# Owner-only perms for the runtime directory. The directory holds the PID
# file, Unix socket, daemon log, and WAL — all of which can leak the
# operator's cwd / state paths — so other local users must not traverse
# or list it. POSIX-only; chmod semantics differ on Windows where the
# named-pipe transport gates access via DACL/SID instead.
RUNTIME_DIR_MODE: int = 0o700

#: Subdirectory of the base runtime dir that holds one directory per tree.
TREES_DIRNAME: str = "trees"

#: Hex digits of the tree key. Sixteen keep ``<base>/trees/<key>/eawfd.sock``
#: well inside the 104-byte AF_UNIX path cap while making a collision between
#: two trees of one user negligible.
_TREE_KEY_CHARS: int = 16


def runtime_base_dir() -> Path:
    """Return the per-user directory that holds every tree's runtime dir.

    The same directory was, before runtime dirs were keyed per tree, the one
    runtime dir every daemon of the user bound to, so a daemon still bound
    at ``<base>/eawfd.sock`` is one started under that layout.

    Returns:
        ``$XDG_RUNTIME_DIR/eawfd`` on Linux when ``XDG_RUNTIME_DIR`` is set,
        else ``~/.eawfd``.
    """
    if sys.platform.startswith("linux"):
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            return Path(xdg) / "eawfd"
    return Path.home() / ".eawfd"


def tree_key(state_path: Path) -> str:
    """Return the runtime-dir key of the tree whose ledger is *state_path*.

    Args:
        state_path: The ``state.json`` a daemon binds to.

    Returns:
        A short hex digest of the resolved path.
    """
    digest = hashlib.sha256(str(state_path.resolve()).encode("utf-8")).hexdigest()
    return digest[:_TREE_KEY_CHARS]


def runtime_dir() -> Path:
    """Return the runtime directory the daemon should use.

    Resolution order:

    1. ``EAWF_RUNTIME_DIR`` env var — explicit operator/test override,
       and the pin a spawned daemon inherits from the client that
       spawned it. The value is used verbatim; the caller is responsible
       for choosing a path short enough for AF_UNIX (104-byte cap on
       macOS) and for ensuring write access.
    2. ``<base>/trees/<key>`` where *key* names the ``state.json`` the
       daemon resolves (``EA_STATE``, else upward from the working
       directory), so clients of two trees never reach one daemon.

    Returns:
        Path to the daemon runtime directory. Caller is responsible for
        ensuring it exists with ``Path.mkdir`` when a write is imminent.
    """
    override = os.environ.get("EAWF_RUNTIME_DIR")
    if override:
        return Path(override)
    # The daemon resolves its tree with ``workspace=None`` too, so the key a
    # client dials is the key of the tree the daemon it spawns will serve.
    state_path, _reason = resolve_with_reason(workspace=None)
    return runtime_base_dir() / TREES_DIRNAME / tree_key(state_path)


def harden_runtime_dir(path: Path) -> None:
    """Lock *path* down to owner-only perms on POSIX.

    The runtime directory holds the PID file, Unix socket, log, and WAL —
    artifacts that embed the operator's cwd / state paths — so it must not
    be traversable or listable by other local users. No-op on Windows
    where chmod does not map onto the NTFS ACL model the named-pipe
    transport relies on.

    Args:
        path: Existing runtime directory to chmod.
    """
    if os.name == "nt":
        return
    os.chmod(path, RUNTIME_DIR_MODE)


def ensure_runtime_dir() -> Path:
    """Materialise the runtime directory with owner-only perms.

    Idempotent: creates the directory tree when absent and re-applies the
    :data:`RUNTIME_DIR_MODE` owner-only mode on every call so a directory
    created before the hardening landed (or under a permissive umask) is
    tightened in place. Callers that need the directory on disk MUST route
    through this helper rather than a bare ``Path.mkdir`` so the perms
    invariant holds at exactly one creation point.

    Returns:
        The hardened runtime directory path.
    """
    rt_dir = runtime_dir()
    rt_dir.mkdir(parents=True, exist_ok=True)
    harden_runtime_dir(rt_dir)
    trees = runtime_base_dir() / TREES_DIRNAME
    if rt_dir.parent == trees:
        # The base and ``trees`` list every tree's runtime dir, so they get
        # the same owner-only mode.
        harden_runtime_dir(trees)
        harden_runtime_dir(trees.parent)
    logger.debug(f"ensure_runtime_dir path={str(rt_dir)!r} mode={RUNTIME_DIR_MODE:#o}")
    return rt_dir


def socket_path() -> Path:
    """Return the Unix domain socket path on POSIX.

    Returns:
        ``<runtime_dir>/eawfd.sock`` — the listener bind address on
        Linux + macOS + BSD. Windows callers should not invoke this.
    """
    return runtime_dir() / "eawfd.sock"


def pid_path() -> Path:
    """Return the daemon PID file path.

    Returns:
        ``<runtime_dir>/eawfd.pid`` — atomic-written by the daemon on boot.
    """
    return runtime_dir() / "eawfd.pid"


def daemon_singleton_lock_path() -> Path:
    """Return the daemon lifetime singleton lock file path.

    Returns:
        ``<runtime_dir>/eawfd.lock`` — held by the live daemon process
        before it writes a PID file, replays WAL, or binds the socket.
    """
    return runtime_dir() / "eawfd.lock"


def daemon_spawn_lock_path() -> Path:
    """Return the CLI auto-spawn coordination lock file path.

    Returns:
        ``<runtime_dir>/eawfd.spawn.lock`` — held only while a CLI is
        deciding whether it needs to fork a daemon.
    """
    return runtime_dir() / "eawfd.spawn.lock"


def log_path() -> Path:
    """Return the daemon log file path.

    Returns:
        ``<runtime_dir>/eawfd.log`` — rotated daily by W02+ logic; W01
        appends only.
    """
    return runtime_dir() / "eawfd.log"
