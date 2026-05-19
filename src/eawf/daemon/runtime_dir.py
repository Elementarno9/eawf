"""Resolve the daemon runtime directory.

Locates per-user storage for the daemon's PID file, Unix domain socket, log
file, and write-ahead log entries. Pure helpers — no filesystem side effects;
callers materialise the directory when they need it.

Resolution rules (per C02 §5.5 D5):

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


def runtime_dir() -> Path:
    """Return the runtime directory the daemon should use.

    Returns:
        Path to the per-user daemon runtime directory. Caller is
        responsible for ensuring it exists with ``Path.mkdir`` when
        a write is imminent.
    """
    if sys.platform.startswith("linux"):
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            return Path(xdg) / "eawfd"
    return Path.home() / ".eawfd"


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
