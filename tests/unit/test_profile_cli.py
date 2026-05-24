"""Unit tests for ``eawf profile`` Typer commands (P14-W05 / D19)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from eawf.platform.profiles import discovery
from eawf.surfaces.cli.app import app


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    discovery._clear_cache_for_tests()
    yield
    discovery._clear_cache_for_tests()


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_profile_new_writes_yaml(runner: CliRunner, tmp_path: Path, fake_home: Path) -> None:
    ws = _workspace(tmp_path)
    result = runner.invoke(
        app,
        ["--workspace", str(ws), "profile", "new", "alpha"],
    )
    assert result.exit_code == 0, result.stdout
    target = ws / ".ea" / "profiles" / "alpha.yaml"
    assert target.is_file()
    body = yaml.safe_load(target.read_text())
    assert body["name"] == "alpha"
    assert "extends" not in body


def test_profile_new_records_extends(runner: CliRunner, tmp_path: Path, fake_home: Path) -> None:
    ws = _workspace(tmp_path)
    result = runner.invoke(
        app,
        ["--workspace", str(ws), "profile", "new", "alpha", "--inherit", "core"],
    )
    assert result.exit_code == 0, result.stdout
    body = yaml.safe_load((ws / ".ea" / "profiles" / "alpha.yaml").read_text())
    assert body["extends"] == "core"


def test_profile_new_refuses_builtin_collision(
    runner: CliRunner, tmp_path: Path, fake_home: Path
) -> None:
    ws = _workspace(tmp_path)
    result = runner.invoke(
        app,
        ["--workspace", str(ws), "profile", "new", "core"],
    )
    assert result.exit_code != 0


def test_profile_new_force_overrides_collision(
    runner: CliRunner, tmp_path: Path, fake_home: Path
) -> None:
    ws = _workspace(tmp_path)
    result = runner.invoke(
        app,
        ["--workspace", str(ws), "profile", "new", "core", "--force"],
    )
    assert result.exit_code == 0, result.stdout


def test_profile_new_refuses_unknown_parent(
    runner: CliRunner, tmp_path: Path, fake_home: Path
) -> None:
    ws = _workspace(tmp_path)
    result = runner.invoke(
        app,
        ["--workspace", str(ws), "profile", "new", "alpha", "--inherit", "ghost"],
    )
    assert result.exit_code != 0


def test_profile_validate_all_passes_when_only_builtins(
    runner: CliRunner, tmp_path: Path, fake_home: Path
) -> None:
    ws = _workspace(tmp_path)
    result = runner.invoke(
        app,
        ["--workspace", str(ws), "profile", "validate", "--all"],
    )
    assert result.exit_code == 0, result.stdout


def test_profile_validate_untrusted_overlay_fails_no_input(
    runner: CliRunner, tmp_path: Path, fake_home: Path
) -> None:
    ws = _workspace(tmp_path)
    target = ws / ".ea" / "profiles" / "myprofile.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent('name: myprofile\nversion: "1.0"\n'))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(ws),
            "--no-input",
            "profile",
            "validate",
            "myprofile",
        ],
    )
    assert result.exit_code != 0


def test_profile_validate_rejects_both_name_and_all(
    runner: CliRunner, tmp_path: Path, fake_home: Path
) -> None:
    ws = _workspace(tmp_path)
    result = runner.invoke(
        app,
        ["--workspace", str(ws), "profile", "validate", "core", "--all"],
    )
    assert result.exit_code != 0
