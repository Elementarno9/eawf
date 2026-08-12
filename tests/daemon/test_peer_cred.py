"""Per-OS peer-credential dispatcher coverage.

Tests exercise :func:`eawf.runtime.daemon.auth.verify_peer_credential` on every
supported platform via :func:`socket.socketpair` round-trips. Each
per-OS test is gated with ``sys.platform`` so the suite passes
end-to-end on any single host — the CI matrix only runs the helper
for its native OS.

Platform-independent tests verify the typed :class:`PeerCredential`
model and that the dispatch table covers every host-platform branch
the daemon claims to support.
"""

from __future__ import annotations

import os
import socket
import sys
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.runtime.daemon.auth import (
    PeerCredential,
    UnauthorizedError,
    _current_platform,
    verify_peer_credential,
)

# ---------------------------------------------------------------------------
# Platform-independent: model + dispatcher contract
# ---------------------------------------------------------------------------


def test_peer_credential_model_accepts_canonical_fields() -> None:
    """Happy path: model round-trips through ``model_dump``."""
    cred = PeerCredential(uid=1000, gid=1000, pid=4242, platform="linux")
    assert cred.uid == 1000
    assert cred.gid == 1000
    assert cred.pid == 4242
    assert cred.platform == "linux"


def test_peer_credential_model_allows_none_pid() -> None:
    """macOS xucred + older FreeBSD return without a PID."""
    cred = PeerCredential(uid=1000, gid=-1, pid=None, platform="darwin")
    assert cred.pid is None
    assert cred.gid == -1


def test_peer_credential_model_rejects_extra_fields() -> None:
    """``extra="forbid"`` per AGENTS rule 2."""
    with pytest.raises(ValidationError):
        PeerCredential.model_validate(
            {
                "uid": 1000,
                "gid": 1000,
                "pid": 4242,
                "platform": "linux",
                "rogue_field": "boom",
            }
        )


def test_peer_credential_model_rejects_unknown_platform() -> None:
    """Only the four supported platform tags are valid."""
    with pytest.raises(ValidationError):
        PeerCredential.model_validate({"uid": 1000, "gid": 1000, "pid": 4242, "platform": "sunos"})


def test_current_platform_returns_supported_tag() -> None:
    """On every host the daemon claims to support, ``_current_platform``
    returns one of the four canonical tags rather than raising."""
    if (
        not sys.platform.startswith("linux")
        and sys.platform != "darwin"
        and not sys.platform.startswith("freebsd")
        and sys.platform != "win32"
    ):
        with pytest.raises(NotImplementedError):
            _current_platform()
        return
    tag = _current_platform()
    assert tag in ("linux", "darwin", "freebsd", "win32")


def test_dispatch_table_covers_every_supported_platform() -> None:
    """Sanity: the dispatcher must route every supported platform — no
    silent fall-through to ``NotImplementedError`` for a tag the model
    accepts."""
    from eawf.runtime.daemon.auth import _POSIX_DISPATCH

    assert set(_POSIX_DISPATCH) == {"linux", "darwin", "freebsd"}


def test_unauthorized_error_carries_forensics() -> None:
    """Forensic payload is attached for downstream JSON-RPC ``data``."""
    exc = UnauthorizedError(
        "peer uid mismatch: expected 1000, got 1001",
        forensics={"platform": "linux", "expected_uid": 1000, "actual_uid": 1001},
    )
    assert exc.forensics["expected_uid"] == 1000
    assert exc.forensics["actual_uid"] == 1001
    assert exc.forensics["platform"] == "linux"


def test_unauthorized_error_default_forensics_is_empty_dict() -> None:
    """Forensics default to an empty mapping (never ``None``)."""
    exc = UnauthorizedError("generic reject")
    assert exc.forensics == {}


# ---------------------------------------------------------------------------
# Linux: SO_PEERCRED round-trip
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux-only recipe")
def test_verify_peer_credential_linux_happy_path() -> None:
    """``SO_PEERCRED`` returns our own UID + PID over a socketpair."""
    s1, s2 = socket.socketpair()
    try:
        cred = verify_peer_credential(s2, expected_uid=os.geteuid())
        assert isinstance(cred, PeerCredential)
        assert cred.uid == os.geteuid()
        assert cred.gid == os.getegid()
        assert cred.pid == os.getpid()
        assert cred.platform == "linux"
    finally:
        s1.close()
        s2.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux-only recipe")
def test_verify_peer_credential_linux_mismatch_rejects() -> None:
    """Bogus expected UID triggers ``UnauthorizedError`` with forensics."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses peer-cred mismatch")
    s1, s2 = socket.socketpair()
    try:
        with pytest.raises(UnauthorizedError) as excinfo:
            verify_peer_credential(s2, expected_uid=os.geteuid() + 1)
        assert excinfo.value.forensics["platform"] == "linux"
        assert excinfo.value.forensics["expected_uid"] == os.geteuid() + 1
        assert excinfo.value.forensics["actual_uid"] == os.geteuid()
    finally:
        s1.close()
        s2.close()


# ---------------------------------------------------------------------------
# macOS: LOCAL_PEERCRED round-trip
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only recipe")
def test_verify_peer_credential_darwin_happy_path() -> None:
    """``LOCAL_PEERCRED`` returns our own UID; PID is ``None``."""
    s1, s2 = socket.socketpair()
    try:
        cred = verify_peer_credential(s2, expected_uid=os.geteuid())
        assert isinstance(cred, PeerCredential)
        assert cred.uid == os.geteuid()
        # xucred does not populate a usable PID on macOS.
        assert cred.pid is None
        assert cred.platform == "darwin"
    finally:
        s1.close()
        s2.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only recipe")
def test_verify_peer_credential_darwin_mismatch_rejects() -> None:
    """Bogus expected UID triggers ``UnauthorizedError`` with forensics."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses peer-cred mismatch")
    s1, s2 = socket.socketpair()
    try:
        with pytest.raises(UnauthorizedError) as excinfo:
            verify_peer_credential(s2, expected_uid=os.geteuid() + 1)
        assert excinfo.value.forensics["platform"] == "darwin"
        assert excinfo.value.forensics["expected_uid"] == os.geteuid() + 1
        assert excinfo.value.forensics["actual_uid"] == os.geteuid()
    finally:
        s1.close()
        s2.close()


# ---------------------------------------------------------------------------
# FreeBSD: LOCAL_PEERCRED via ctypes round-trip
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("freebsd"), reason="freebsd-only recipe")
def test_verify_peer_credential_freebsd_happy_path() -> None:
    """FreeBSD xucred prefix returns our own UID over a socketpair."""
    s1, s2 = socket.socketpair()
    try:
        cred = verify_peer_credential(s2, expected_uid=os.geteuid())
        assert isinstance(cred, PeerCredential)
        assert cred.uid == os.geteuid()
        assert cred.platform == "freebsd"
    finally:
        s1.close()
        s2.close()


@pytest.mark.skipif(not sys.platform.startswith("freebsd"), reason="freebsd-only recipe")
def test_verify_peer_credential_freebsd_mismatch_rejects() -> None:
    """Bogus expected UID triggers ``UnauthorizedError`` with forensics."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses peer-cred mismatch")
    s1, s2 = socket.socketpair()
    try:
        with pytest.raises(UnauthorizedError) as excinfo:
            verify_peer_credential(s2, expected_uid=os.geteuid() + 1)
        assert excinfo.value.forensics["platform"] == "freebsd"
        assert excinfo.value.forensics["expected_uid"] == os.geteuid() + 1
        assert excinfo.value.forensics["actual_uid"] == os.geteuid()
    finally:
        s1.close()
        s2.close()


@pytest.mark.skipif(not sys.platform.startswith("freebsd"), reason="freebsd-only recipe")
def test_verify_peer_credential_freebsd_unsupported_socket_fails_closed() -> None:
    """A TCP socket has no LOCAL_PEERCRED — must raise ``UnauthorizedError``
    (fail closed), not silently authorise."""
    s1, s2 = socket.socketpair(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(UnauthorizedError) as excinfo:
            verify_peer_credential(s2, expected_uid=os.geteuid())
        assert excinfo.value.forensics["platform"] == "freebsd"
    finally:
        s1.close()
        s2.close()


# ---------------------------------------------------------------------------
# Windows: SID round-trip via windows_security
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="win32-only recipe")
def test_verify_peer_credential_win32_happy_path() -> None:
    """SID verification through the unified dispatcher.

    Round-trips through the same named-pipe scaffolding W02 already
    exercises: a server pipe is created with the user-only DACL, a
    client connects, and the dispatcher returns a credential with
    ``platform="win32"``.
    """
    pytest.importorskip("win32security")
    pytest.importorskip("win32pipe")
    import threading
    import uuid

    import win32file
    import win32pipe

    from eawf.runtime.daemon.windows_security import build_user_only_security_attributes

    pipe_name = rf"\\.\pipe\eawfd-w05-{uuid.uuid4().hex[:8]}"
    sa = build_user_only_security_attributes()
    server_handle = win32pipe.CreateNamedPipe(
        pipe_name,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1,
        65536,
        65536,
        0,
        sa,
    )

    result: dict[str, Any] = {}

    def _connect_client() -> None:
        try:
            client = win32file.CreateFile(
                pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
            result["client"] = client
        except Exception as exc:  # pragma: no cover - thread-local error path
            result["error"] = exc

    thread = threading.Thread(target=_connect_client)
    thread.start()
    try:
        win32pipe.ConnectNamedPipe(server_handle, None)
        cred = verify_peer_credential(server_handle)
        assert cred.platform == "win32"
    finally:
        thread.join(timeout=5.0)
        win32file.CloseHandle(server_handle)
        if "client" in result:
            win32file.CloseHandle(result["client"])
