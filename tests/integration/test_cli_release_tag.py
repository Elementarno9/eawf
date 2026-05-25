"""Integration tests for ``eawf release tag``.

The verb shells out to git, so each test drives it inside a throwaway
git repo under ``tmp_path``. Covers the dry-run plan, real annotated-tag
creation, the already-exists guard, and the dirty-tree guard.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()

pytestmark = pytest.mark.integration


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _init_repo(path: Path) -> None:
    _git(["init"], path)
    _git(["config", "user.email", "test@example.invalid"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "f.txt"], path)
    _git(["commit", "-m", "init"], path)


def test_release_tag_dry_run_creates_no_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9", "--dry-run"])
    assert result.exit_code == 0
    assert "would create tag v9.9.9" in result.stdout
    assert _git(["tag", "--list"], tmp_path).strip() == ""


def test_release_tag_push_dry_run_mentions_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9", "--push", "--dry-run"])
    assert result.exit_code == 0
    assert "triggers pipeline" in result.stdout


def test_release_tag_creates_annotated_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9"])
    assert result.exit_code == 0
    assert "tagged v9.9.9" in result.stdout
    assert "v9.9.9" in _git(["tag", "--list"], tmp_path)


def test_release_tag_existing_tag_without_force_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _git(["tag", "v9.9.9"], tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9"])
    assert result.exit_code != 0
    assert "already exists" in (result.stdout + str(result.exception or ""))


def test_release_tag_dirty_tree_without_force_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["release", "tag", "9.9.9"])
    assert result.exit_code != 0
    assert "dirty" in (result.stdout + str(result.exception or ""))
