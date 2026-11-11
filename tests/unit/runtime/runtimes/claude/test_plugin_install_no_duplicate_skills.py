"""The Claude installer renders no project skill once the enabled eawf plugin serves them.

Claude Code loads a plugin skill and a project skill of the same name side by
side, so a project copy beside the enabled plugin doubles every skill. Every
test runs against a temporary ``HOME`` and the real registry detector, so
nothing reads the operator's own Claude files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.runtimes.claude import plugin_conflict, plugin_install, statusline_install
from eawf.runtime.runtimes.claude.plugin_install import expected_paths, install_plugin

_RESEARCH_SKILL = Path(".claude/skills/research/SKILL.md")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``HOME`` at an empty tmp dir and restore the real home-reading seams."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setattr(
        plugin_install, "detect_marketplace_install", plugin_conflict.detect_marketplace_install
    )
    monkeypatch.setattr(
        plugin_install, "global_settings_path", statusline_install.global_settings_path
    )
    return home_dir


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "project"
    target.mkdir()
    return target


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _register_plugin(home: Path) -> None:
    _write_json(
        home / ".claude" / "plugins" / "installed_plugins.json",
        {
            "version": 2,
            "plugins": {"eawf@eawf": [{"scope": "user", "installPath": str(home / "eawf-plugin")}]},
        },
    )


def _enable_plugin(home: Path, *, enabled: bool = True) -> None:
    _write_json(home / ".claude" / "settings.json", {"enabledPlugins": {"eawf@eawf": enabled}})


def _skill_files(project: Path) -> list[Path]:
    return sorted((project / ".claude" / "skills").glob("*/SKILL.md"))


def test_plugin_enabled_no_project_skills(home: Path, project: Path) -> None:
    install_plugin(project)
    assert (project / _RESEARCH_SKILL).is_file()

    _register_plugin(home)
    _enable_plugin(home)
    result = install_plugin(project)

    assert not (project / _RESEARCH_SKILL).exists()
    assert not (project / _RESEARCH_SKILL).parent.exists()
    assert _skill_files(project) == []
    assert result.skills
    assert {delta.action for delta in result.skills} == {"removed"}
    manifest = json.loads((project / ".ea" / "indexes" / "generated.json").read_text())
    assert not [key for key in manifest["generated"] if "::plugin.claude.skill." in key]


def test_plugin_installed_but_disabled_keeps_project_skills(home: Path, project: Path) -> None:
    _register_plugin(home)
    _enable_plugin(home, enabled=False)

    install_plugin(project)

    assert (project / _RESEARCH_SKILL).is_file()


def test_project_settings_disable_overrides_user_enable(home: Path, project: Path) -> None:
    _register_plugin(home)
    _enable_plugin(home)
    _write_json(project / ".claude" / "settings.json", {"enabledPlugins": {"eawf@eawf": False}})

    install_plugin(project)

    assert (project / _RESEARCH_SKILL).is_file()


def test_edited_project_skill_is_kept(home: Path, project: Path) -> None:
    install_plugin(project)
    edited = project / _RESEARCH_SKILL
    edited.write_text("my own research notes\n", encoding="utf-8")
    _register_plugin(home)
    _enable_plugin(home)

    result = install_plugin(project)

    assert edited.read_text(encoding="utf-8") == "my own research notes\n"
    assert [delta.action for delta in result.skills if delta.path == edited] == ["kept"]


def test_dry_run_reports_removal_and_deletes_nothing(home: Path, project: Path) -> None:
    install_plugin(project)
    _register_plugin(home)
    _enable_plugin(home)

    result = install_plugin(project, dry_run=True)

    assert (project / _RESEARCH_SKILL).is_file()
    assert {delta.action for delta in result.skills} == {"removed"}


def test_doctor_expects_no_skill_once_plugin_serves_them(home: Path, project: Path) -> None:
    _register_plugin(home)
    _enable_plugin(home)

    served, _ = expected_paths(project, marketplace_installed=True)
    unserved, _ = expected_paths(project, marketplace_installed=False)

    assert not [region for region in served if region.startswith("plugin.claude.skill.")]
    assert "plugin.claude.skill.research" in unserved


def test_malformed_project_settings_is_refused(home: Path, project: Path) -> None:
    _register_plugin(home)
    _enable_plugin(home)
    settings = project / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(ValueError, match="json object"):
        install_plugin(project)
