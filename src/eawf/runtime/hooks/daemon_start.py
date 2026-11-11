"""Start the repository's daemon in the background when a hook finds none.

Hooks only attach to a running daemon, and the daemon exits once idle, so
without a start somewhere a managed repository would record nothing after
any pause until some command happened to start one. Session start therefore
launches the daemon entry point, the same one the CLI's cold spawn executes,
fully detached: a new session, stdio on the null device, no wait; any other
hook whose daemon call found none asks for the same launch, at most once per
:data:`DAEMON_START_INTERVAL_S` across processes. A daemon already serving
the tree makes the newcomer log ``run duplicate-daemon`` and exit 0, so the
launch needs no liveness probe first.

The module imports only the standard library and the detach kwargs because
every hook that finds no daemon loads it.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path
from subprocess import DEVNULL, Popen
from typing import Final

from eawf.platform.subprocess_detach import detached_subprocess_kwargs

#: The handler name a failed launch is recorded under.
DAEMON_START_HOOK: Final = "daemon.background_start"

#: The stamp of the last background start, relative to a tree's ``.ea``; it
#: sits beside the hook health record.
DAEMON_START_STAMP: Final = Path("local") / "hooks" / "daemon-start.stamp"

#: How long a stamp holds off the next background start: a daemon that is
#: still booting is not asked for again, and a launch that keeps failing
#: leaves one health row a minute, not one per tool call.
DAEMON_START_INTERVAL_S: Final = 60.0

#: Where a checkout's own environment keeps its interpreter and its eawf
#: launcher, POSIX then Windows; the hook wrapper tests the same launchers.
_VENV_RUNNERS: Final = (
    (Path(".venv") / "bin" / "python", Path(".venv") / "bin" / "eawf"),
    (Path(".venv") / "Scripts" / "python.exe", Path(".venv") / "Scripts" / "eawf.exe"),
)


def daemon_start_argv(main: Path) -> list[str]:
    """Return the command that runs the daemon of the repository whose main checkout is *main*.

    A hook fired in a linked worktree runs that branch's environment, whose
    eawf may be mid-development; the repository's daemon serves every tree,
    so it runs from the main checkout's environment when that environment
    holds eawf, which its executable launcher shows. An environment without
    one is some other project's, or a broken install.

    Args:
        main: The main checkout of the repository.

    Returns:
        The main checkout's interpreter running the daemon module, else the
        current interpreter, as the CLI's cold spawn runs it.

    Raises:
        TypeError: *main* is not a :class:`~pathlib.Path`.
    """
    if not isinstance(main, Path):
        raise TypeError(f"main must be a Path; got {type(main).__name__}")
    python = next(
        (
            main / interpreter
            for interpreter, launcher in _VENV_RUNNERS
            if (main / interpreter).is_file()
            and (main / launcher).is_file()
            and os.access(main / launcher, os.X_OK)
        ),
        None,
    )
    return [str(python) if python else sys.executable, "-m", "eawf.runtime.daemon.main"]


def claim_daemon_start(ea_dir: Path) -> bool:
    """Claim the background start of the tree whose ``.ea`` is *ea_dir*, at most once an interval.

    The claim is the stamp's creation, which only one process can win. A
    stamp older than :data:`DAEMON_START_INTERVAL_S`, or dated in the future
    by a clock set back, is retired by renaming it to a name of this
    process's own, which only one process can do to a given file, and the
    claim is tried once more. A process whose rename took a stamp another
    had just made puts it back and loses.

    Args:
        ea_dir: The tree's ``.ea`` directory.

    Returns:
        Whether this process may launch the daemon now; ``False`` too when
        *ea_dir* is not a directory or the stamp cannot be written, since a
        tree that cannot be stamped cannot be rate-limited either.

    Raises:
        TypeError: *ea_dir* is not a :class:`~pathlib.Path`.
    """
    if not isinstance(ea_dir, Path):
        raise TypeError(f"ea_dir must be a Path; got {type(ea_dir).__name__}")
    if not ea_dir.is_dir():
        return False
    stamp = ea_dir / DAEMON_START_STAMP
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        if _created(stamp):
            return True
        if _fresh(stamp):
            return False
        retired = stamp.with_name(f"{stamp.name}.{os.getpid()}.{time.monotonic_ns()}")
        try:
            os.rename(stamp, retired)
        except FileNotFoundError:
            return False
        if _fresh(retired):
            # Another process renewed the stamp between this one's look and its
            # rename; the renewal stands.
            with contextlib.suppress(FileExistsError):
                os.link(retired, stamp)
            retired.unlink()
            return False
        retired.unlink()
        return _created(stamp)
    except OSError:
        return False


def _created(stamp: Path) -> bool:
    """Create *stamp* exclusively; return whether this call created it."""
    try:
        os.close(os.open(stamp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        return False
    return True


def _fresh(stamp: Path) -> bool:
    """Return whether *stamp* holds off a start now; a vanished stamp is a renewal underway."""
    try:
        age = time.time() - stamp.stat().st_mtime
    except FileNotFoundError:
        return True
    return 0 <= age < DAEMON_START_INTERVAL_S


def request_daemon_start(main: Path) -> None:
    """Launch the daemon of the repository whose main checkout is *main*, without waiting.

    The daemon resolves its tree from ``EA_STATE``, so it serves the same tree
    the hooks pin with ``-w``, and with it the same runtime directory. The
    child is detached with every stream on the null device, so what can go
    wrong is learnt at spawn time or not at all: an interpreter that does not
    exist or cannot be executed makes the launch raise here.

    Args:
        main: The main checkout of a managed repository.

    Raises:
        OSError: The interpreter could not be launched.
    """
    env = dict(os.environ)
    env["EA_STATE"] = str(main / ".ea" / "state.json")
    # Popen without wait: the child outlives this hook in a session of its own,
    # and with no inherited stdout it cannot write into the hook's answer.
    Popen(
        daemon_start_argv(main),
        cwd=main,
        env=env,
        stdin=DEVNULL,
        stdout=DEVNULL,
        stderr=DEVNULL,
        close_fds=True,
        **detached_subprocess_kwargs(),
    )


__all__ = [
    "DAEMON_START_HOOK",
    "DAEMON_START_INTERVAL_S",
    "DAEMON_START_STAMP",
    "claim_daemon_start",
    "daemon_start_argv",
    "request_daemon_start",
]
