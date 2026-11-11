"""Start the repository's daemon in the background when a host session starts.

Hooks only attach to a running daemon, so without a start somewhere a new
session in a managed repository would record nothing until some command
happened to start one. Session start therefore launches the daemon entry
point, the same one the CLI's cold spawn executes, fully detached: a new
session, stdio on the null device, no wait. A daemon already serving the
tree makes the newcomer log ``run duplicate-daemon`` and exit 0, so the
launch needs no liveness probe first.

The module imports only the standard library and the detach kwargs because
every session-start hook loads it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from subprocess import DEVNULL, Popen
from typing import Final

from eawf.platform.subprocess_detach import detached_subprocess_kwargs

#: The handler name a failed launch is recorded under.
DAEMON_START_HOOK: Final = "daemon.background_start"


#: Where a checkout's own environment keeps its interpreter, POSIX then Windows.
_VENV_PYTHONS: Final = (Path(".venv") / "bin" / "python", Path(".venv") / "Scripts" / "python.exe")


def daemon_start_argv(main: Path) -> list[str]:
    """Return the command that runs the daemon of the repository whose main checkout is *main*.

    A hook fired in a linked worktree runs that branch's environment, whose
    eawf may be mid-development; the repository's daemon serves every tree,
    so it runs from the main checkout's environment when there is one.

    Args:
        main: The main checkout of the repository.

    Returns:
        The main checkout's interpreter running the daemon module, else the
        current interpreter, as the CLI's cold spawn runs it.
    """
    python = next((main / p for p in _VENV_PYTHONS if (main / p).is_file()), None)
    return [str(python) if python else sys.executable, "-m", "eawf.runtime.daemon.main"]


def request_daemon_start(main: Path) -> None:
    """Launch the daemon of the repository whose main checkout is *main*, without waiting.

    The daemon resolves its tree from ``EA_STATE``, so it serves the same tree
    the hooks pin with ``-w``, and with it the same runtime directory.

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


__all__ = ["DAEMON_START_HOOK", "daemon_start_argv", "request_daemon_start"]
