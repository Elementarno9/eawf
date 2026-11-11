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
from collections.abc import Mapping
from pathlib import Path
from subprocess import DEVNULL, Popen
from typing import Final

from eawf.platform.subprocess_detach import detached_subprocess_kwargs

#: The handler name a failed launch is recorded under.
DAEMON_START_HOOK: Final = "daemon.background_start"

#: The directory of a tree's ``.ea`` the hook health record and the files
#: below are kept in.
HOOKS_DIR: Final = Path("local") / "hooks"

#: The stamp of the last background start, relative to a tree's ``.ea``.
DAEMON_START_STAMP: Final = HOOKS_DIR / "daemon-start.stamp"

#: Touched when a hook reaches the daemon, so a start the daemon answered is
#: told from one nobody answered.
DAEMON_SEEN_MARKER: Final = HOOKS_DIR / "daemon-seen"

#: Touched when an unanswered start is recorded, so an outage is reported once.
DAEMON_UNANSWERED_MARKER: Final = HOOKS_DIR / "daemon-start.unanswered"

#: The health code of a background start no daemon came of.
DAEMON_START_UNANSWERED_CODE: Final = "daemon_start_unanswered"

#: How long a stamp holds off the next background start: a daemon that is
#: still booting is not asked for again, and a launch that keeps failing
#: leaves one health row a minute, not one per tool call.
DAEMON_START_INTERVAL_S: Final = 60.0

#: How long one report of an unanswered start stands while the daemon stays down.
UNANSWERED_REPEAT_S: Final = 24 * 60 * 60.0

#: The host's variables that describe the one session a hook ran in, which
#: the daemon outlives.
SESSION_ENV_DROPPED: Final = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_ENV_FILE",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_SESSION_ID",
        "CODEX_THREAD_ID",
    }
)

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
    """
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
        tree that cannot be stamped cannot be rate-limited either. Where
        nothing can be written no health row can be either, so the first
        such refusal in a process prints one warning line on stderr and no
        later one does: a tree whose ``.ea/local/hooks`` is read-only gets
        no background start and no record of it.
    """
    if not ea_dir.is_dir():
        return False
    try:
        return _claim(ea_dir / DAEMON_START_STAMP)
    except OSError as exc:
        _warn_unwritable(exc)
        return False


def _warn_unwritable(exc: OSError) -> None:
    """Print the one warning a process gives about a hook directory it cannot write."""
    global _warned_unwritable
    if _warned_unwritable:
        return
    _warned_unwritable = True
    sys.stderr.write(
        f"eawf hook: no background daemon start: {HOOKS_DIR.as_posix()} "
        f"is not writable ({type(exc).__name__})\n"
    )


#: Whether this process has printed :func:`_warn_unwritable`'s line.
_warned_unwritable = False


def _claim(stamp: Path) -> bool:
    """Apply :func:`claim_daemon_start`'s rule to *stamp*; raise what the filesystem raises."""
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


def _mtime(path: Path) -> float | None:
    """Return *path*'s modification time; ``None`` when it is absent."""
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def note_daemon_seen(ea_dir: Path) -> None:
    """Mark that a hook of the tree whose ``.ea`` is *ea_dir* reached its daemon.

    The marker is touched at most once an interval, and whenever it is older
    than the last start's stamp, so a start the daemon answered is always
    followed by a mark. A marker that cannot be written is left alone: the
    worst it costs is one :data:`DAEMON_START_UNANSWERED_CODE` row too many.

    Args:
        ea_dir: The tree's ``.ea`` directory.
    """
    seen = ea_dir / DAEMON_SEEN_MARKER
    marked = _mtime(seen)
    started = _mtime(ea_dir / DAEMON_START_STAMP)
    recent = marked is not None and 0 <= time.time() - marked < DAEMON_START_INTERVAL_S
    if recent and marked is not None and (started is None or marked >= started):
        return
    with contextlib.suppress(OSError):
        seen.parent.mkdir(parents=True, exist_ok=True)
        seen.touch()


def unanswered_start_age(ea_dir: Path) -> float | None:
    """Return the age of the last background start when it went unanswered and is due a report.

    A start went unanswered when its stamp is older than
    :data:`DAEMON_START_INTERVAL_S` and no hook has reached the daemon since
    (:data:`DAEMON_SEEN_MARKER` is older than the stamp); the caller asks
    only after finding no daemon running. A daemon that answered and later
    idled out left a mark after the stamp, so its next start owes nothing.
    One report covers an outage: another is due only after the daemon was
    reached again, or :data:`UNANSWERED_REPEAT_S` after the last report, so
    a daemon that never comes up costs one row a day, not one a minute.

    Args:
        ea_dir: The tree's ``.ea`` directory.

    Returns:
        The stamp's age in seconds, or ``None`` when no report is due.
    """
    started = _mtime(ea_dir / DAEMON_START_STAMP)
    if started is None:
        return None
    now = time.time()
    age = now - started
    if age < DAEMON_START_INTERVAL_S:
        return None
    seen = _mtime(ea_dir / DAEMON_SEEN_MARKER)
    if seen is not None and seen >= started:
        return None
    reported = _mtime(ea_dir / DAEMON_UNANSWERED_MARKER)
    same_outage = reported is not None and (seen is None or reported >= seen)
    if same_outage and reported is not None and 0 <= now - reported < UNANSWERED_REPEAT_S:
        return None
    return age


def note_unanswered_reported(ea_dir: Path) -> None:
    """Mark that the unanswered start :func:`unanswered_start_age` found was recorded.

    Args:
        ea_dir: The tree's ``.ea`` directory.
    """
    marker = ea_dir / DAEMON_UNANSWERED_MARKER
    with contextlib.suppress(OSError):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()


def daemon_start_env(environ: Mapping[str, str], main: Path) -> dict[str, str]:
    """Return the environment the detached daemon of *main* starts with.

    The daemon outlives the host session whose hook started it, so the
    variables that describe that one session are dropped
    (:data:`SESSION_ENV_DROPPED`): left in, ``CLAUDECODE`` would mark every
    later call the daemon makes as made from inside an agent session, and
    ``CLAUDE_PROJECT_DIR`` would pin it to the session's project. ``TMPDIR``
    is kept, since nothing tells a per-session one from the user's own.

    Args:
        environ: The hook's environment.
        main: The main checkout of a managed repository.

    Returns:
        A copy of *environ* without the session's variables, with
        ``EA_STATE`` naming *main*'s state.
    """
    env = {key: value for key, value in environ.items() if key not in SESSION_ENV_DROPPED}
    env["EA_STATE"] = str(main / ".ea" / "state.json")
    return env


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
    env = daemon_start_env(os.environ, main)
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
    "DAEMON_SEEN_MARKER",
    "DAEMON_START_HOOK",
    "DAEMON_START_INTERVAL_S",
    "DAEMON_START_STAMP",
    "DAEMON_START_UNANSWERED_CODE",
    "DAEMON_UNANSWERED_MARKER",
    "HOOKS_DIR",
    "SESSION_ENV_DROPPED",
    "UNANSWERED_REPEAT_S",
    "claim_daemon_start",
    "daemon_start_argv",
    "daemon_start_env",
    "note_daemon_seen",
    "note_unanswered_reported",
    "request_daemon_start",
    "unanswered_start_age",
]
