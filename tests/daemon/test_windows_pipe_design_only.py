"""Platform-independent unit tests for the Windows transport surface.

The Windows pipe + security modules are import-guarded on non-Windows
(``raise ImportError`` at module top) so the POSIX dev loop is not
contaminated by pywin32 attribute access. These tests therefore
``pytest.importorskip`` the underlying pywin32 packages so they run
only where the optional extras are installed — on macOS / Linux the
suite simply reports SKIPPED for each case.

What we cover here:

- :func:`build_user_only_security_attributes` returns an object
  shaped like ``SECURITY_ATTRIBUTES`` with the DACL + owner SID bound.
- :class:`WindowsPipeServer` constructor wires the asyncio queue +
  threading event + handler reference WITHOUT touching the pipe (so
  the test does not require pipe-create privileges nor a live SCM).

The live round-trip + DACL rejection cases live in the sibling
``test_windows_pipe.py``; they require an actual pipe on Windows.
"""

from __future__ import annotations

import asyncio
import sys
import threading

import pytest


def test_module_raises_importerror_on_non_windows() -> None:
    """``import eawf.daemon.windows_pipe`` must fail on POSIX."""
    if sys.platform == "win32":
        pytest.skip("guard only fires on non-Windows")
    with pytest.raises(ImportError, match="win32-only"):
        import eawf.daemon.windows_pipe  # noqa: F401


def test_security_module_raises_importerror_on_non_windows() -> None:
    """``import eawf.daemon.windows_security`` must fail on POSIX."""
    if sys.platform == "win32":
        pytest.skip("guard only fires on non-Windows")
    with pytest.raises(ImportError, match="win32-only"):
        import eawf.daemon.windows_security  # noqa: F401


def test_verify_windows_peer_rejects_non_windows() -> None:
    """``verify_windows_peer`` is win32-only — POSIX raises NotImplementedError."""
    if sys.platform == "win32":
        pytest.skip("entry point routes through windows_security on Windows")
    from eawf.daemon.auth import verify_windows_peer

    with pytest.raises(NotImplementedError, match="win32-only"):
        verify_windows_peer(object(), None)


def test_build_user_only_security_attributes_returns_sec_attrs() -> None:
    """SECURITY_ATTRIBUTES with DACL + owner SID bound."""
    pytest.importorskip("win32security")
    pytest.importorskip("ntsecuritycon")

    from eawf.daemon.windows_security import build_user_only_security_attributes

    sa = build_user_only_security_attributes()
    # pywin32 ``SECURITY_ATTRIBUTES`` exposes ``SECURITY_DESCRIPTOR`` +
    # ``bInheritHandle`` attributes; both must be wired.
    assert hasattr(sa, "SECURITY_DESCRIPTOR")
    assert sa.bInheritHandle is False

    sd = sa.SECURITY_DESCRIPTOR
    # Owner SID bound + DACL present (defaulted=False).
    owner_sid, _defaulted = sd.GetSecurityDescriptorOwner(), None
    assert owner_sid is not None
    dacl = sd.GetSecurityDescriptorDacl()
    assert dacl is not None
    # The DACL contains exactly one allow-ACE (the user-only grant).
    assert dacl.GetAceCount() == 1


def test_windows_pipe_server_constructor_wires_queue_and_thread() -> None:
    """``WindowsPipeServer.__init__`` wires queue + shutdown event without binding the pipe."""
    pytest.importorskip("win32pipe")
    pytest.importorskip("win32file")

    from eawf.daemon.windows_pipe import WindowsPipeServer

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        async def _handler(payload: bytes) -> bytes:
            return b'{"ok":true}\n'

        server = WindowsPipeServer(
            loop,
            _handler,
            pipe_name=r"\\.\pipe\eawfd-unit-test",
            verify_sid_enabled=False,
        )
        assert server.pipe_path == r"\\.\pipe\eawfd-unit-test"
        assert server.is_listening() is False
        # Queue must be an asyncio.Queue bound to this loop.
        assert isinstance(server._queue, asyncio.Queue)  # type: ignore[attr-defined]
        # Shutdown event is a threading.Event (cross-thread signalling).
        assert isinstance(server._shutdown, threading.Event)  # type: ignore[attr-defined]
        # No listener thread created yet.
        assert server._listener_thread is None  # type: ignore[attr-defined]
        assert server._dispatch_task is None  # type: ignore[attr-defined]

    asyncio.run(runner())


def test_windows_pipe_server_default_pipe_name_uses_username() -> None:
    """Default pipe name includes the resolved username."""
    pytest.importorskip("win32pipe")

    import getpass

    from eawf.daemon.windows_pipe import default_pipe_name

    name = default_pipe_name()
    assert name.startswith(r"\\.\pipe\eawfd-")
    assert getpass.getuser() in name


def test_windows_pipe_server_start_twice_rejected() -> None:
    """A double ``start()`` raises RuntimeError before launching threads."""
    pytest.importorskip("win32pipe")

    from eawf.daemon.windows_pipe import WindowsPipeServer

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        async def _handler(payload: bytes) -> bytes:
            return b""

        # Use a pipe name that will not be opened (we never call start
        # in this test; the second start would create a thread we'd
        # have to clean up, so we exercise the guard via direct attr).
        server = WindowsPipeServer(
            loop,
            _handler,
            pipe_name=r"\\.\pipe\eawfd-double-start-test",
            verify_sid_enabled=False,
        )
        # Simulate an already-started state.
        server._listener_thread = threading.Thread(  # type: ignore[attr-defined]
            target=lambda: None,
            daemon=True,
        )
        with pytest.raises(RuntimeError, match=r"start\(\) called twice"):
            server.start()

    asyncio.run(runner())
