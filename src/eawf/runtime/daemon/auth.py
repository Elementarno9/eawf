"""Peer-credential authentication for the daemon listener.

Single dispatcher entry point :func:`verify_peer_credential` returns a
typed :class:`PeerCredential` for any supported host platform. Callers
never branch on :data:`sys.platform` — the dispatcher routes to the
right per-OS helper internally and normalises every reject path into a
:class:`UnauthorizedError` whose ``forensics`` payload carries
``{platform, expected_uid, actual_uid}`` (or ``expected_sid`` /
``actual_sid`` on Windows) for inclusion in the JSON-RPC ``-32000
unauthorized`` envelope.

Per-OS recipes:

- **Linux** — ``SO_PEERCRED`` at ``SOL_SOCKET`` returns ``struct ucred``
  ``(pid, uid, gid)`` packed as three native ``int``s.
- **macOS** — ``LOCAL_PEERCRED`` at ``SOL_LOCAL`` (constant ``0``)
  returns an ``xucred`` whose first 8 bytes hold
  ``(cr_version, cr_uid)`` as two native ``u_int``s. The kernel
  returns a 76-byte buffer overall; the trailing fields
  (``cr_ngroups`` + ``cr_groups[16]``) are not needed for the UID
  check.
- **FreeBSD** — ``LOCAL_PEERCRED`` at ``SOL_LOCAL`` (constant ``0``,
  option ``1``) returns FreeBSD's xucred. The wire layout differs
  from macOS:

  .. code-block:: c

     struct xucred {
         u_int    cr_version;          /* 4 bytes */
         uid_t    cr_uid;              /* 4 bytes */
         short    cr_ngroups;          /* 2 bytes + 2 pad */
         gid_t    cr_groups[XU_NGROUPS];  /* XU_NGROUPS=16 → 64 bytes */
         /* union { void *_cr_unused1; pid_t cr_pid; } on FreeBSD 13+ */
     };

  Mirrored as a :class:`ctypes.Structure` so the prefix layout is
  explicit. Neither ``socket.SOL_LOCAL`` nor
  ``socket.LOCAL_PEERCRED`` is exposed in CPython's stdlib on
  FreeBSD — we use the kernel's documented numeric values
  (``SOL_LOCAL = 0``, ``LOCAL_PEERCRED = 1`` from ``<sys/un.h>``).
- **Windows** — DACL + post-connect SID verification, delegated to
  :mod:`eawf.runtime.daemon.windows_security`. The dispatcher catches
  :class:`~eawf.runtime.daemon.windows_security.WindowsAuthError` and
  re-raises as :class:`UnauthorizedError` so a caller only ever
  catches one exception type regardless of platform.

Each helper takes ``socket.socket`` (POSIX) or an opaque pipe handle
(Windows) and returns the connecting peer's UID/SID via
:class:`PeerCredential`. The PID field is best-effort: Linux supplies
it natively, macOS does not (xucred lacks PID), FreeBSD supplies it
only on 13+ — set to ``None`` when unavailable.
"""

from __future__ import annotations

import ctypes
import logging
import socket
import struct
import sys
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


# FreeBSD + macOS share the numeric SOL_LOCAL value (``0``) per
# ``<sys/un.h>``; Python's stdlib does not expose it on either platform.
_SOL_LOCAL = 0

# macOS xucred wire length per ``<sys/ucred.h>``. Only the first 8 bytes
# (``cr_version`` + ``cr_uid``) are consumed for the UID check.
_DARWIN_XUCRED_BYTES = 76

# FreeBSD ``LOCAL_PEERCRED`` option value per ``<sys/un.h>`` (option 1 at
# the SOL_LOCAL level). On FreeBSD ``XU_NGROUPS`` is 16; the prefix we
# read totals 76 bytes (cr_version 4 + cr_uid 4 + cr_ngroups 2 + 2 pad +
# cr_groups[16] 64). FreeBSD 13+ appends a 8-byte union with cr_pid that
# we leave on the wire — ``getsockopt`` accepts a smaller buffer and
# truncates safely.
_FREEBSD_LOCAL_PEERCRED = 1
_FREEBSD_XU_NGROUPS = 16
_FREEBSD_XUCRED_PREFIX_BYTES = 4 + 4 + 2 + 2 + 4 * _FREEBSD_XU_NGROUPS  # = 76


Platform = Literal["linux", "darwin", "freebsd", "win32"]


class PeerCredential(BaseModel):
    """Typed peer-credential returned by :func:`verify_peer_credential`.

    Attributes:
        uid: Connecting peer's numeric UID. On Windows this echoes the
            local UID of the process owner (the SID check itself
            happens in the Windows helper).
        gid: Connecting peer's primary GID. ``-1`` on Windows where the
            POSIX GID concept does not apply.
        pid: Connecting peer's PID when the host kernel exposes it
            (Linux ``SO_PEERCRED`` always; FreeBSD 13+ optionally);
            ``None`` otherwise (macOS xucred + older FreeBSD).
        platform: Identifier matching :data:`sys.platform` so callers
            can branch on the credential's origin if needed without
            re-reading ``sys.platform``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    uid: int
    gid: int
    pid: int | None
    platform: Platform


class UnauthorizedError(Exception):
    """Peer credential check rejected the connecting client.

    The daemon translates this to JSON-RPC error code ``-32000``
    (``unauthorized``) before closing the connection. The ``forensics``
    attribute is a mapping suitable for the ``data`` field of the
    JSON-RPC error envelope and carries the platform + observed vs.
    expected credentials for log correlation.
    """

    def __init__(self, message: str, *, forensics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.forensics: dict[str, Any] = forensics or {}


# ---------------------------------------------------------------------------
# Per-OS helpers
# ---------------------------------------------------------------------------


def _verify_linux(sock: socket.socket) -> PeerCredential:
    """Resolve the peer credential via Linux ``SO_PEERCRED``.

    Args:
        sock: Connected stream socket on the daemon side.

    Returns:
        Typed credential with ``platform="linux"``.

    Raises:
        OSError: When the kernel rejects ``getsockopt`` (e.g. the socket
            is not a Unix-domain stream).
    """
    fmt = "3i"
    raw = sock.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,  # type: ignore[attr-defined,unused-ignore]
        struct.calcsize(fmt),
    )
    pid, uid, gid = struct.unpack(fmt, raw)
    return PeerCredential(uid=int(uid), gid=int(gid), pid=int(pid), platform="linux")


def _verify_darwin(sock: socket.socket) -> PeerCredential:
    """Resolve the peer credential via macOS ``LOCAL_PEERCRED``.

    The macOS xucred wire layout is ``(cr_version, cr_uid,
    cr_ngroups, cr_groups[NGROUPS])`` — we read the first 8 bytes for
    UID. The kernel does not populate a PID on xucred, so the returned
    credential carries ``pid=None``.

    Args:
        sock: Connected stream socket on the daemon side.

    Returns:
        Typed credential with ``platform="darwin"`` and ``pid=None``.

    Raises:
        OSError: When the kernel rejects ``getsockopt``.
    """
    raw = sock.getsockopt(_SOL_LOCAL, socket.LOCAL_PEERCRED, _DARWIN_XUCRED_BYTES)  # type: ignore[attr-defined,unused-ignore]
    _version, uid = struct.unpack("II", raw[:8])
    # macOS xucred lacks a usable GID for the connecting peer (the
    # cr_groups array is the *daemon's* groups from the bind side, not
    # the client's). Surface gid=-1 to keep the typed model honest.
    return PeerCredential(uid=int(uid), gid=-1, pid=None, platform="darwin")


class _FreeBSDXucredPrefix(ctypes.Structure):
    """Prefix of FreeBSD's ``struct xucred`` up to ``cr_groups[16]``.

    Mirrors ``<sys/ucred.h>`` byte-for-byte so the UID + primary GID
    can be lifted out without a second ``struct.unpack`` step. Padding
    after ``cr_ngroups`` is implicit under the default
    ``ctypes.Structure`` alignment rules — the layout matches what
    FreeBSD's kernel writes.
    """

    _fields_ = [
        ("cr_version", ctypes.c_uint),
        ("cr_uid", ctypes.c_uint),
        ("cr_ngroups", ctypes.c_short),
        ("cr_groups", ctypes.c_uint * _FREEBSD_XU_NGROUPS),
    ]


def _verify_freebsd(sock: socket.socket) -> PeerCredential:
    """Resolve the peer credential via FreeBSD ``LOCAL_PEERCRED``.

    FreeBSD ships its own xucred — same name as macOS, different
    layout. Neither ``socket.SOL_LOCAL`` nor ``socket.LOCAL_PEERCRED``
    is exposed in CPython's stdlib on FreeBSD, so we pass the kernel's
    documented numeric values directly. The returned buffer is reshaped
    into :class:`_FreeBSDXucredPrefix` so the field accesses are
    self-documenting.

    Args:
        sock: Connected stream socket on the daemon side.

    Returns:
        Typed credential with ``platform="freebsd"``. ``pid`` is
        ``None`` (the cr_pid union member at the struct tail is
        FreeBSD-13+ optional and we do not read past the prefix).

    Raises:
        UnauthorizedError: When the ctypes path fails (e.g. a
            stripped-down jail without LOCAL_PEERCRED support). Fail
            closed rather than authorise on a kernel error.
        OSError: When the kernel rejects ``getsockopt`` with an
            unexpected errno.
    """
    try:
        raw = sock.getsockopt(_SOL_LOCAL, _FREEBSD_LOCAL_PEERCRED, _FREEBSD_XUCRED_PREFIX_BYTES)
    except OSError as exc:
        logger.warning(f"_verify_freebsd getsockopt failed errno={exc.errno!r}")
        raise UnauthorizedError(
            "freebsd peer-cred unavailable",
            forensics={"platform": "freebsd", "errno": exc.errno},
        ) from exc

    if len(raw) < _FREEBSD_XUCRED_PREFIX_BYTES:
        # Kernel returned a short buffer — treat as unauthorised; we
        # cannot trust the contents.
        raise UnauthorizedError(
            "freebsd peer-cred truncated",
            forensics={"platform": "freebsd", "wire_bytes": len(raw)},
        )

    cred = _FreeBSDXucredPrefix.from_buffer_copy(raw[:_FREEBSD_XUCRED_PREFIX_BYTES])
    primary_gid = int(cred.cr_groups[0]) if cred.cr_ngroups > 0 else -1
    return PeerCredential(
        uid=int(cred.cr_uid),
        gid=primary_gid,
        pid=None,
        platform="freebsd",
    )


def _verify_win32(pipe_handle: Any, expected_sid: Any | None) -> PeerCredential:
    """Verify a Windows pipe peer's SID via :mod:`eawf.runtime.daemon.windows_security`.

    The Windows helper compares the connecting peer's SID with
    *expected_sid* (defaulting to the daemon process owner's SID).
    On match we still synthesise a :class:`PeerCredential` so the
    dispatcher's return type stays uniform — ``uid`` echoes the local
    process UID (Windows treats SID and UID as orthogonal; the
    credential row is the bridge).

    Args:
        pipe_handle: Connected pipe handle from
            ``win32pipe.CreateNamedPipe`` after ``ConnectNamedPipe``
            returns.
        expected_sid: Required peer SID; ``None`` resolves to the
            running process owner's SID inside the helper.

    Returns:
        Typed credential with ``platform="win32"`` and ``gid=-1``
        (Windows has no POSIX-style primary group).

    Raises:
        UnauthorizedError: When the peer SID does not match
            *expected_sid*; the helper's
            :class:`~eawf.runtime.daemon.windows_security.WindowsAuthError`
            is re-raised under this type.
        NotImplementedError: When called on a non-Windows host.
    """
    if sys.platform != "win32":
        raise NotImplementedError("_verify_win32 is win32-only")

    from eawf.runtime.daemon.windows_security import WindowsAuthError, verify_peer_sid

    try:
        verify_peer_sid(pipe_handle, expected_sid)
    except WindowsAuthError as exc:
        raise UnauthorizedError(
            str(exc),
            forensics={"platform": "win32", "reason": "sid_mismatch"},
        ) from exc

    # Win32 process UID equivalence: there is no real "uid" — surface
    # ``-1`` so model invariants hold. Tests that need a real UID call
    # the POSIX paths directly.
    return PeerCredential(uid=-1, gid=-1, pid=None, platform="win32")


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


_POSIX_DISPATCH: dict[str, Callable[[socket.socket], PeerCredential]] = {
    "linux": _verify_linux,
    "darwin": _verify_darwin,
    "freebsd": _verify_freebsd,
}


def _current_platform() -> Platform:
    """Return the canonical platform tag for the running interpreter.

    Returns:
        One of ``"linux"``, ``"darwin"``, ``"freebsd"``, ``"win32"``.

    Raises:
        NotImplementedError: When the host platform is not in the
            supported set (peer-cred has no recipe so the dispatcher
            fails closed rather than silently accept the connection).
    """
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("freebsd"):
        return "freebsd"
    if sys.platform == "win32":
        return "win32"
    raise NotImplementedError(f"peer-credential check unsupported on {sys.platform!r}")


def verify_peer_credential(
    transport: Any,
    *,
    expected_uid: int | None = None,
    expected_sid: Any | None = None,
) -> PeerCredential:
    """Verify the peer credential on a daemon-accepted connection.

    Single entry point used by the JSON-RPC server. Routes to the
    per-OS helper based on :data:`sys.platform` and returns a typed
    :class:`PeerCredential`. When *expected_uid* (POSIX) or
    *expected_sid* (Windows) is supplied, mismatch raises
    :class:`UnauthorizedError` with a forensic payload suitable for
    the JSON-RPC ``-32000`` error envelope's ``data`` field.

    Args:
        transport: Connected ``socket.socket`` on POSIX, or pipe
            handle on Windows.
        expected_uid: Required peer UID on POSIX. ``None`` skips the
            comparison (e.g. tests that only want to read the
            credential).
        expected_sid: Required peer SID on Windows. ``None`` defers
            to the Windows helper which falls back to the running
            process owner's SID.

    Returns:
        Typed peer credential.

    Raises:
        UnauthorizedError: Peer UID/SID mismatch, or the per-OS
            helper failed in a way that could not be trusted as
            "authorised".
        NotImplementedError: Running on a platform without a recipe.
    """
    platform = _current_platform()

    if platform == "win32":
        cred = _verify_win32(transport, expected_sid)
        # SID check itself is the gate; nothing more to do.
        return cred

    helper = _POSIX_DISPATCH[platform]
    cred = helper(transport)

    if expected_uid is not None and cred.uid != expected_uid:
        forensics = {
            "platform": platform,
            "expected_uid": expected_uid,
            "actual_uid": cred.uid,
        }
        logger.warning(
            f"verify_peer_credential reject platform={platform} "
            f"expected={expected_uid} actual={cred.uid}"
        )
        raise UnauthorizedError(
            f"peer uid mismatch: expected {expected_uid}, got {cred.uid}",
            forensics=forensics,
        )

    return cred


# ---------------------------------------------------------------------------
# Backwards-compatible shims (kept for W01 callers; tests in
# ``tests/daemon/test_scaffolding.py`` import them directly).
# ---------------------------------------------------------------------------


def check_peer_uid(sock: socket.socket, expected_uid: int) -> None:
    """Reject a connection whose peer UID differs from *expected_uid*.

    Thin shim over :func:`verify_peer_credential` retained so the
    W01 call sites keep working without surgery. New code should call
    :func:`verify_peer_credential` directly and consume the typed
    :class:`PeerCredential` result.

    Args:
        sock: Accepted stream socket on the daemon side.
        expected_uid: The daemon's own UID; only the same user may
            speak to the listener.

    Raises:
        UnauthorizedError: When the peer UID does not match.
        NotImplementedError: When the host platform has no recipe.
    """
    verify_peer_credential(sock, expected_uid=expected_uid)


def verify_windows_peer(pipe_handle: Any, expected_sid: Any | None = None) -> None:
    """Reject a Windows pipe client whose SID is not *expected_sid*.

    Thin shim over :func:`verify_peer_credential` retained for
    callers that pre-date the unified dispatcher.

    Args:
        pipe_handle: Connected pipe handle from pywin32.
        expected_sid: Required peer SID. ``None`` defers to the
            Windows helper's default (running process owner SID).

    Raises:
        UnauthorizedError: When the peer SID does not match.
        NotImplementedError: When invoked on a non-Windows host.
    """
    if sys.platform != "win32":
        raise NotImplementedError("verify_windows_peer is win32-only; POSIX uses check_peer_uid")
    verify_peer_credential(pipe_handle, expected_sid=expected_sid)
