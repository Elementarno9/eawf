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


@pytest.fixture(autouse=True)
def _isolate_user_scope_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise codex + opencode user-scope conflict detectors so the
    developer machine's real ``~/.codex/plugins/`` /
    ``~/.config/opencode/plugins/`` cannot trip the gate during tests."""
    monkeypatch.setattr(
        "eawf.cli.commands.plugin.codex_detect_user_install",
        lambda: None,
    )
    monkeypatch.setattr(
        "eawf.cli.commands.plugin.opencode_detect_user_install",
        lambda: None,
    )


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
    """An unsupported runtime maps to exit 3 (``INVALID_INPUT``).

    OpenCode + Codex landed in P14-W06/W07; use a still-deferred id
    (``goose``) to exercise the rejection path.
    """
    _equip_ea_dir(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "goose"])
    assert result.exit_code == 1, result.stdout


def test_plugin_install_after_hand_edit_aborts(tmp_path: Path) -> None:
    """Hand-edit + ``plugin install`` re-run exits 8 (``INTEGRITY_VIOLATION``)."""
    _equip_ea_dir(tmp_path)
    runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude"])
    skill_path = tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# user-edit\n")
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude"])
    assert result.exit_code == INTEGRITY_VIOLATION, result.stdout


@pytest.mark.parametrize("scope", ["project", "user"])
def test_plugin_install_codex_writes_native_layout(
    tmp_path: Path, scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``eawf plugin install codex [--scope]`` lands the native plugin tree."""
    _equip_ea_dir(tmp_path)
    if scope == "user":
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        plugin_root = fake_home / ".codex" / "plugins" / "eawf"
        config_path = fake_home / ".codex" / "config.toml"
    else:
        plugin_root = tmp_path / ".codex" / "plugins" / "eawf"
        config_path = tmp_path / ".codex" / "config.toml"
    args = ["-w", str(tmp_path), "plugin", "install", "codex", "--scope", scope]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.stdout
    assert (plugin_root / ".codex-plugin" / "plugin.json").is_file()
    assert (plugin_root / "skills").is_dir()
    text = config_path.read_text(encoding="utf-8")
    assert "[plugins.eawf]" in text
    assert "enabled = true" in text


@pytest.mark.parametrize("scope", ["project", "user"])
def test_plugin_install_opencode_drops_plugins_array(
    tmp_path: Path, scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``eawf plugin install opencode [--scope]`` writes plugin under
    auto-discovery dir; does not push ``plugin.js`` / ``eawf.js`` into
    ``plugins:[...]`` array."""
    _equip_ea_dir(tmp_path)
    if scope == "user":
        fake_xdg = tmp_path / "fake-xdg"
        fake_xdg.mkdir()
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(fake_xdg))
        plugin_dir = fake_xdg / "plugins"
        config_path = fake_xdg / "opencode.json"
    else:
        plugin_dir = tmp_path / ".opencode" / "plugins"
        config_path = tmp_path / "opencode.json"
    args = ["-w", str(tmp_path), "plugin", "install", "opencode", "--scope", scope]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.stdout
    assert (plugin_dir / "eawf.js").is_file()
    assert (plugin_dir / ".eawf-managed.json").is_file()
    parsed = json.loads(config_path.read_text(encoding="utf-8"))
    plugins = parsed.get("plugins")
    assert plugins is None or plugins == [], plugins


def test_plugin_install_claude_user_scope_rejected(tmp_path: Path) -> None:
    """``plugin install claude --scope user`` exits 3 (``INVALID_INPUT``)."""
    _equip_ea_dir(tmp_path)
    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "plugin", "install", "claude", "--scope", "user"],
    )
    assert result.exit_code == 1, result.stdout
    combined = result.stdout + (result.stderr or "")
    assert "project-scope only" in combined or "marketplace" in combined.lower()


def test_plugin_doctor_codex_finds_install_at_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``plugin doctor codex --scope <s>`` exits 0 after install at that scope."""
    _equip_ea_dir(tmp_path)
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "codex", "--scope", "user"])
    result = runner.invoke(
        app, ["-w", str(tmp_path), "plugin", "doctor", "codex", "--scope", "user"]
    )
    assert result.exit_code == 0, result.stdout


def test_plugin_package_codex_writes_marketplace_tree(tmp_path: Path) -> None:
    """``eawf plugin package codex`` emits marketplace.json + plugin tree."""
    _equip_ea_dir(tmp_path)
    target = tmp_path / "build" / "codex-mkt"
    result = runner.invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "plugin",
            "package",
            "codex",
            "--target",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.stdout
    manifest_path = target / ".agents" / "plugins" / "marketplace.json"
    assert manifest_path.is_file()
    assert not (target / "marketplace.json").exists()
    assert (target / "plugins" / "eawf" / ".codex-plugin" / "plugin.json").is_file()
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert body["name"] == "eawf-local-codex"
    assert body["plugins"][0]["name"] == "eawf"


def test_plugin_package_opencode_rejected(tmp_path: Path) -> None:
    """opencode has no marketplace; CLI rejects with InvalidInput."""
    _equip_ea_dir(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "package", "opencode"])
    assert result.exit_code == 1, result.stdout


def test_plugin_install_codex_idempotent_at_scope(tmp_path: Path) -> None:
    """Two project-scope codex installs produce byte-identical output."""
    _equip_ea_dir(tmp_path)
    runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "codex"])
    manifest = tmp_path / ".codex" / "plugins" / "eawf" / ".codex-plugin" / "plugin.json"
    snapshot = manifest.read_bytes()
    config_snapshot = (tmp_path / ".codex" / "config.toml").read_bytes()
    runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "codex"])
    assert manifest.read_bytes() == snapshot
    assert (tmp_path / ".codex" / "config.toml").read_bytes() == config_snapshot
