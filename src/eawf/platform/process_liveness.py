"""Signal-free process liveness and process-group lookup for win32.

The POSIX liveness idiom ``os.kill(pid, 0)`` is destructive on Windows.
``signal.CTRL_C_EVENT`` is ``0`` there, so the call is
``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``: a console Ctrl+C delivered
to every process attached to the caller's console, the caller included. A
CLI that probes a stale daemon PID that way interrupts the operator's own
shell and agent. Any other signal value maps to ``TerminateProcess``.

:func:`pid_alive` answers the same question with ``OpenProcess`` +
``GetExitCodeProcess``, which never signals anything. :func:`process_group_id`
stands in for ``os.getpgid``, which win32 lacks: Windows has no process
groups in the POSIX sense, so a child's own PID is the handle every cancel
path addresses.

Both helpers are win32 implementations. POSIX call sites keep their own
``os.kill`` / ``os.getpgid`` handling, so their exception semantics stay as
they were.

The held-handle registry (:func:`hold_process_handle`,
:func:`release_process_handle`, :func:`held_process_active`) guards the win32
tree kill against PID reuse. Windows never hands a PID to a new process while
any handle to the old one stays open, so a kill gated on a handle the spawner
holds can only ever reach the child it spawned.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def pid_alive(pid: int) -> bool:
    """Return whether *pid* names a running process, without signalling it.

    Args:
        pid: Process id to probe.

    Returns:
        ``True`` while the process runs, including one owned by another user
        (``OpenProcess`` refuses with access denied: it exists, it is just not
        ours to open). ``False`` for a non-positive pid, a pid no process
        holds, or a process that has exited but whose handle is still open.
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes

    handle = _open_process_handle(pid)
    if handle is None:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        return _handle_exit_code(handle) == _STILL_ACTIVE
    finally:
        _close_process_handle(handle)


def _kernel32() -> Any:
    """Return ``kernel32`` with the process-handle prototypes declared.

    Raises:
        NotImplementedError: Off win32; the branch also lets mypy skip the
            win32-only ``ctypes.WinDLL`` when checking on POSIX.
    """
    if sys.platform != "win32":
        raise NotImplementedError("kernel32 exists only on win32")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel32


def _open_process_handle(pid: int) -> int | None:
    """Open a query handle to *pid*; ``None`` when ``OpenProcess`` refuses."""
    handle = _kernel32().OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return int(handle) if handle else None


def _handle_exit_code(handle: int) -> int | None:
    """Return the exit code behind *handle*; ``None`` when the query fails."""
    import ctypes
    from ctypes import wintypes

    code = wintypes.DWORD()
    if not _kernel32().GetExitCodeProcess(handle, ctypes.byref(code)):
        return None
    return int(code.value)


def _close_process_handle(handle: int) -> None:
    """Close a handle opened by :func:`_open_process_handle`."""
    _kernel32().CloseHandle(handle)


#: Process handles held per spawned child pid. Only the spawning process fills
#: it, so a kill issued from any other process finds nothing and refuses.
_HELD_HANDLES: dict[int, int] = {}
#: Serialises registry reads against a concurrent release, so an exit-code
#: query never runs on a handle another thread has just closed.
_HELD_LOCK = threading.Lock()


def hold_process_handle(pid: int) -> None:
    """Hold a handle to the spawned child *pid* so its PID cannot be reused.

    Call it right after the spawn, while the spawner's own ``Popen`` handle
    still pins the PID. A no-op off win32, where process groups give the
    kill its own guard.

    Args:
        pid: Process id of a child this process just spawned.
    """
    if sys.platform != "win32":
        return
    handle = _open_process_handle(pid)
    if handle is None:
        return
    with _HELD_LOCK:
        _HELD_HANDLES[pid] = handle


def release_process_handle(pid: int) -> None:
    """Close the handle held for *pid*, if any.

    Call it once the child is reaped; after it the PID may be reused and
    :func:`held_process_active` refuses it.

    Args:
        pid: Process id passed to :func:`hold_process_handle`.
    """
    with _HELD_LOCK:
        handle = _HELD_HANDLES.pop(pid, None)
        if handle is not None:
            _close_process_handle(handle)


def held_process_active(pid: int) -> bool:
    """Return whether this process holds a handle to *pid* and it still runs.

    Args:
        pid: Process id to check.

    Returns:
        ``True`` only when a handle is held for *pid* and its exit code is
        ``STILL_ACTIVE`` (259). ``False`` when no handle is held, the exit
        code query fails, or the process has exited.
    """
    with _HELD_LOCK:
        handle = _HELD_HANDLES.get(pid)
        if handle is None:
            return False
        return _handle_exit_code(handle) == _STILL_ACTIVE


def process_group_id(pid: int) -> int:
    """Return the process-group id a cancel path should address for *pid*.

    Args:
        pid: Process id of a child spawned as its own group leader.

    Returns:
        ``os.getpgid(pid)`` on POSIX; *pid* itself on win32, which has no
        process groups.

    Raises:
        ProcessLookupError: On POSIX, when *pid* has already exited.
    """
    if sys.platform == "win32":
        return pid
    return os.getpgid(pid)


__all__ = [
    "held_process_active",
    "hold_process_handle",
    "pid_alive",
    "process_group_id",
    "release_process_handle",
]
