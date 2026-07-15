"""Tests for the ``git`` statusline module (Phase 4 W06)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.runtimes.claude.statusline_modules import git as git_module


def test_git_module_clean_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clean tree → ``git:<branch>`` with ``status="ok"``."""

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        first = args[1]
        if first == "rev-parse":
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if first == "status":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    seg = git_module.build({"cwd": str(tmp_path)}, None)
    assert seg.module == "git"
    assert seg.text == "git:main"
    assert seg.status == "ok"


def test_git_module_dirty_branch_marks_asterisk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        first = args[1]
        if first == "rev-parse":
            return subprocess.CompletedProcess(args, 0, stdout="feature/x\n", stderr="")
        if first == "status":
            return subprocess.CompletedProcess(args, 0, stdout=" M src/foo.py\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    seg = git_module.build({"cwd": str(tmp_path)}, None)
    assert seg.text == "git:feature/x*"
    assert seg.status == "warn"


def test_git_module_detached_head_renders_short_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        first = args[1]
        # rev-parse --abbrev-ref HEAD on detached returns "HEAD".
        if first == "rev-parse" and "--abbrev-ref" in args:
            return subprocess.CompletedProcess(args, 0, stdout="HEAD\n", stderr="")
        if first == "rev-parse" and "--short=7" in args:
            return subprocess.CompletedProcess(args, 0, stdout="abc1234\n", stderr="")
        if first == "status":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    seg = git_module.build({"cwd": str(tmp_path)}, None)
    assert seg.text == "git:HEAD@abc1234"
    assert seg.status == "ok"


def test_git_module_missing_git_binary_renders_dash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git stubbed away")

    monkeypatch.setattr(subprocess, "run", fake_run)
    seg = git_module.build({"cwd": str(tmp_path)}, None)
    assert seg.text == "git:-"
    assert seg.status == "missing"


def test_git_module_uses_state_parent_when_no_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the Claude payload omits ``cwd`` the module falls back to the
    workspace root (parent of ``.ea/``) inferred from *state_path*."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir()
    state_path.touch()

    seen_cwd: list[Path] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_cwd.append(kwargs.get("cwd"))
        return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    git_module.build({}, state_path)
    assert seen_cwd[0] == tmp_path
