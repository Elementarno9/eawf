"""Integration coverage for repo bootstrap/link aliases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


def _init_repo(target: Path, code: str = "DEMO") -> None:
    res = runner.invoke(
        app,
        [
            "--no-input",
            "init",
            "--project-code",
            code,
            "--project-title",
            "Demo Repo",
            "--target",
            str(target),
        ],
    )
    assert res.exit_code == 0, res.output


def test_project_init_upgrade_fills_legacy_project_record(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state_path = repo / ".ea" / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["project"] = None
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "-w",
            str(repo),
            "project",
            "init",
            "DEMO",
            "--title",
            "Demo Repo",
            "--domains",
            "demo",
            "--upgrade",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["project"]["code"] == "DEMO"
    assert payload["project"]["domains"] == ["demo"]


def test_repo_register_alias_adds_registry_entry(tmp_path: Path) -> None:
    repo = tmp_path / "Repos" / "demo"
    repo.mkdir(parents=True)
    _init_repo(repo)
    registry_path = tmp_path / "registry.json"

    res = runner.invoke(
        app,
        ["repo", "register", str(repo), "--registry-path", str(registry_path)],
    )
    assert res.exit_code == 0, res.output
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["repos"]["DEMO"]["path"] == str(repo.resolve())


def test_repo_link_workspace_alias_cross_links_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    repo = tmp_path / "Repos" / "demo"
    workspace_state = workspace / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(workspace_state))

    res = runner.invoke(app, ["workspace", "init", "MAIN", "--title", "Main Workspace"])
    assert res.exit_code == 0, res.output
    repo.mkdir(parents=True)
    _init_repo(repo)

    res = runner.invoke(
        app,
        [
            "repo",
            "link-workspace",
            "MAIN",
            "DEMO",
            "--workspace-state",
            str(workspace_state),
            "--target",
            str(repo),
        ],
    )
    assert res.exit_code == 0, res.output
    ws_payload = json.loads(workspace_state.read_text(encoding="utf-8"))
    repo_payload = json.loads((repo / ".ea" / "state.json").read_text(encoding="utf-8"))
    assert ws_payload["workspace"]["repos"]["DEMO"]["project_code"] == "DEMO"
    assert repo_payload["indexes"]["workspace_code"] == "MAIN"
