"""A managed session start launches the daemon in the background, and nothing else does.

The launcher is stubbed: it records each launch and fails the test if the
hook ever waits on the child.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the hook's exit and stdout are the contract

from eawf.runtime.hooks import daemon_start
from eawf.runtime.hooks import runner as runner_module
from eawf.runtime.hooks.context import HookContext, HookContextKind
from eawf.runtime.hooks.daemon_start import (
    DAEMON_START_HOOK,
    daemon_start_argv,
    request_daemon_start,
)
from eawf.runtime.hooks.health import HOOK_RAISED, read_health_rows
from eawf.surfaces.cli._daemon_client import DaemonNotRunningError
from eawf.surfaces.cli.app import app

_EVENTS = (
    "user_prompt_submit",
    "pre_tool_use",
    "post_tool_use",
    "post_tool_use_failure",
    "permission_request",
    "subagent_start",
    "subagent_stop",
    "pre_compact",
    "session_end",
)


class _Launches:
    """Stands in for ``subprocess.Popen`` and records every launch."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._error = error

    def __call__(self, argv: list[str], **kwargs: Any) -> _Launches:
        if self._error is not None:
            raise self._error
        self.calls.append((argv, kwargs))
        return self

    def wait(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("the hook waited on the daemon start")

    communicate = wait


def _refusing_daemon() -> Any:
    raise ConnectionRefusedError("no daemon")


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runner_module, "_default_daemon_client_factory", _refusing_daemon)
    root = tmp_path / "repo"
    (root / ".ea").mkdir(parents=True)
    (root / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    return root


def _context(monkeypatch: pytest.MonkeyPatch, tree: Path, kind: HookContextKind) -> None:
    monkeypatch.setattr(
        "eawf.runtime.hooks.context.resolve_hook_context",
        lambda _cwd: HookContext(kind=kind, top=tree, main=tree),
    )


def _launcher(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None) -> _Launches:
    launches = _Launches(error)
    monkeypatch.setattr(daemon_start, "Popen", launches)
    return launches


def _hook_run(tree: Path, event: str) -> Any:
    return CliRunner().invoke(
        app,
        ["--workspace", str(tree), "hook", "run", event, "--runtime", "claude"],
        input=json.dumps({"session_id": "s1", "cwd": str(tree), "tool_name": "Bash"}),
    )


def test_a_managed_session_start_requests_one_detached_start(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _context(monkeypatch, tree, "managed")
    launches = _launcher(monkeypatch)

    result = _hook_run(tree, "session_start")

    assert result.exit_code == 0, result.output
    ((argv, kwargs),) = launches.calls
    assert argv == daemon_start_argv(tree)
    assert argv[1:] == ["-m", "eawf.runtime.daemon.main"]
    assert kwargs["cwd"] == tree
    assert kwargs["env"]["EA_STATE"] == str(tree / ".ea" / "state.json")
    assert kwargs["start_new_session"] is True


@pytest.mark.parametrize("kind", ["unmanaged-git", "no-git"])
def test_no_start_outside_a_managed_repository(
    tree: Path, monkeypatch: pytest.MonkeyPatch, kind: HookContextKind
) -> None:
    _context(monkeypatch, tree, kind)
    launches = _launcher(monkeypatch)

    result = _hook_run(tree, "session_start")

    assert result.exit_code == 0, result.output
    assert launches.calls == []


def test_no_start_when_no_wrapper_named_the_repository(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _context(monkeypatch, tree, "managed")
    launches = _launcher(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["hook", "run", "session_start", "--runtime", "claude"],
        input=json.dumps({"session_id": "s1", "cwd": str(tree)}),
    )

    assert result.exit_code == 0, result.output
    assert launches.calls == []


@pytest.mark.parametrize("event", _EVENTS)
def test_no_start_on_any_other_event(
    tree: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    _context(monkeypatch, tree, "managed")
    launches = _launcher(monkeypatch)

    result = _hook_run(tree, event)

    assert result.exit_code == 0, result.output
    assert launches.calls == []


def test_a_failed_launch_leaves_the_answer_and_one_health_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runner_module, "_default_daemon_client_factory", _refusing_daemon)
    outputs: list[str] = []
    for name, error in (("ok", None), ("broken", OSError("no interpreter"))):
        root = tmp_path / name
        (root / ".ea").mkdir(parents=True)
        (root / ".ea" / "state.json").write_text("{}", encoding="utf-8")
        _context(monkeypatch, root, "managed")
        _launcher(monkeypatch, error)
        result = _hook_run(root, "session_start")
        assert result.exit_code == 0, result.output
        outputs.append(result.stdout)

    assert outputs[0] == outputs[1]
    recorded = read_health_rows(tmp_path / "broken" / ".ea").rows
    rows = [r for r in recorded if r.hook == DAEMON_START_HOOK]
    assert [(r.code, r.error, r.session_id) for r in rows] == [(HOOK_RAISED, "OSError", "s1")]


def _not_running() -> Any:
    raise DaemonNotRunningError("no eawfd daemon is running")


@pytest.mark.parametrize(
    ("factory", "warned"),
    [(_not_running, False), (_refusing_daemon, True)],
    ids=["not-up-yet", "refused"],
)
def test_a_fresh_session_warns_the_next_only_about_real_failures(
    tree: Path, monkeypatch: pytest.MonkeyPatch, factory: Any, warned: bool
) -> None:
    _context(monkeypatch, tree, "managed")
    _launcher(monkeypatch)
    monkeypatch.setattr(runner_module, "_default_daemon_client_factory", factory)

    first = _hook_run(tree, "session_start")
    second = _hook_run(tree, "session_start")

    assert (first.exit_code, second.exit_code) == (0, 0)
    assert ("hook failures since the last session" in second.stdout) is warned
    assert bool(read_health_rows(tree / ".ea").rows) is warned


def test_request_daemon_start_detaches_every_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches = _launcher(monkeypatch)

    request_daemon_start(tmp_path)

    ((_argv, kwargs),) = launches.calls
    devnull = daemon_start.DEVNULL
    assert (kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]) == (devnull, devnull, devnull)
    assert kwargs["close_fds"] is True


@pytest.mark.parametrize(
    "runner",
    [Path(".venv") / "bin" / "python", Path(".venv") / "Scripts" / "python.exe"],
    ids=["posix", "windows"],
)
def test_daemon_start_prefers_the_main_checkouts_runner(tmp_path: Path, runner: Path) -> None:
    """A hook in a linked worktree runs the branch's eawf; the daemon must not."""
    assert daemon_start_argv(tmp_path)[0] == sys.executable
    python = tmp_path / runner
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    assert daemon_start_argv(tmp_path) == [str(python), "-m", "eawf.runtime.daemon.main"]


def test_daemon_start_ignores_a_runner_that_is_a_directory(tmp_path: Path) -> None:
    (tmp_path / ".venv" / "bin" / "python").mkdir(parents=True)
    assert daemon_start_argv(tmp_path)[0] == sys.executable


def test_request_daemon_start_raises_what_the_launcher_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _launcher(monkeypatch, OSError("no interpreter"))

    with pytest.raises(OSError, match="no interpreter"):
        request_daemon_start(tmp_path)
