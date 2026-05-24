"""Resolve the daemon runtime directory.

Locates per-user storage for the daemon's PID file, Unix domain socket, log
file, and write-ahead log entries. Pure helpers — no filesystem side effects;
callers materialise the directory when they need it.

Resolution rules:

- Linux: ``$XDG_RUNTIME_DIR/eawfd/`` when ``XDG_RUNTIME_DIR`` is set;
  otherwise ``~/.eawfd/``.
- macOS / generic POSIX: ``~/.eawfd/``.
- Windows: ``~/.eawfd/`` for log + PID + WAL storage; the listener itself
  is a named pipe at ``\\\\.\\pipe\\eawfd-<user>`` (wired by W02).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Owner-only perms for the runtime directory. The directory holds the PID
# file, Unix socket, daemon log, and WAL — all of which can leak the
# operator's cwd / state paths — so other local users must not traverse
# or list it. POSIX-only; chmod semantics differ on Windows where the
# named-pipe transport gates access via DACL/SID instead.
RUNTIME_DIR_MODE: int = 0o700


def runtime_dir() -> Path:
    """Return the runtime directory the daemon should use.

    Resolution order:

    1. ``EAWF_RUNTIME_DIR`` env var — explicit operator/test override.
       The value is used verbatim; the caller is responsible for
       choosing a path short enough for AF_UNIX (104-byte cap on
       macOS) and for ensuring write access.
    2. ``XDG_RUNTIME_DIR/eawfd`` on Linux when ``XDG_RUNTIME_DIR`` is
       set.
    3. ``~/.eawfd/`` everywhere else (macOS / generic POSIX /
       Windows).

    Returns:
        Path to the per-user daemon runtime directory. Caller is
        responsible for ensuring it exists with ``Path.mkdir`` when
        a write is imminent.
    """
    override = os.environ.get("EAWF_RUNTIME_DIR")
    if override:
        return Path(override)
    if sys.platform.startswith("linux"):
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            return Path(xdg) / "eawfd"
    return Path.home() / ".eawfd"


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


def log_path() -> Path:
    """Return the daemon log file path.

    Returns:
        ``<runtime_dir>/eawfd.log`` — rotated daily by W02+ logic; W01
        appends only.
    """
    return runtime_dir() / "eawfd.log"
