"""Integration coverage for repo bootstrap and registry aliases."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_project_init_upgrade_refuses_on_an_initialised_tree(tmp_path: Path) -> None:
    """REL-021: ``eawf init`` bears the tree at epoch 2, so the epoch-1 upgrade refuses."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    state_path = repo / ".ea" / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["project"] = None
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    before = state_path.read_bytes()

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
    assert res.exit_code == 1, res.output
    assert "legacy_operation_removed" in res.output
    assert "run `eawf repository create` instead" in res.output
    assert state_path.read_bytes() == before


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
