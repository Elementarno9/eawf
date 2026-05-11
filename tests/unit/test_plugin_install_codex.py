"""Unit tests for the Codex runtime plugin installer (P14-W06)."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.render.agents import AGENT_REGISTRY
from eawf.render.hooks import HOOK_REGISTRY
from eawf.render.skills import SKILL_REGISTRY
from eawf.runtimes.codex import doctor_plugin, expected_paths, install_plugin
from eawf.runtimes.codex.plugin_install import IntegrityViolation


def test_install_creates_skill_agent_hook_config(tmp_path: Path) -> None:
    result = install_plugin(tmp_path)
    assert (tmp_path / ".codex" / "config.toml").is_file()
    assert len(result.skills) == len(SKILL_REGISTRY)
    assert len(result.agents) == len(AGENT_REGISTRY)
    assert len(result.hooks) == len(HOOK_REGISTRY)
    for delta in result.skills:
        assert delta.action == "created"


def test_install_idempotent_second_run_unchanged(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    second = install_plugin(tmp_path)
    assert second.config is not None
    assert second.config.action == "unchanged"
    for delta in second.skills + second.agents + second.hooks:
        assert delta.action == "unchanged", (delta.path, delta.action)


def test_install_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = install_plugin(tmp_path, dry_run=True)
    assert result.dry_run is True
    assert not (tmp_path / ".codex").exists()


def test_install_rejects_hand_edited_skill_without_force(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_paths, _ = expected_paths(tmp_path)
    first_skill = next(iter(skill_paths.values()))
    first_skill.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        install_plugin(tmp_path)


def test_install_force_overrides_hand_edit(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_paths, _ = expected_paths(tmp_path)
    first_skill = next(iter(skill_paths.values()))
    first_skill.write_text("tampered\n", encoding="utf-8")
    result = install_plugin(tmp_path, force=True)
    assert any(d.action == "updated" for d in result.skills)


def test_config_toml_contains_managed_block(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    config_text = (tmp_path / ".codex" / "config.toml").read_text()
    assert "[__eawf_managed]" in config_text
    assert '"1970-01-01T00:00:00+00:00"' in config_text
    assert "[[__eawf_managed.skills]]" in config_text
    assert "[[__eawf_managed.hooks]]" in config_text


def test_config_toml_preserves_user_sections(tmp_path: Path) -> None:
    cfg_path = tmp_path / ".codex" / "config.toml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text('user_setting = "keep_me"\n')
    install_plugin(tmp_path)
    text = cfg_path.read_text()
    assert 'user_setting = "keep_me"' in text
    assert "[__eawf_managed]" in text


def test_doctor_reports_clean_after_install(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    report = doctor_plugin(tmp_path)
    assert report.clean is True
    assert not report.drifted
    assert not report.missing


def test_doctor_flags_missing_files(tmp_path: Path) -> None:
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert report.missing


def test_doctor_flags_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_paths, _ = expected_paths(tmp_path)
    first_skill = next(iter(skill_paths.values()))
    first_skill.write_text("tampered\n", encoding="utf-8")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert report.drifted
