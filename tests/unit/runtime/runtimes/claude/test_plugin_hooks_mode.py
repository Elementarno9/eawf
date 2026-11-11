"""The packaged plugin hooks run whatever file mode the release path leaves.

The release workflow archives the rendered plugin tree with a step that
drops POSIX modes, so the wrappers can reach a user as 0644. Claude Code
runs each ``hooks.json`` command through a shell; these tests pin that the
commands invoke ``bash`` on the wrapper, so a non-executable wrapper still
runs instead of failing with exit 126.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess  # noqa: EAWF024 -- the hook command is a shell line; executing it is the criterion gate
from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude.plugin_package import package_plugin

pytestmark = pytest.mark.unit

_PREFIX = 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/eawf-hook.sh" '
_SESSION_END = f"{_PREFIX}session_end"


def _package(target: Path) -> Path:
    package_plugin(target, include_hooks=True, extra_tools={})
    return target


def _commands(target: Path) -> list[str]:
    body = json.loads((target / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    return [
        hook["command"]
        for entries in body["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]


def _run_command(
    command: str, *, plugin_root: Path, bin_dir: Path, home: Path
) -> subprocess.CompletedProcess[str]:
    # Claude Code exports CLAUDE_PLUGIN_ROOT and hands the command to a shell.
    return subprocess.run(
        ["sh", "-c", command],
        input=json.dumps({"hook_event_name": "SessionEnd", "session_id": "s-1"}),
        capture_output=True,
        text=True,
        cwd=home,
        env={
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(home),
        },
        check=False,
        timeout=30,
    )


def _stub_uv(tmp_path: Path) -> Path:
    # The wrapper execs ``uv run eawf hook run ...``; the stub stands in for
    # that whole chain and succeeds after draining the payload.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    return bin_dir


def test_every_hook_command_runs_its_wrapper_through_bash(tmp_path: Path) -> None:
    target = _package(tmp_path / "plugin")
    commands = _commands(target)

    assert commands, "the packaged hooks.json names no command"
    for command in commands:
        assert command.startswith(_PREFIX), command
        assert command.removeprefix(_PREFIX).isidentifier(), command
    assert (target / "hooks" / "eawf-hook.sh").is_file()


def test_wrappers_still_render_executable(tmp_path: Path) -> None:
    target = _package(tmp_path / "plugin")
    wrappers = sorted((target / "hooks").glob("*.sh"))

    assert wrappers
    assert {stat.S_IMODE(w.stat().st_mode) for w in wrappers} == {0o755}


def test_hook_command_runs_a_0644_wrapper(tmp_path: Path) -> None:
    target = _package(tmp_path / "plugin")
    for wrapper in (target / "hooks").glob("*.sh"):
        wrapper.chmod(0o644)
    bin_dir = _stub_uv(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    [command] = [c for c in _commands(target) if c == _SESSION_END]

    result = _run_command(command, plugin_root=target, bin_dir=bin_dir, home=home)

    assert result.returncode == 0, result.stderr


def test_hook_command_survives_a_plugin_root_with_spaces(tmp_path: Path) -> None:
    target = _package(tmp_path / "plugin root")
    bin_dir = _stub_uv(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    [command] = [c for c in _commands(target) if c == _SESSION_END]

    result = _run_command(command, plugin_root=target, bin_dir=bin_dir, home=home)

    assert result.returncode == 0, result.stderr


def test_hook_command_fails_when_the_wrapper_is_missing(tmp_path: Path) -> None:
    target = _package(tmp_path / "plugin")
    (target / "hooks" / "eawf-hook.sh").unlink()
    bin_dir = _stub_uv(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    [command] = [c for c in _commands(target) if c == _SESSION_END]

    result = _run_command(command, plugin_root=target, bin_dir=bin_dir, home=home)

    assert result.returncode == 127, result.stderr
