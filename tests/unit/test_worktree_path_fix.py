"""Unit tests for the worktree ``path-fix`` cross-platform absoluteness helper.

The legacy state.json files written on POSIX hosts store paths like
``/Users/foo/proj/.claude/worktrees/...``. When such a file is opened on
Windows, ``pathlib.Path`` parses the string as a *relative* ``WindowsPath``
(no drive letter), so a Windows-native ``is_absolute()`` check would
silently skip the legacy entry and leave it as-is. The helper
:func:`_is_path_absolute_any_platform` covers both dialects so the sweep
behaves the same on every host.
"""

from __future__ import annotations

import pytest

from eawf.cli.commands.worktree import _is_path_absolute_any_platform


@pytest.mark.parametrize(
    "path_str",
    [
        "/Users/foo/proj/.claude/worktrees/W01",
        "/var/log",
        "/",
        "C:\\Users\\foo\\proj",
        "C:/Users/foo/proj",
        "D:\\repo",
        "\\\\server\\share\\worktree",
    ],
)
def test_absolute_paths_are_detected_on_any_host(path_str: str) -> None:
    assert _is_path_absolute_any_platform(path_str) is True


@pytest.mark.parametrize(
    "path_str",
    [
        "relative/path",
        ".claude/worktrees/W01",
        "subdir/file.txt",
        "..",
        "",
    ],
)
def test_relative_paths_are_not_detected_as_absolute(path_str: str) -> None:
    assert _is_path_absolute_any_platform(path_str) is False
