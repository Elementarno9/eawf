"""Unit tests for the Codex runtime plugin installer (P14-I02-W01).

Covers the native plugin layout (``<plugin_root>/.codex-plugin/plugin.json``
plus skills/agents/hooks under the plugin root) and the scope-aware
install path: ``scope="project"`` writes under
``<target>/.codex/plugins/eawf/``; ``scope="user"`` writes under
``<home>/.codex/plugins/eawf/`` with ``home`` injected via the
``fake_home`` fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.render.agents import AGENT_REGISTRY
from eawf.render.hooks import HOOK_REGISTRY
from eawf.render.skills import SKILL_REGISTRY
from eawf.runtimes.codex import doctor_plugin, expected_paths, install_plugin
from eawf.runtimes.codex.plugin_install import IntegrityViolation


@pytest.fixture()
def fake_home(tmp_path: Path) -> Path:
    """Synthetic ``$HOME`` for ``scope="user"`` paths."""
    home = tmp_path / "fake-home"
    home.mkdir()
    return home


def _install_kwargs(scope: str, fake_home: Path) -> dict[str, object]:
    return {"scope": scope, "home": fake_home} if scope == "user" else {"scope": scope}


def _plugin_root(target: Path, scope: str, fake_home: Path) -> Path:
    if scope == "project":
        return target / ".codex" / "plugins" / "eawf"
    return fake_home / ".codex" / "plugins" / "eawf"


def _config_path(target: Path, scope: str, fake_home: Path) -> Path:
    if scope == "project":
        return target / ".codex" / "config.toml"
    return fake_home / ".codex" / "config.toml"


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_creates_plugin_layout(tmp_path: Path, fake_home: Path, scope: str) -> None:
    result = install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    root = _plugin_root(tmp_path, scope, fake_home)
    assert (root / ".codex-plugin" / "plugin.json").is_file()
    assert (root / ".codex-plugin" / ".eawf-managed.json").is_file()
    assert len(result.skills) == len(SKILL_REGISTRY)
    assert len(result.agents) == len(AGENT_REGISTRY)
    assert len(result.hooks) == len(HOOK_REGISTRY)
    assert result.scope == scope
    for delta in result.skills:
        assert delta.action == "created"


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_idempotent_second_run_unchanged(
    tmp_path: Path, fake_home: Path, scope: str
) -> None:
    install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    second = install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    assert second.config is not None
    assert second.config.action == "unchanged"
    assert second.manifest is not None
    assert second.manifest.action == "unchanged"
    assert second.sidecar is not None
    assert second.sidecar.action == "unchanged"
    for delta in second.skills + second.agents + second.hooks:
        assert delta.action == "unchanged", (delta.path, delta.action)


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_dry_run_writes_nothing(tmp_path: Path, fake_home: Path, scope: str) -> None:
    result = install_plugin(tmp_path, dry_run=True, **_install_kwargs(scope, fake_home))
    assert result.dry_run is True
    assert not _plugin_root(tmp_path, scope, fake_home).exists()


def test_install_rejects_hand_edited_skill_without_force(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_paths, _ = expected_paths(tmp_path)
    first_skill_region = next(k for k in skill_paths if k.startswith("plugin.codex.skill."))
    first_skill = skill_paths[first_skill_region]
    first_skill.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        install_plugin(tmp_path)


def test_install_force_overrides_hand_edit(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_paths, _ = expected_paths(tmp_path)
    first_skill_region = next(k for k in skill_paths if k.startswith("plugin.codex.skill."))
    first_skill = skill_paths[first_skill_region]
    first_skill.write_text("tampered\n", encoding="utf-8")
    result = install_plugin(tmp_path, force=True)
    assert any(d.action == "updated" for d in result.skills)


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_codex_emits_manifest_toml(tmp_path: Path, fake_home: Path, scope: str) -> None:
    """Manifest is JSON at ``.codex-plugin/plugin.json`` per Codex schema."""
    install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    manifest_path = _plugin_root(tmp_path, scope, fake_home) / ".codex-plugin" / "plugin.json"
    assert manifest_path.is_file()
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert body["name"] == "eawf"
    assert body["version"] == "1.0"
    assert body["description"]
    assert body["skills"] == "./skills/"
    assert body["hooks"] == "./hooks/"


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_codex_writes_enabled_entry_in_config_toml(
    tmp_path: Path, fake_home: Path, scope: str
) -> None:
    install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    config_path = _config_path(tmp_path, scope, fake_home)
    text = config_path.read_text(encoding="utf-8")
    assert "[plugins.eawf]" in text
    assert "enabled = true" in text
    assert "# ---- __eawf_managed begin ----" in text
    assert "# ---- __eawf_managed end ----" in text
    # Legacy __eawf_managed TOML table must NOT appear inside config.toml.
    assert "[[__eawf_managed.skills]]" not in text


def test_config_toml_preserves_user_sections(tmp_path: Path) -> None:
    cfg_path = tmp_path / ".codex" / "config.toml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text('user_setting = "keep_me"\n', encoding="utf-8")
    install_plugin(tmp_path)
    text = cfg_path.read_text(encoding="utf-8")
    assert 'user_setting = "keep_me"' in text
    assert "[plugins.eawf]" in text


@pytest.mark.parametrize("scope", ["project", "user"])
def test_doctor_reports_clean_after_install(tmp_path: Path, fake_home: Path, scope: str) -> None:
    install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    report = doctor_plugin(tmp_path, scope=scope, home=fake_home if scope == "user" else None)
    assert report.clean is True, (report.drifted, report.missing)
    assert not report.drifted
    assert not report.missing


def test_doctor_flags_missing_files(tmp_path: Path) -> None:
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert report.missing


def test_doctor_flags_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_paths, _ = expected_paths(tmp_path)
    first_skill_region = next(k for k in skill_paths if k.startswith("plugin.codex.skill."))
    first_skill = skill_paths[first_skill_region]
    first_skill.write_text("tampered\n", encoding="utf-8")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert report.drifted


def test_doctor_reports_legacy_flat_paths(tmp_path: Path) -> None:
    """Flat ``<target>/.codex/{skills,agents,hooks}/`` is reported as legacy."""
    legacy_dir = tmp_path / ".codex" / "skills"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "stub.md").write_text("# stub\n", encoding="utf-8")
    install_plugin(tmp_path)
    report = doctor_plugin(tmp_path)
    assert legacy_dir in report.legacy_paths
