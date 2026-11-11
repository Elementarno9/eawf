"""A 0.7.1 plugin hook wrapper keeps working against the current ``eawf hook run``.

Right after the package is upgraded and before ``eawf plugin update``, the
installed plugin still runs the 0.7.1 per-event wrappers: ``set -euo
pipefail``, a payload synthesised from positional arguments when stdin is
empty, ``--target-epoch 2`` on every call, and exit 2 on a judged pre-tool
call whenever the command fails. Those wrappers, copied verbatim from the
0.7.1 render under ``tests/fixtures/hooks/claude-0.7.1``, are run here against
the current CLI through a stand-in ``uv`` with the daemon down.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import eawf
from tests._epoch2_helpers import lay_epoch2_tree
from tests._linked_worktree import git

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed"),
]

_WRAPPERS = Path(__file__).resolve().parents[3] / "fixtures" / "hooks" / "claude-0.7.1"
_EVENTS: tuple[str, ...] = tuple(sorted(p.stem for p in _WRAPPERS.glob("*.sh")))
_SRC = Path(eawf.__file__).resolve().parents[1]

# Runs the current CLI with every daemon call refused, so no daemon starts.
_LAUNCHER = """
import sys
import eawf.runtime.hooks.runner as runner
import eawf.surfaces.cli._daemon_client as daemon_client

def _down(*_args, **_kwargs):
    raise ConnectionRefusedError("daemon down")

runner._default_daemon_client_factory = _down
daemon_client.DaemonClient = _down
sys.argv = ["eawf", *sys.argv[1:]]
from eawf.__main__ import main
main()
"""

# Stands in for uv: drops ``run [--project <root> --no-sync | --no-project]``
# and runs the rest of the line through the launcher.
_STUB_UV = """#!/bin/bash
while [ $# -gt 0 ] && [ "$1" != "eawf" ]; do shift; done
shift
exec "$EAWF_TEST_PYTHON" "$EAWF_TEST_LAUNCHER" "$@"
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "uv").write_text(_STUB_UV, encoding="utf-8")
    (stubs / "uv").chmod(0o755)
    launcher = tmp_path / "launcher.py"
    launcher.write_text(_LAUNCHER, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "xdg"
    runtime.mkdir()
    return {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(runtime),
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": str(_SRC),
        "EAWF_TEST_PYTHON": sys.executable,
        "EAWF_TEST_LAUNCHER": str(launcher),
    }


def _repo(root: Path, *, managed: bool) -> Path:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    if managed:
        lay_epoch2_tree(root)
    return root.resolve()


def _payload(event: str, cwd: Path, command: str = "git status") -> dict[str, Any]:
    return {
        "session_id": "host-1",
        "cwd": str(cwd),
        "hook_event_name": event,
        "prompt": "/eawf:plan x",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": ""},
    }


def _run(
    event: str, stdin: str, *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_WRAPPERS / f"{event}.sh")],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=60,
        check=False,
    )


def _no_usage_error(result: subprocess.CompletedProcess[str]) -> None:
    for marker in ("No such option", "Usage:", "could not run"):
        assert marker not in result.stderr, result.stderr


def test_the_fixture_is_the_071_wrapper_set() -> None:
    assert len(_EVENTS) == 10
    for event in _EVENTS:
        text = (_WRAPPERS / f"{event}.sh").read_text(encoding="utf-8")
        assert f"eawf hook run {event} --runtime claude --target-epoch 2" in text


@pytest.mark.parametrize("managed", [True, False], ids=["managed", "unmanaged"])
@pytest.mark.parametrize("event", _EVENTS)
def test_a_071_wrapper_call_passes_against_the_current_cli(
    tmp_path: Path, env: dict[str, str], event: str, managed: bool
) -> None:
    repo = _repo(tmp_path / "repo", managed=managed)

    result = _run(event, json.dumps(_payload(event, repo)), cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    _no_usage_error(result)
    assert '"permissionDecision":"deny"' not in result.stdout.replace(" ", "")
    if not managed:
        assert result.stdout == ""


@pytest.mark.parametrize("event", _EVENTS)
def test_a_071_wrapper_with_empty_stdin_still_passes(
    tmp_path: Path, env: dict[str, str], event: str
) -> None:
    """The positional fallback payload carries no ``cwd``; it answers for the process cwd."""
    repo = _repo(tmp_path / "repo", managed=True)
    result = _run(event, "", cwd=repo, env=env)
    assert result.returncode == 0, result.stderr
    _no_usage_error(result)


def test_a_071_pre_tool_wrapper_still_relays_a_real_deny(
    tmp_path: Path, env: dict[str, str]
) -> None:
    repo = _repo(tmp_path / "repo", managed=True)
    payload = _payload("pre_tool_use", repo, command="rm -rf .ea")

    result = _run("pre_tool_use", json.dumps(payload), cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "canonical_store_edit" in decision["permissionDecisionReason"]
    # The refused daemon client started nothing.
    assert not any(p.name.startswith("eawfd") for p in Path(env["XDG_RUNTIME_DIR"]).rglob("*"))
    assert ".eawfd" not in os.listdir(env["HOME"])
