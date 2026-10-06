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
import hashlib
import importlib.util
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest


def test_module_raises_importerror_on_non_windows() -> None:
    """``import eawf.runtime.daemon.windows_pipe`` must fail on POSIX."""
    if sys.platform == "win32":
        pytest.skip("guard only fires on non-Windows")
    with pytest.raises(ImportError, match="win32-only"):
        import eawf.runtime.daemon.windows_pipe  # noqa: F401


def test_security_module_raises_importerror_on_non_windows() -> None:
    """``import eawf.runtime.daemon.windows_security`` must fail on POSIX."""
    if sys.platform == "win32":
        pytest.skip("guard only fires on non-Windows")
    with pytest.raises(ImportError, match="win32-only"):
        import eawf.runtime.daemon.windows_security  # noqa: F401


def test_verify_windows_peer_rejects_non_windows() -> None:
    """``verify_windows_peer`` is win32-only — POSIX raises NotImplementedError."""
    if sys.platform == "win32":
        pytest.skip("entry point routes through windows_security on Windows")
    from eawf.runtime.daemon.auth import verify_windows_peer

    with pytest.raises(NotImplementedError, match="win32-only"):
        verify_windows_peer(object(), None)


def test_build_user_only_security_attributes_returns_sec_attrs() -> None:
    """SECURITY_ATTRIBUTES with DACL + owner SID bound."""
    pytest.importorskip("win32security")
    pytest.importorskip("ntsecuritycon")

    from eawf.runtime.daemon.windows_security import build_user_only_security_attributes

    sa = build_user_only_security_attributes()
    # pywin32 ``SECURITY_ATTRIBUTES`` exposes ``SECURITY_DESCRIPTOR`` +
    # ``bInheritHandle`` attributes; both must be wired. ``bInheritHandle`` is a
    # win32 BOOL, which pywin32 hands back as an int -- an identity check against
    # the False singleton can never hold, so the handle-inheritance assertion was
    # failing on the falsiness it meant to assert.
    assert hasattr(sa, "SECURITY_DESCRIPTOR")
    assert sa.bInheritHandle == 0

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

    from eawf.runtime.daemon.windows_pipe import WindowsPipeServer

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


def test_windows_pipe_server_start_twice_rejected() -> None:
    """A double ``start()`` raises RuntimeError before launching threads."""
    pytest.importorskip("win32pipe")

    from eawf.runtime.daemon.windows_pipe import WindowsPipeServer

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
        with pytest.raises(RuntimeError, match=r"already started"):
            server.start()

    asyncio.run(runner())


def _load_windows_pipe_with_fakes(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Load ``windows_pipe`` under fake pywin32 modules on any host."""

    class _PyWinError(Exception):
        def __init__(self, winerror: int, strerror: object) -> None:
            super().__init__(strerror)
            self.winerror = winerror
            self.strerror = strerror

    class _CancelIoEx:
        def __call__(self, *_args: object) -> bool:
            return True

    class _Kernel32:
        CancelIoEx = _CancelIoEx()

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "ctypes.windll",
        types.SimpleNamespace(kernel32=_Kernel32()),
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "pywintypes", types.SimpleNamespace(error=_PyWinError))
    monkeypatch.setitem(
        sys.modules,
        "win32file",
        types.SimpleNamespace(
            GENERIC_READ=1,
            GENERIC_WRITE=2,
            OPEN_EXISTING=3,
            CloseHandle=lambda _handle: None,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32pipe",
        types.SimpleNamespace(
            PIPE_ACCESS_DUPLEX=1,
            PIPE_TYPE_MESSAGE=2,
            PIPE_READMODE_MESSAGE=4,
            PIPE_WAIT=8,
            PIPE_UNLIMITED_INSTANCES=255,
        ),
    )
    monkeypatch.setitem(sys.modules, "winerror", types.SimpleNamespace(ERROR_MORE_DATA=234))

    module_path = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "eawf"
        / "runtime"
        / "daemon"
        / "windows_pipe.py"
    )
    spec = importlib.util.spec_from_file_location("_eawf_test_windows_pipe", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_more_data_exception_strerror_is_not_treated_as_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ERROR_MORE_DATA`` exception text is not response/request bytes."""
    module = _load_windows_pipe_with_fakes(monkeypatch)
    calls = 0

    def _read_file(_pipe: object, _size: int) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise module.pywintypes.error(module.winerror.ERROR_MORE_DATA, b"not-payload")
        return 0, b"actual-payload"

    module.win32file.ReadFile = _read_file

    assert module._read_full_message(object()) == b"actual-payload"


def test_first_chunk_more_data_exception_does_not_emit_strerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-verify read never treats exception strerror as client bytes."""
    module = _load_windows_pipe_with_fakes(monkeypatch)

    def _read_file(_pipe: object, _size: int) -> tuple[int, bytes]:
        raise module.pywintypes.error(module.winerror.ERROR_MORE_DATA, b"not-payload")

    module.win32file.ReadFile = _read_file

    assert module._read_first_chunk(object()) == (b"", True)


def test_pipe_client_call_timeout_cancels_blocked_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled response read is bounded by ``wait_ms`` and cancelled."""
    module = _load_windows_pipe_with_fakes(monkeypatch)
    cancelled = threading.Event()

    class _Handle:
        def __int__(self) -> int:
            return 1234

    handle = _Handle()
    module.win32pipe.WaitNamedPipe = lambda _name, _wait_ms: True
    module.win32file.CreateFile = lambda *_args: handle
    module.win32pipe.SetNamedPipeHandleState = lambda *_args: None
    module.win32file.WriteFile = lambda *_args: None
    module.win32file.CloseHandle = lambda _handle: None

    def _read_file(_pipe: object, _size: int) -> tuple[int, bytes]:
        cancelled.wait(1.0)
        raise module.pywintypes.error(995, "operation aborted")

    module.win32file.ReadFile = _read_file

    def _cancel(_pipe: object) -> bool:
        cancelled.set()
        return True

    module.cancel_pending_read = _cancel

    with pytest.raises(TimeoutError, match="exceeded timeout"):
        module.pipe_client_call(r"\\.\pipe\eawfd-timeout-test", b"{}\n", wait_ms=20)
    assert cancelled.is_set()


def test_listener_delegates_accepted_pipe_to_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accept loop hands connected pipes to workers before serving them."""
    loop = asyncio.new_event_loop()
    module = _load_windows_pipe_with_fakes(monkeypatch)
    spawned: list[object] = []
    pipe = object()

    monkeypatch.setitem(
        sys.modules,
        "eawf.runtime.daemon.windows_security",
        types.SimpleNamespace(build_user_only_security_attributes=lambda: object()),
    )
    module.win32pipe.CreateNamedPipe = lambda *_args: pipe
    module.win32pipe.ConnectNamedPipe = lambda *_args: None
    module.win32file.CloseHandle = lambda _handle: None

    try:

        async def _handler(_payload: bytes) -> bytes:
            return b""

        server = module.WindowsPipeServer(
            loop,
            _handler,
            pipe_name=r"\\.\pipe\eawfd-worker-test",
            verify_sid_enabled=False,
        )

        def _spawn(accepted_pipe: object) -> None:
            spawned.append(accepted_pipe)
            server._shutdown.set()

        server._spawn_connection_worker = _spawn
        server._listen_loop()
    finally:
        loop.close()
    assert spawned == [pipe]


def test_the_win32_subscribe_literal_equals_the_dispatch_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicated literal names exactly the methods the streamer routes.

    The win32 module declares the set as a literal rather than importing it, so nothing
    but this test stops the two drifting; a method missing here falls to the
    request/response path and its stream never reaches a Windows client.
    """
    from eawf.runtime.daemon.methods.state_subscribe import SUBSCRIBE_METHODS

    module = _load_windows_pipe_with_fakes(monkeypatch)

    assert module._SUBSCRIBE_METHODS == SUBSCRIBE_METHODS


def _expected_key(runtime_dir: Path) -> str:
    return hashlib.sha256(str(runtime_dir.resolve()).encode("utf-8")).hexdigest()[:16]


def test_default_pipe_name_is_per_user_and_per_runtime_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The name carries the user and a 16-hex key of the resolved runtime dir."""
    module = _load_windows_pipe_with_fakes(monkeypatch)
    runtime_dir = tmp_path / "trees" / "abc"

    name = module.default_pipe_name(runtime_dir, username="alice")

    assert name == rf"\\.\pipe\eawfd-alice-{_expected_key(runtime_dir)}"


def test_default_pipe_name_reads_the_current_login(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without an override the user part is the current login."""
    module = _load_windows_pipe_with_fakes(monkeypatch)
    monkeypatch.setattr(module.getpass, "getuser", lambda: "bob")

    name = module.default_pipe_name(tmp_path)

    assert name == rf"\\.\pipe\eawfd-bob-{_expected_key(tmp_path)}"


def test_default_pipe_name_separates_two_trees_of_one_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two runtime dirs give two names; the same dir gives the same name."""
    module = _load_windows_pipe_with_fakes(monkeypatch)
    tree_a = tmp_path / "trees" / "a"
    tree_b = tmp_path / "trees" / "b"

    name_a = module.default_pipe_name(tree_a, username="alice")
    name_b = module.default_pipe_name(tree_b, username="alice")

    assert name_a != name_b
    assert module.default_pipe_name(tree_a, username="alice") == name_a


def test_default_pipe_name_resolves_the_runtime_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two spellings of one directory key to one pipe."""
    module = _load_windows_pipe_with_fakes(monkeypatch)
    runtime_dir = tmp_path / "trees" / "a"
    runtime_dir.mkdir(parents=True)
    detour = tmp_path / "trees" / "a" / ".." / "a"

    assert module.default_pipe_name(detour, username="alice") == module.default_pipe_name(
        runtime_dir, username="alice"
    )


@pytest.mark.parametrize("extra", [-1, 0, 1, 500])
def test_default_pipe_name_stays_within_the_win32_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra: int,
) -> None:
    """A user name at, below or past the budget never pushes the name past 256."""
    module = _load_windows_pipe_with_fakes(monkeypatch)
    budget = 256 - len(r"\\.\pipe\eawfd-") - 1 - 16
    username = "u" * (budget + extra)

    name = module.default_pipe_name(tmp_path, username=username)

    assert len(name) == min(256, 256 + extra)
    assert name.endswith(f"-{_expected_key(tmp_path)}")


def test_default_pipe_name_with_empty_user_keeps_the_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An empty user still yields the tree key, so trees stay apart."""
    module = _load_windows_pipe_with_fakes(monkeypatch)

    assert module.default_pipe_name(tmp_path, username="") == (
        rf"\\.\pipe\eawfd--{_expected_key(tmp_path)}"
    )


def test_default_pipe_name_rejects_a_string_runtime_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The key needs a resolvable path; a bare string is a caller error."""
    module = _load_windows_pipe_with_fakes(monkeypatch)

    with pytest.raises(AttributeError):
        module.default_pipe_name(str(tmp_path), username="alice")


def test_windows_pipe_server_defaults_to_the_resolved_runtime_dir_pipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A server built without a pipe name binds the pipe of its runtime dir."""
    module = _load_windows_pipe_with_fakes(monkeypatch)
    monkeypatch.setattr(module.getpass, "getuser", lambda: "alice")
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(tmp_path))
    loop = asyncio.new_event_loop()
    try:

        async def _handler(_payload: bytes) -> bytes:
            return b""

        server = module.WindowsPipeServer(loop, _handler, verify_sid_enabled=False)
    finally:
        loop.close()

    assert server.pipe_path == rf"\\.\pipe\eawfd-alice-{_expected_key(tmp_path)}"
