"""win32: a POSIX shell's drive spelling cannot route a mutation around the guard.

The Claude Code Bash tool on Windows is Git Bash, where ``/c/work/x`` names
``C:\\work\\x``. The guard used to resolve that spelling under the current
drive (``C:\\c\\work\\x``), so ``rm -rf /c/.../repo`` was allowed. Every
command here is judged, never run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from eawf.runtime.sandbox.data_loss import DataLossPattern, judge_tool_call

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="win32 shell drive spellings")

FOREIGN = DataLossPattern.FOREIGN_WORKTREE_MUTATION


@pytest.fixture
def main(tmp_path: Path) -> Path:
    main = tmp_path / "repo"
    (main / ".git" / "worktrees" / "w1").mkdir(parents=True)
    (main / ".ea" / "store").mkdir(parents=True)
    worktree = main / ".ea" / "worktrees" / "w1"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {main.as_posix()}/.git/worktrees/w1\n")
    return main


def _spellings(path: Path) -> list[str]:
    """Return the Git Bash, Cygwin and WSL spellings of the absolute *path*."""
    drive, rest = path.as_posix().split(":", 1)
    return [f"/{drive.lower()}{rest}", f"/cygdrive/{drive.lower()}{rest}", f"/mnt/{drive}{rest}"]


def _judge(main: Path, command: str) -> DataLossPattern | None | str:
    denial = judge_tool_call("Bash", {"command": command}, cwd=main, anchor=main)
    return "allowed" if denial is None else denial.pattern


@pytest.mark.parametrize("form", [0, 1, 2], ids=["msys", "cygdrive", "wsl"])
def test_removing_the_checkout_by_its_shell_drive_spelling_is_denied(main: Path, form: int) -> None:
    assert _judge(main, f"rm -rf {_spellings(main)[form]}") is FOREIGN


@pytest.mark.parametrize("form", [0, 1, 2], ids=["msys", "cygdrive", "wsl"])
def test_removing_a_managed_worktree_by_its_shell_drive_spelling_is_denied(
    main: Path, form: int
) -> None:
    worktree = main / ".ea" / "worktrees" / "w1"
    assert _judge(main, f"rm -rf {_spellings(worktree)[form]}") is FOREIGN


def test_the_drive_spelling_matches_the_native_spelling(main: Path) -> None:
    worktree = main / ".ea" / "worktrees" / "w1"
    native = _judge(main, f"rm -rf '{worktree}'")
    assert native is FOREIGN
    assert _judge(main, f"rm -rf {_spellings(worktree)[0]}") == native


def test_a_single_letter_directory_that_is_not_a_drive_stays_relative(main: Path) -> None:
    assert _judge(main, "rm -rf ./c/tmp") == "allowed"
    assert _judge(main, "rm -rf /cache/tmp") == "allowed"
