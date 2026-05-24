"""On-demand auto-spawn of the eawfd daemon for the CLI.

The CLI auto-starts the daemon when no live process is attached to
the runtime dir. Procedure:

1. Read ``<runtime_dir>/eawfd.pid``; when the file exists, the holder
   PID is alive, and the holder UID matches the calling UID, return
   the PID (a daemon is already running).
2. Otherwise treat as a cold start: POSIX uses the double-fork pattern
   (fork → setsid → fork → exec), Windows uses ``subprocess.Popen``
   with ``DETACHED_PROCESS`` + ``CREATE_NEW_PROCESS_GROUP``. The
   parent CLI then polls the socket/pipe for up to 5 s for liveness.

The cold-spawn is **silent** — no stdout/stderr noise unless the
operator opts in with ``EAWF_VERBOSE=1``. The :mod:`eawf.cli`
verbose-flag plumbing reads the same env var.

The spawn helper is the only piece that imports
:mod:`eawf.runtime.daemon.main` lazily from the CLI side; the import lives
inside the spawn function so importing :mod:`eawf.runtime.daemon.spawn` from
test fixtures does not trigger the full daemon module graph.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


#: How long the CLI waits for the freshly spawned daemon to expose its
#: socket / pipe before bailing out with :class:`DaemonSpawnTimeout`.
SPAWN_POLL_TIMEOUT_SECONDS: float = 5.0

#: Backoff between socket-poll attempts during spawn.
SPAWN_POLL_INTERVAL_SECONDS: float = 0.05


class DaemonSpawnTimeoutError(RuntimeError):
    """Raised when a freshly spawned daemon never opened its socket.

    The CLI wraps this in a :class:`-32010` JSON-RPC error envelope so
    the operator-facing diagnostic surfaces with the standard error
    code rather than a Python traceback. The error message names the
    runtime directory + the elapsed poll time.
    """


# Backwards-compatible alias without the ``Error`` suffix so existing
# call sites that import the shorter name keep resolving while new
# code prefers the PEP 8 spelling.
DaemonSpawnTimeout = DaemonSpawnTimeoutError


def _is_verbose() -> bool:
    """Return True when ``EAWF_VERBOSE=1`` is set in the environment."""
    return os.environ.get("EAWF_VERBOSE", "") == "1"


def _emit(message: str) -> None:
    """Print *message* to stderr when verbose; otherwise stay silent.

    Args:
        message: Plain text. f-string interpolation happens at the
            call site so this stays a simple I/O wrapper.
    """
    if _is_verbose():
        print(message, file=sys.stderr)


def _read_pid_file(pid_file: Path) -> int | None:
    """Return the PID recorded in *pid_file*, or ``None`` on failure.

    Args:
        pid_file: Path to ``<runtime_dir>/eawfd.pid``.

    Returns:
        Integer PID, or ``None`` when the file is missing or
        unparseable. The caller falls through to a cold spawn in
        either case.
    """
    if not pid_file.exists():
        return None
    try:
        head = pid_file.read_text(encoding="utf-8").splitlines()[0]
        return int(head.strip())
    except OSError, ValueError, IndexError:
        return None


def _pid_alive(pid: int) -> bool:
    """Return True when *pid* refers to a live process on this system.

    The POSIX recipe uses ``os.kill(pid, 0)``; ``ProcessLookupError``
    means the process is gone, ``PermissionError`` means it exists
    but belongs to another user (treated as alive — the caller's UID
    check rejects mismatched ownership separately).

    On Windows the same check is implemented via ``os.kill`` which
    Microsoft now wires to ``OpenProcess`` + ``TerminateProcess`` when
    sig == 0 — the import of ``signal.CTRL_BREAK_EVENT`` is NOT
    triggered by a signal value of 0.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_owned_by_current_uid(pid: int) -> bool:
    """Return True when the calling UID owns the process at *pid* (POSIX).

    On Windows there is no st_uid; the named-pipe DACL is the
    ownership gate, so this helper returns True unconditionally.
    The caller checks pipe + PID liveness separately.
    """
    if sys.platform == "win32":
        return True
    try:
        proc_dir = Path("/proc") / str(pid)
        if proc_dir.exists():
            return proc_dir.stat().st_uid == os.geteuid()
    except OSError:
        pass
    # macOS / *BSD have no /proc; fall back to assuming the kill(0)
    # probe — which already returned True for "alive" — implies the
    # daemon is in the operator's session. The peer-cred handshake
    # on connect rejects on actual UID skew.
    return True


def _wait_for_socket(runtime_dir: Path, deadline: float) -> bool:
    """Poll the daemon socket until it accepts a connection or *deadline* passes.

    Args:
        runtime_dir: Daemon runtime directory; the socket path is
            ``<runtime_dir>/eawfd.sock`` on POSIX. Windows callers
            currently fall through to the named-pipe liveness check
            via :func:`_wait_for_pipe`.
        deadline: ``time.monotonic()`` value at which the poll loop
            gives up and returns False.

    Returns:
        True on success, False on deadline expiry.
    """
    sock_path = runtime_dir / "eawfd.sock"
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    probe.connect(str(sock_path))
                    return True
            except OSError:
                pass
        time.sleep(SPAWN_POLL_INTERVAL_SECONDS)
    return False


def _wait_for_pipe(runtime_dir: Path, deadline: float) -> bool:
    """Poll the daemon's named pipe until liveness or *deadline* passes (Windows).

    Args:
        runtime_dir: Daemon runtime directory; used to compute the
            per-user pipe name fallback.
        deadline: ``time.monotonic()`` value at which the poll loop
            gives up and returns False.

    Returns:
        True when the pipe responds; False on deadline expiry. The
        implementation uses :func:`os.path.exists` against the pipe
        path because pywin32 is import-guarded — a basic existence
        check is sufficient for spawn liveness; the JSON-RPC client
        opens the actual pipe afterwards.
    """
    # The per-user pipe name lives at \\.\pipe\eawfd-<username>;
    # `runtime_dir` is unused on Windows but kept symmetric for
    # callers that pass it positionally.
    del runtime_dir  # silence unused-variable lint without renaming the arg
    pipe_path = rf"\\.\pipe\eawfd-{os.environ.get('USERNAME', 'eawf')}"
    while time.monotonic() < deadline:
        if os.path.exists(pipe_path):
            return True
        time.sleep(SPAWN_POLL_INTERVAL_SECONDS)
    return False


def _spawn_posix(runtime_dir: Path) -> None:
    """Fork-fork-exec the daemon and detach via :func:`os.setsid`.

    Args:
        runtime_dir: Runtime directory the spawned daemon should bind
            its socket + PID file under. Passed through the
            ``EAWF_RUNTIME_DIR`` env var so the daemon's
            :func:`eawf.runtime.daemon.runtime_dir.runtime_dir` resolver picks
            it up at module init.

    POSIX double-fork: the parent CLI returns immediately; the
    grandchild becomes the daemon and is reparented to PID 1. The
    intermediate child exits with status 0 so the parent's
    :func:`os.waitpid` does not zombify.
    """
    # First fork — parent waits + returns; child continues.
    pid = os.fork()
    if pid != 0:
        # Reap the immediate child so the kernel does not leak a
        # zombie process table entry.
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
        return
    # Become a session leader so the daemon is fully detached from
    # the controlling terminal.
    os.setsid()
    # Second fork — ensures the daemon is not a session leader and
    # therefore can never re-acquire a controlling tty.
    pid = os.fork()
    if pid != 0:
        # The intermediate child exits silently; the grandchild
        # owns the daemon lifecycle from here.
        os._exit(0)
    # Redirect stdio to /dev/null so log output stays in the
    # configured log file rather than leaking to the spawning
    # terminal.
    devnull_in = os.open(os.devnull, os.O_RDONLY)
    devnull_out = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_in, 0)
    os.dup2(devnull_out, 1)
    os.dup2(devnull_out, 2)
    if devnull_in > 2:
        os.close(devnull_in)
    if devnull_out > 2:
        os.close(devnull_out)
    # Pin the spawned daemon to *runtime_dir* via the
    # ``EAWF_RUNTIME_DIR`` env var so a test (or operator override)
    # binds its socket + PID file under the caller-supplied directory
    # rather than the user-global default.
    os.environ["EAWF_RUNTIME_DIR"] = str(runtime_dir)
    # Replace the current process image with the daemon entry point.
    # Using ``execve`` propagates the override env into the daemon
    # process explicitly rather than relying on ``execv``'s default
    # of inheriting the parent environment (which it does, but the
    # explicit form documents the intent).
    os.execve(sys.executable, [sys.executable, "-m", "eawf.runtime.daemon.main"], os.environ)


def _spawn_windows(runtime_dir: Path) -> None:
    """Spawn the daemon detached via :func:`subprocess.Popen` (Windows).

    Args:
        runtime_dir: Runtime directory the spawned daemon should bind
            its socket + PID file under.

    Uses ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` flags so the
    CLI's CTRL+C does not propagate to the spawned daemon. pywin32 is
    NOT required here — the standard library carries the constants
    on the win32 build of Python.
    """
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    env = dict(os.environ)
    env["EAWF_RUNTIME_DIR"] = str(runtime_dir)
    subprocess.Popen(
        [sys.executable, "-m", "eawf.runtime.daemon.main"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
        env=env,
    )


def auto_spawn_daemon(runtime_dir: Path) -> int:
    """Ensure a daemon is running for *runtime_dir*; return its PID.

    Args:
        runtime_dir: Daemon runtime directory (the same one the
            daemon itself materialises on boot). The PID file path
            is ``<runtime_dir>/eawfd.pid``.

    Returns:
        Integer PID of the live daemon — either the pre-existing one
        or the freshly spawned one.

    Raises:
        DaemonSpawnTimeout: When the daemon failed to expose its
            socket / pipe within :data:`SPAWN_POLL_TIMEOUT_SECONDS`.
    """
    # Resolve against the caller-supplied runtime_dir so tests can
    # point the helper at a per-test temporary directory without
    # mutating the global runtime-dir resolution.
    pid_file = runtime_dir / "eawfd.pid"
    existing = _read_pid_file(pid_file)
    if existing is not None and _pid_alive(existing) and _pid_owned_by_current_uid(existing):
        logger.info(
            f"auto_spawn_daemon already-running pid={existing} runtime={runtime_dir.name!r}"
        )
        return existing
    if existing is not None:
        # Stale PID — file present but the holder is dead or owned
        # by another UID. Clean up so the fresh daemon can write a
        # new pid file without colliding.
        logger.info(f"auto_spawn_daemon stale-pid pid={existing} runtime={runtime_dir.name!r}")
        with contextlib.suppress(OSError):
            pid_file.unlink()
    _emit(f"eawf: spawning eawfd from {runtime_dir.name}")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        _spawn_windows(runtime_dir)
    else:
        _spawn_posix(runtime_dir)
    deadline = time.monotonic() + SPAWN_POLL_TIMEOUT_SECONDS
    if sys.platform == "win32":
        ready = _wait_for_pipe(runtime_dir, deadline)
    else:
        ready = _wait_for_socket(runtime_dir, deadline)
    if not ready:
        raise DaemonSpawnTimeout(
            f"daemon did not open socket within {SPAWN_POLL_TIMEOUT_SECONDS}s "
            f"(runtime={runtime_dir.name!r})"
        )
    spawned = _read_pid_file(pid_file)
    if spawned is None:
        raise DaemonSpawnTimeout(
            f"daemon socket up but pid file missing (runtime={runtime_dir.name!r})"
        )
    logger.info(f"auto_spawn_daemon spawned pid={spawned} runtime={runtime_dir.name!r}")
    return spawned


__all__ = [
    "SPAWN_POLL_TIMEOUT_SECONDS",
    "DaemonSpawnTimeout",
    "DaemonSpawnTimeoutError",
    "auto_spawn_daemon",
]
