"""Integration tests for ``eawf state resolve``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_ea_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("EA_STATE", raising=False)
    yield


def test_state_resolve_prints_workspace_path_and_reason(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    result = runner.invoke(app, ["--json", "state", "resolve", "-w", str(workspace)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["path"].endswith(str(Path(".ea") / "state.json"))
    assert payload["reason"] == "workspace_flag"


def test_state_resolve_text_output_includes_reason(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    result = runner.invoke(app, ["state", "resolve", "-w", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "reason: workspace_flag" in result.stdout
    assert str(workspace / ".ea" / "state.json") in result.stdout


def test_state_resolve_env_var_takes_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_state = tmp_path / "env_state.json"
    workspace = tmp_path / "ws"
    monkeypatch.setenv("EA_STATE", str(env_state))
    result = runner.invoke(app, ["--json", "state", "resolve", "-w", str(workspace)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["path"] == str(env_state)
    assert payload["reason"] == "env"


def test_state_resolve_pwd_upward_walks_to_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    state = repo / ".ea" / "state.json"
    state.write_text("{}", encoding="utf-8")
    deep = repo / "src" / "pkg"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    result = runner.invoke(app, ["--json", "state", "resolve"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert Path(payload["path"]) == state.resolve()
    assert payload["reason"] == "pwd_upward"


def test_state_resolve_global_workspace_flag_propagates(tmp_path: Path) -> None:
    """The root-level ``-w`` flag also works (no per-command override needed)."""
    workspace = tmp_path / "ws"
    result = runner.invoke(app, ["-w", str(workspace), "--json", "state", "resolve"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["reason"] == "workspace_flag"


def test_state_no_args_help_returns_zero_or_two() -> None:
    """``eawf state`` (no subcommand) prints help via Typer's no_args_is_help."""
    result = runner.invoke(app, ["state"])
    # Typer's no_args_is_help renders help text and exits with 0 or 2 depending on version.
    assert result.exit_code in (0, 2)
    assert "resolve" in result.stdout or "resolve" in result.stderr


def test_state_resolve_help_lists_workspace_flag() -> None:
    result = runner.invoke(app, ["state", "resolve", "--help"])
    assert result.exit_code == 0
    assert "--workspace" in result.stdout or "-w" in result.stdout
