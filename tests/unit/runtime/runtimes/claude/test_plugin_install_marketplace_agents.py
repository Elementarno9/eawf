"""The Claude installer leaves agents to an installed eawf marketplace plugin.

The marketplace plugin ships every agent, so a project copy beside it makes
Claude Code load each agent twice with undefined precedence. Skills, hooks
and settings are unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude import plugin_install
from eawf.runtime.runtimes.claude.plugin_conflict import CCPluginConflict
from eawf.runtime.runtimes.claude.plugin_doctor import doctor_plugin
from eawf.runtime.runtimes.claude.plugin_install import install_plugin
from eawf.runtime.runtimes.claude.plugin_update import update_plugin
from eawf.runtime.runtimes.plugin_sync import sync_plugins
from eawf.surfaces.render.agents import AGENT_REGISTRY

_MANIFEST = Path(".ea") / "indexes" / "generated.json"


@pytest.fixture
def plugin_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CCPluginConflict:
    conflict = CCPluginConflict(plugin_dir=tmp_path / "eawf-plugin")
    monkeypatch.setattr(plugin_install, "detect_marketplace_install", lambda: conflict)
    return conflict


def _agent_region_ids(target: Path) -> set[str]:
    generated = json.loads((target / _MANIFEST).read_text(encoding="utf-8"))["generated"]
    return {
        entry["region_id"]
        for entry in generated.values()
        if entry["region_id"].startswith("plugin.claude.agent.")
    }


def _agents_dir(target: Path) -> Path:
    return target / ".claude" / "agents"


def test_install_plugin_renders_no_agent_file_when_plugin_installed(
    tmp_path: Path, plugin_installed: CCPluginConflict
) -> None:
    result = install_plugin(tmp_path, extra_tools={})

    assert result.agents == []
    assert not (tmp_path / ".claude" / "agents").exists()
    assert result.skills
    assert all(delta.path.exists() for delta in result.skills)
    assert _agent_region_ids(tmp_path) == set()


def test_install_plugin_renders_every_agent_file_without_plugin(tmp_path: Path) -> None:
    result = install_plugin(tmp_path, extra_tools={})

    written = sorted(path.name for path in (tmp_path / ".claude" / "agents").iterdir())
    assert written == sorted(f"{spec.role}.md" for spec in AGENT_REGISTRY)
    assert len(result.agents) == len(AGENT_REGISTRY)
    assert _agent_region_ids(tmp_path) == {
        f"plugin.claude.agent.{spec.role}" for spec in AGENT_REGISTRY
    }


def test_install_plugin_drops_agent_manifest_rows_once_plugin_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugin(tmp_path, extra_tools={})
    assert len(_agent_region_ids(tmp_path)) == len(AGENT_REGISTRY)
    conflict = CCPluginConflict(plugin_dir=tmp_path / "eawf-plugin")
    monkeypatch.setattr(plugin_install, "detect_marketplace_install", lambda: conflict)

    result = install_plugin(tmp_path, extra_tools={})

    assert [delta.action for delta in result.agents] == ["removed"] * len(AGENT_REGISTRY)
    assert not any((tmp_path / ".claude" / "agents").iterdir())
    assert _agent_region_ids(tmp_path) == set()


def test_install_plugin_keeps_edited_agent_file_once_plugin_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugin(tmp_path, extra_tools={})
    edited = _agents_dir(tmp_path) / f"{AGENT_REGISTRY[0].role}.md"
    edited.write_text("my own agent\n", encoding="utf-8")
    conflict = CCPluginConflict(plugin_dir=tmp_path / "eawf-plugin")
    monkeypatch.setattr(plugin_install, "detect_marketplace_install", lambda: conflict)

    result = install_plugin(tmp_path, extra_tools={})

    actions = {delta.path.name: delta.action for delta in result.agents}
    assert actions.pop(edited.name) == "kept"
    assert set(actions.values()) == {"removed"}
    assert len(actions) == len(AGENT_REGISTRY) - 1
    assert edited.read_text(encoding="utf-8") == "my own agent\n"
    assert [path.name for path in _agents_dir(tmp_path).iterdir()] == [edited.name]


def test_install_plugin_force_still_keeps_edited_agent_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugin(tmp_path, extra_tools={})
    edited = _agents_dir(tmp_path) / f"{AGENT_REGISTRY[-1].role}.md"
    edited.write_text("edited\n", encoding="utf-8")
    conflict = CCPluginConflict(plugin_dir=tmp_path / "eawf-plugin")
    monkeypatch.setattr(plugin_install, "detect_marketplace_install", lambda: conflict)

    install_plugin(tmp_path, force=True, extra_tools={})

    assert edited.read_text(encoding="utf-8") == "edited\n"


def test_install_plugin_keeps_unowned_agent_file_when_plugin_installed(
    tmp_path: Path, plugin_installed: CCPluginConflict
) -> None:
    unowned = _agents_dir(tmp_path) / f"{AGENT_REGISTRY[0].role}.md"
    unowned.parent.mkdir(parents=True)
    unowned.write_text("hand written\n", encoding="utf-8")

    result = install_plugin(tmp_path, extra_tools={})

    assert [(delta.path, delta.action) for delta in result.agents] == [(unowned, "kept")]
    assert unowned.exists()


def test_install_plugin_dry_run_reports_removal_but_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugin(tmp_path, extra_tools={})
    conflict = CCPluginConflict(plugin_dir=tmp_path / "eawf-plugin")
    monkeypatch.setattr(plugin_install, "detect_marketplace_install", lambda: conflict)

    result = install_plugin(tmp_path, dry_run=True, extra_tools={})

    assert [delta.action for delta in result.agents] == ["removed"] * len(AGENT_REGISTRY)
    assert len(list(_agents_dir(tmp_path).iterdir())) == len(AGENT_REGISTRY)
    assert len(_agent_region_ids(tmp_path)) == len(AGENT_REGISTRY)


def test_update_plugin_retires_owned_agent_files_once_plugin_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugin(tmp_path)
    conflict = CCPluginConflict(plugin_dir=tmp_path / "eawf-plugin")
    monkeypatch.setattr(plugin_install, "detect_marketplace_install", lambda: conflict)

    checked = update_plugin(tmp_path, check=True)
    assert len(list(_agents_dir(tmp_path).iterdir())) == len(AGENT_REGISTRY)
    updated = update_plugin(tmp_path)

    assert {delta.action for delta in checked.agents} == {"removed"}
    assert {delta.action for delta in updated.agents} == {"removed"}
    assert not any(_agents_dir(tmp_path).iterdir())


def test_sync_plugins_retires_owned_agent_files_once_plugin_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugin(tmp_path)
    conflict = CCPluginConflict(plugin_dir=tmp_path / "eawf-plugin")
    monkeypatch.setattr(plugin_install, "detect_marketplace_install", lambda: conflict)

    sync_plugins(tmp_path, runtimes=("claude-code",))

    assert not any(_agents_dir(tmp_path).iterdir())


@pytest.mark.parametrize("upgraded", [False, True])
def test_doctor_reports_no_missing_agent_after_install_with_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upgraded: bool
) -> None:
    if upgraded:
        install_plugin(tmp_path)
    conflict = CCPluginConflict(plugin_dir=tmp_path / "eawf-plugin")
    monkeypatch.setattr(plugin_install, "detect_marketplace_install", lambda: conflict)
    install_plugin(tmp_path)

    report = doctor_plugin(tmp_path, marketplace_installed=True)

    assert report.missing == []
    assert not any(e.region_id.startswith("plugin.claude.agent.") for e in report.ok)


def test_doctor_still_expects_agents_without_plugin(tmp_path: Path) -> None:
    install_plugin(tmp_path)

    report = doctor_plugin(tmp_path)

    agent_ok = {e.region_id for e in report.ok if e.region_id.startswith("plugin.claude.agent.")}
    assert len(agent_ok) == len(AGENT_REGISTRY)


def test_install_plugin_dry_run_reports_no_agent_when_plugin_installed(
    tmp_path: Path, plugin_installed: CCPluginConflict
) -> None:
    result = install_plugin(tmp_path, dry_run=True, extra_tools={})

    assert result.agents == []
    assert result.dry_run
    assert not (tmp_path / ".claude").exists()
