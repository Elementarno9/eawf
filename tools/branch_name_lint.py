"""Branch-name lint for Milestone and Task branches.

A Milestone branch is ``feature/<symbol>-v<X.Y>`` and a Task worktree branch
is ``feature/<symbol>-v<X.Y>-<task-key>``, the Task key in lower case
(``feature/eawf-v0.7-eawf-0628``). Any other suffix on a branch that looks
like one of them (``-final``, ``-ready``, ``-rc4-fix``) is refused, because
the integration and the release verbs find a Milestone's work by these exact
names. A branch that does not start ``feature/<name>-v<digit>`` is not a
Milestone or Task branch and is left alone. The grammar lives in
:mod:`eawf.runtime.vcs.branch_name`, which also binds a host session to the
Task its branch names.

At the ``pre-push`` stage the lint reads the local and remote branch names
pre-commit exports; at the ``pre-commit`` stage, where Task branches that are
never pushed get their only check, it reads the checked-out branch. A
detached ``HEAD`` names no branch and passes.

Exit codes:
- ``0`` — every named branch is accepted or out of scope.
- ``1`` — a branch is refused (message printed to stderr).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping

from eawf.runtime.vcs.branch_name import branch_name_error

_PUSH_BRANCH_VARS = ("PRE_COMMIT_LOCAL_BRANCH", "PRE_COMMIT_REMOTE_BRANCH")
_HEADS_PREFIX = "refs/heads/"
_GIT_TIMEOUT_SECONDS = 20.0


def _current_branch() -> str | None:
    proc = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def branches_to_lint(env: Mapping[str, str]) -> list[str]:
    """Return the branch names the running hook stage names.

    Args:
        env: The hook environment.

    Returns:
        The pushed local and remote branch names when pre-commit exports
        them, else the checked-out branch; empty on a detached ``HEAD``.
    """
    pushed = [env[name] for name in _PUSH_BRANCH_VARS if env.get(name)]
    if pushed:
        return list(dict.fromkeys(name.removeprefix(_HEADS_PREFIX) for name in pushed))
    current = _current_branch()
    return [] if current is None else [current]


def main() -> int:
    errors = [
        error
        for branch in branches_to_lint(os.environ)
        if (error := branch_name_error(branch)) is not None
    ]
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
