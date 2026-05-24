"""Windows security helpers for the daemon named-pipe listener.

Builds a :class:`SECURITY_ATTRIBUTES` whose DACL grants full control
to the owning user SID and denies everyone else, and verifies a
connecting peer's SID after the pipe accepts the client. The
``win32security``/``win32api``/``ntsecuritycon`` packages are imported
at the top of this module under a ``sys.platform == "win32"`` guard so
mypy correctly narrows the win32-only attributes away on POSIX dev
hosts. ``from eawf.runtime.daemon import windows_security`` raises
:class:`ImportError` on non-Windows so callers fail fast (they must
gate the import behind ``sys.platform``).

Defence in depth: the pipe DACL is the primary gate (kernel refuses
non-owner ``CreateFile``), and the post-connect SID check is the
second gate that catches DACL-bypass scenarios such as an
attacker-controlled second pipe instance racing the daemon.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

if sys.platform != "win32":
    raise ImportError("eawf.runtime.daemon.windows_security is win32-only")

if sys.platform == "win32":  # pragma: no cover - win32-only branch
    import ntsecuritycon
    import win32api
    import win32con
    import win32pipe
    import win32security

logger = logging.getLogger(__name__)


class WindowsAuthError(Exception):
    """Peer SID check rejected the connecting client.

    The daemon translates this to JSON-RPC error code ``-32000``
    (``unauthorized``) before closing the pipe.
    """


def _current_user_sid() -> Any:
    """Return the SID of the running process's primary token.

    Returns:
        ``PySID`` for the daemon-spawning user.

    Raises:
        OSError: When the Windows API rejects the token query.
    """
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY,
    )
    try:
        user_sid, _attrs = win32security.GetTokenInformation(
            token,
            win32security.TokenUser,
        )
        return user_sid
    finally:
        win32api.CloseHandle(token)


def build_user_only_security_attributes() -> Any:
    """Build a ``SECURITY_ATTRIBUTES`` granting only the owning user SID.

    The DACL contains one allow-ACE (full control for the running
    user) — no allow-ACE for ``Everyone`` or ``Authenticated Users``.
    The empty-but-present DACL on the security descriptor means
    "explicit deny by absence" per the Windows access-check rules.

    Returns:
        ``SECURITY_ATTRIBUTES`` suitable for passing to
        :func:`win32pipe.CreateNamedPipe` as the ``sa`` argument.

    Raises:
        OSError: When the Windows API rejects the SID query or DACL
            construction.
    """
    user_sid = _current_user_sid()

    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        ntsecuritycon.FILE_ALL_ACCESS,
        user_sid,
    )

    sd = win32security.SECURITY_DESCRIPTOR()
    sd.SetSecurityDescriptorOwner(user_sid, False)
    sd.SetSecurityDescriptorDacl(True, dacl, False)

    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = False
    logger.debug("build_user_only_security_attributes sid-bound")
    return sa


def verify_peer_sid(pipe_handle: Any, expected_sid: Any | None = None) -> None:
    """Reject a pipe connection whose client SID is not the daemon owner.

    Reads the named-pipe client token via ``ImpersonateNamedPipeClient``
    + ``OpenThreadToken`` and compares the resulting SID to
    *expected_sid* (default: the running user's SID). The impersonation
    token is reverted before returning so the daemon thread keeps its
    own security context.

    Args:
        pipe_handle: Connected pipe handle from
            :func:`win32pipe.CreateNamedPipe` after
            :func:`win32pipe.ConnectNamedPipe` returns.
        expected_sid: Required peer SID. Defaults to the running
            process owner's SID when ``None``.

    Raises:
        WindowsAuthError: When the peer SID does not match
            *expected_sid*.
        OSError: When the impersonation / token-query Windows API
            fails for a reason other than SID mismatch.
    """
    if expected_sid is None:
        expected_sid = _current_user_sid()

    win32pipe.ImpersonateNamedPipeClient(pipe_handle)
    try:
        thread_token = win32security.OpenThreadToken(
            win32api.GetCurrentThread(),
            win32con.TOKEN_QUERY,
            True,
        )
        try:
            peer_sid, _attrs = win32security.GetTokenInformation(
                thread_token,
                win32security.TokenUser,
            )
        finally:
            win32api.CloseHandle(thread_token)
    finally:
        win32security.RevertToSelf()

    if not win32security.EqualSid(peer_sid, expected_sid):
        peer_repr = win32security.ConvertSidToStringSid(peer_sid)
        expected_repr = win32security.ConvertSidToStringSid(expected_sid)
        logger.warning(f"verify_peer_sid reject expected={expected_repr!r} actual={peer_repr!r}")
        raise WindowsAuthError(f"peer sid mismatch: expected {expected_repr}, got {peer_repr}")
