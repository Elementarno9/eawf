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
