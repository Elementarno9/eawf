"""Peer-credential authentication for the daemon listener.

Minimum POSIX recipes — only the daemon-owning UID may connect:

- Linux: ``SO_PEERCRED`` socket option returns ``(pid, uid, gid)``.
- macOS: ``LOCAL_PEERCRED`` at ``SOL_LOCAL`` returns an ``xucred``
  whose first 8 bytes hold ``(cr_version, cr_uid)``.

FreeBSD ``LOCAL_PEERCRED`` via ctypes lands in W05; the Windows DACL +
SID flow is wired through :func:`verify_windows_peer` which defers to
:mod:`eawf.daemon.windows_security`. Unsupported platforms raise
:class:`NotImplementedError` so the caller fails closed.
"""

from __future__ import annotations

import logging
import socket
import struct
import sys
from typing import Any

logger = logging.getLogger(__name__)


# macOS does not expose ``socket.SOL_LOCAL`` on every Python build; the
# numeric value is ``0`` per ``<sys/un.h>``. The ``xucred`` struct begins
# with ``u_int cr_version`` + ``uid_t cr_uid`` — both 4 bytes — and the
# kernel returns a 76-byte buffer overall.
_SOL_LOCAL_DARWIN = 0
_XUCRED_BYTES = 76


class UnauthorizedError(Exception):
    """Peer credential check rejected the connecting client.

    The daemon translates this to JSON-RPC error code ``-32000``
    (``unauthorized``) before closing the connection.
    """


def _peer_uid_linux(sock: socket.socket) -> int:
    """Resolve the peer UID via ``SO_PEERCRED``.

    Args:
        sock: Connected stream socket on the daemon side.

    Returns:
        UID of the connecting client.

    Raises:
        OSError: When the kernel rejects ``getsockopt``.
    """
    # ``socket.SO_PEERCRED`` is Linux-only — the typeshed stub omits it
    # on darwin / win32 builds, so we silence the attr-defined warning
    # at this single call site rather than gate the entire module behind
    # ``sys.platform`` imports.
    fmt = "3i"
    raw = sock.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,  # type: ignore[attr-defined]
        struct.calcsize(fmt),
    )
    _pid, uid, _gid = struct.unpack(fmt, raw)
    return int(uid)


def _peer_uid_darwin(sock: socket.socket) -> int:
    """Resolve the peer UID via ``LOCAL_PEERCRED``.

    Args:
        sock: Connected stream socket on the daemon side.

    Returns:
        UID of the connecting client.

    Raises:
        OSError: When the kernel rejects ``getsockopt`` (e.g. the
            socket is not a Unix-domain stream).
    """
    raw = sock.getsockopt(_SOL_LOCAL_DARWIN, socket.LOCAL_PEERCRED, _XUCRED_BYTES)
    _version, uid = struct.unpack("II", raw[:8])
    return int(uid)


def peer_uid(sock: socket.socket) -> int:
    """Return the connecting peer's UID.

    Args:
        sock: Accepted stream socket on the daemon side.

    Returns:
        UID of the connecting client.

    Raises:
        NotImplementedError: When the host platform has no W01 recipe;
            W05 expands FreeBSD + Windows coverage.
    """
    if sys.platform.startswith("linux"):
        return _peer_uid_linux(sock)
    if sys.platform == "darwin":
        return _peer_uid_darwin(sock)
    raise NotImplementedError(f"peer-credential check unsupported on {sys.platform!r}")


def check_peer_uid(sock: socket.socket, expected_uid: int) -> None:
    """Reject a connection whose peer UID differs from *expected_uid*.

    Args:
        sock: Accepted stream socket on the daemon side.
        expected_uid: The daemon's own UID; only the same user may
            speak to the listener.

    Raises:
        UnauthorizedError: When the peer UID does not match.
        NotImplementedError: When the host platform has no W01 recipe.
    """
    actual = peer_uid(sock)
    if actual != expected_uid:
        logger.warning(f"check_peer_uid reject expected={expected_uid} actual={actual}")
        raise UnauthorizedError(f"peer uid mismatch: expected {expected_uid}, got {actual}")


def verify_windows_peer(pipe_handle: Any, expected_sid: Any | None = None) -> None:
    """Reject a Windows pipe client whose SID is not *expected_sid*.

    Thin entry point that delegates to
    :func:`eawf.daemon.windows_security.verify_peer_sid` after the
    import-guard on the underlying module has resolved. The
    indirection keeps ``eawf.daemon.auth`` importable on POSIX (the
    helper itself is only invoked on Windows).

    Args:
        pipe_handle: Connected pipe handle from pywin32 after
            ``ConnectNamedPipe`` returns.
        expected_sid: Required peer SID. ``None`` resolves to the
            running process owner's SID inside the helper.

    Raises:
        UnauthorizedError: When the peer SID does not match.
        NotImplementedError: When invoked on a non-Windows host.
    """
    if sys.platform != "win32":
        raise NotImplementedError("verify_windows_peer is win32-only; POSIX uses check_peer_uid")
    from eawf.daemon.windows_security import WindowsAuthError, verify_peer_sid

    try:
        verify_peer_sid(pipe_handle, expected_sid)
    except WindowsAuthError as exc:
        raise UnauthorizedError(str(exc)) from exc
