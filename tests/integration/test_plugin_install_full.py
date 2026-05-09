"""Integration tests for ``eawf plugin install/update/doctor claude``.

End-to-end coverage via the Typer dispatcher: a temp ``.ea/``-equipped
repo is installed, doctor reports clean, an external hand-edit is
applied, doctor reports drift, and update aborts with exit 8.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.cli.exit_codes import INTEGRITY_VIOLATION

pytestmark = pytest.mark.integration

runner = CliRunner()


def _equip_ea_dir(target: Path) -> None:
    """Drop a minimal ``.ea/`` skeleton under *target* (no state.json needed)."""
    (target / ".ea").mkdir(parents=True, exist_ok=True)
    (target / ".ea" / "indexes").mkdir(exist_ok=True)


def test_plugin_install_via_cli_dry_run(tmp_path: Path) -> None:
    """``eawf plugin install claude --dry-run`` exits 0 and writes nothing."""
    _equip_ea_dir(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "dry-run" in result.stdout
    assert not (tmp_path / ".claude").exists()


def test_plugin_install_via_cli_writes_tree(tmp_path: Path) -> None:
    """``eawf plugin install claude`` produces the full plugin tree."""
    _equip_ea_dir(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude"])
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / ".claude" / "skills" / "research" / "SKILL.md").exists()
    assert (tmp_path / ".claude" / "agents" / "executor.md").exists()
    assert (tmp_path / ".claude" / "hooks" / "pre_commit.sh").exists()
    settings = tmp_path / ".claude" / "settings.json"
    parsed = json.loads(settings.read_text(encoding="utf-8"))
    assert "__eawf_managed" in parsed


def test_plugin_install_then_doctor_clean(tmp_path: Path) -> None:
    _equip_ea_dir(tmp_path)
    runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude"])
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "doctor", "claude"])
    assert result.exit_code == 0, result.stdout
    assert "drifted=0 missing=0" in result.stdout


def test_plugin_install_then_hand_edit_then_doctor_dirty(tmp_path: Path) -> None:
    """Hand-edit → doctor exits 8 (``INTEGRITY_VIOLATION``)."""
    _equip_ea_dir(tmp_path)
    runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude"])
    skill_path = tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# user-edit\n")
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "doctor", "claude"])
    assert result.exit_code == INTEGRITY_VIOLATION, result.stdout


def test_plugin_update_after_hand_edit_aborts(tmp_path: Path) -> None:
    """Hand-edit + ``plugin update`` exits 8 with the canonical envelope."""
    _equip_ea_dir(tmp_path)
    runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude"])
    skill_path = tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# user-edit\n")
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "update", "claude"])
    assert result.exit_code == INTEGRITY_VIOLATION, result.stdout


def test_plugin_install_idempotent_via_cli(tmp_path: Path) -> None:
    """Two consecutive ``plugin install`` invocations are byte-stable."""
    _equip_ea_dir(tmp_path)
    runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude"])
    snapshot_settings = (tmp_path / ".claude" / "settings.json").read_bytes()
    snapshot_skill = (tmp_path / ".claude" / "skills" / "research" / "SKILL.md").read_bytes()
    runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude"])
    assert (tmp_path / ".claude" / "settings.json").read_bytes() == snapshot_settings
    assert (
        tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    ).read_bytes() == snapshot_skill


def test_plugin_install_unknown_runtime_exits_invalid_input(tmp_path: Path) -> None:
    """An unsupported runtime maps to exit 3 (``INVALID_INPUT``)."""
    _equip_ea_dir(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "opencode"])
    assert result.exit_code == 3, result.stdout


def test_plugin_install_after_hand_edit_aborts(tmp_path: Path) -> None:
    """Hand-edit + ``plugin install`` re-run exits 8 (``INTEGRITY_VIOLATION``)."""
    _equip_ea_dir(tmp_path)
    runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude"])
    skill_path = tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# user-edit\n")
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude"])
    assert result.exit_code == INTEGRITY_VIOLATION, result.stdout
