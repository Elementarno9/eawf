"""The Claude installer wires each hook and the statusline from one settings source.

Claude Code runs the hooks of an enabled plugin's ``hooks.json`` beside the
project ``settings.json`` hooks, so a project entry next to the enabled eawf
plugin fires every handler twice. A project ``statusLine`` overrides the
user's own, so the installer writes none and keeps one it finds.

Every test runs against a temporary ``HOME`` and the real registry detector,
so nothing reads the operator's own Claude files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.runtimes.claude import plugin_conflict, plugin_install, statusline_install
from eawf.runtime.runtimes.claude.hook_map import build_plugin_hooks_json
from eawf.runtime.runtimes.claude.plugin_install import install_plugin

_PROJECT_MARKER = "/.claude/hooks/"
_PLUGIN_MARKER = "${CLAUDE_PLUGIN_ROOT}/hooks/"
_USER_STATUSLINE = {"type": "command", "command": "my-own-statusline"}


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


def _user_settings_path(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _register_plugin(home: Path) -> None:
    """Record the eawf plugin in the user's Claude Code plugin registry."""
    _write_json(
        home / ".claude" / "plugins" / "installed_plugins.json",
        {
            "version": 2,
            "plugins": {"eawf@eawf": [{"scope": "user", "installPath": str(home / "eawf-plugin")}]},
        },
    )


def _set_user_settings(home: Path, settings: dict[str, Any]) -> None:
    _write_json(_user_settings_path(home), settings)


def _project_settings(project: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (project / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    return loaded


def _commands(hooks_block: Any, event: str, marker: str) -> list[str]:
    """Return the commands under *event* in *hooks_block* containing *marker*."""
    entries = hooks_block.get(event, []) if isinstance(hooks_block, dict) else []
    return [
        str(command["command"])
        for entry in entries
        for command in entry.get("hooks", [])
        if marker in str(command.get("command", ""))
    ]


def _union_counts(project: Path, *, plugin_active: bool) -> dict[str, int]:
    """Return per event the eawf hook commands across the plugin and project sources."""
    plugin_hooks = build_plugin_hooks_json()["hooks"] if plugin_active else {}
    project_hooks = _project_settings(project).get("hooks", {})
    events = set(build_plugin_hooks_json()["hooks"]) | set(project_hooks)
    return {
        event: len(_commands(plugin_hooks, event, _PLUGIN_MARKER))
        + len(_commands(project_hooks, event, _PROJECT_MARKER))
        for event in events
    }


def test_one_hook_source(home: Path, project: Path) -> None:
    events = set(build_plugin_hooks_json()["hooks"])
    assert len(events) == 10

    install_plugin(project, extra_tools={})
    project_hooks = _project_settings(project)["hooks"]
    assert {event: len(_commands(project_hooks, event, _PROJECT_MARKER)) for event in events} == (
        dict.fromkeys(events, 1)
    )
    assert _union_counts(project, plugin_active=False) == dict.fromkeys(events, 1)

    _register_plugin(home)
    _set_user_settings(home, {"enabledPlugins": {"eawf@eawf": True}})
    install_plugin(project, extra_tools={})

    settings = _project_settings(project)
    assert _PROJECT_MARKER not in json.dumps(settings.get("hooks", {}))
    assert "hooks" not in settings
    assert _union_counts(project, plugin_active=True) == dict.fromkeys(events, 1)


def test_plugin_hooks_removal_keeps_a_foreign_hook(home: Path, project: Path) -> None:
    install_plugin(project, extra_tools={})
    settings = _project_settings(project)
    foreign = {"matcher": "", "hooks": [{"type": "command", "command": "my-linter"}]}
    settings["hooks"]["PreToolUse"].append(foreign)
    _write_json(project / ".claude" / "settings.json", settings)
    _register_plugin(home)
    _set_user_settings(home, {"enabledPlugins": {"eawf@eawf": True}})

    install_plugin(project, extra_tools={})

    assert _project_settings(project)["hooks"] == {"PreToolUse": [foreign]}


@pytest.mark.parametrize(
    ("user_flags", "project_flags"),
    [
        pytest.param(None, None, id="registered-never-enabled"),
        pytest.param({"eawf@eawf": False}, None, id="user-disabled"),
        pytest.param({"eawf@eawf": True}, {"eawf@eawf": False}, id="project-disables"),
        pytest.param({"eawf@eawf": "true"}, None, id="string-not-bool"),
        pytest.param({"eawf-extra@eawf": True}, None, id="other-plugin-name"),
        pytest.param(["eawf@eawf"], None, id="flags-not-a-mapping"),
    ],
)
def test_project_hooks_stay_while_plugin_is_not_enabled(
    home: Path, project: Path, user_flags: Any, project_flags: Any
) -> None:
    _register_plugin(home)
    if user_flags is not None:
        _set_user_settings(home, {"enabledPlugins": user_flags})
    if project_flags is not None:
        _write_json(project / ".claude" / "settings.json", {"enabledPlugins": project_flags})

    install_plugin(project, extra_tools={})

    events = set(build_plugin_hooks_json()["hooks"])
    hooks = _project_settings(project)["hooks"]
    assert {event: len(_commands(hooks, event, _PROJECT_MARKER)) for event in events} == (
        dict.fromkeys(events, 1)
    )


def test_project_hooks_stay_while_plugin_is_enabled_but_not_registered(
    home: Path, project: Path
) -> None:
    _set_user_settings(home, {"enabledPlugins": {"eawf@eawf": True}})

    install_plugin(project, extra_tools={})

    assert len(_project_settings(project)["hooks"]) == 10


def test_project_enabled_flag_alone_switches_hooks_to_the_plugin(home: Path, project: Path) -> None:
    _register_plugin(home)
    _write_json(project / ".claude" / "settings.json", {"enabledPlugins": {"eawf@eawf": True}})

    install_plugin(project, extra_tools={})

    assert "hooks" not in _project_settings(project)


@pytest.mark.parametrize(
    ("user", "project_settings", "expected"),
    [
        pytest.param(None, None, None, id="neither-sets-it"),
        pytest.param({}, {}, None, id="both-empty"),
        pytest.param({"statusLine": _USER_STATUSLINE}, None, None, id="user-sets-it"),
        pytest.param(None, {"statusLine": _USER_STATUSLINE}, _USER_STATUSLINE, id="project"),
        pytest.param(
            {"statusLine": _USER_STATUSLINE},
            {"statusLine": {"type": "command", "command": "project-line"}},
            {"type": "command", "command": "project-line"},
            id="both-set-it",
        ),
    ],
)
def test_statusline_respects_user_file(
    home: Path,
    project: Path,
    user: dict[str, Any] | None,
    project_settings: dict[str, Any] | None,
    expected: dict[str, str] | None,
) -> None:
    if user is not None:
        _set_user_settings(home, user)
    if project_settings is not None:
        _write_json(project / ".claude" / "settings.json", project_settings)
    user_before = (
        _user_settings_path(home).read_bytes() if _user_settings_path(home).exists() else None
    )

    install_plugin(project, extra_tools={})
    install_plugin(project, extra_tools={})

    assert _project_settings(project).get("statusLine") == expected
    user_after = (
        _user_settings_path(home).read_bytes() if _user_settings_path(home).exists() else None
    )
    assert user_after == user_before


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("{not json", id="invalid-json"),
        pytest.param("[1, 2]", id="not-an-object"),
    ],
)
def test_unreadable_user_settings_refuse_the_install(home: Path, project: Path, raw: str) -> None:
    path = _user_settings_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(ValueError, match="settings"):
        install_plugin(project, extra_tools={})

    assert not (project / ".claude" / "settings.json").exists()


def test_blank_user_settings_read_as_empty(home: Path, project: Path) -> None:
    path = _user_settings_path(home)
    path.parent.mkdir(parents=True)
    path.write_text("  \n", encoding="utf-8")

    install_plugin(project, extra_tools={})

    assert "statusLine" not in _project_settings(project)
