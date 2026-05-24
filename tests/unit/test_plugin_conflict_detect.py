"""Unit tests for :mod:`eawf.runtime.runtimes.claude.plugin_conflict`."""

from __future__ import annotations

from pathlib import Path

from eawf.runtime.runtimes.claude.plugin_conflict import (
    CCPluginConflict,
    detect_marketplace_install,
)


def _make_home(tmp_path: Path, *plugin_names: str) -> Path:
    plugins = tmp_path / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    for name in plugin_names:
        (plugins / name).mkdir()
    return tmp_path


def test_returns_none_when_plugin_root_absent(tmp_path: Path) -> None:
    assert detect_marketplace_install(home=tmp_path) is None


def test_returns_none_when_no_eawf_named_entry(tmp_path: Path) -> None:
    _make_home(tmp_path, "other-plugin", "unrelated")
    assert detect_marketplace_install(home=tmp_path) is None


def test_returns_conflict_on_eawf_named_dir(tmp_path: Path) -> None:
    home = _make_home(tmp_path, "eawf-local")
    result = detect_marketplace_install(home=home)
    assert isinstance(result, CCPluginConflict)
    assert result.plugin_dir == home / ".claude" / "plugins" / "eawf-local"


def test_returns_conflict_case_insensitive(tmp_path: Path) -> None:
    home = _make_home(tmp_path, "EAWF-Marketplace")
    result = detect_marketplace_install(home=home)
    assert result is not None
    assert result.plugin_dir.name == "EAWF-Marketplace"


def test_ignores_files_in_plugins_root(tmp_path: Path) -> None:
    _make_home(tmp_path)
    (tmp_path / ".claude" / "plugins" / "eawf-notes.md").write_text("not a plugin")
    assert detect_marketplace_install(home=tmp_path) is None


def test_returns_first_match_deterministically(tmp_path: Path) -> None:
    home = _make_home(tmp_path, "eawf-z", "eawf-a", "other")
    result = detect_marketplace_install(home=home)
    assert result is not None
    assert result.plugin_dir.name == "eawf-a"
