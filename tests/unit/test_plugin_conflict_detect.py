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
    home = _make_home(tmp_path, "eawf")
    result = detect_marketplace_install(home=home)
    assert isinstance(result, CCPluginConflict)
    assert result.plugin_dir == home / ".claude" / "plugins" / "eawf"


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


# --- real Claude Code layout ------------------------------------------------


def _make_nested_home(tmp_path: Path, *, manifest: str | None = None) -> Path:
    """Build the layout Claude Code actually writes.

    The immediate children of ``plugins/`` are ``cache`` / ``data`` /
    ``marketplaces`` / ``npm-cache``; the plugin tree lives one level deeper.
    """
    plugins = tmp_path / ".claude" / "plugins"
    for name in ("cache", "data", "marketplaces", "npm-cache"):
        (plugins / name).mkdir(parents=True, exist_ok=True)
    (plugins / "cache" / "eawf" / "eawf" / "0.6.7").mkdir(parents=True, exist_ok=True)
    (plugins / "marketplaces" / "eawf").mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (plugins / "installed_plugins.json").write_text(manifest, encoding="utf-8")
    return tmp_path


def test_detects_install_nested_under_cache(tmp_path: Path) -> None:
    """The real layout is found even with no manifest to read.

    The original detector walked only the immediate children of ``plugins/``,
    none of which is named for a plugin, so it returned None against a live
    install and the conflict gate never fired.
    """
    home = _make_nested_home(tmp_path)
    conflict = detect_marketplace_install(home=home)
    assert conflict is not None
    assert "eawf" in str(conflict.plugin_dir)


def test_detects_install_from_manifest(tmp_path: Path) -> None:
    """``installed_plugins.json`` is authoritative and names the install path."""
    manifest = (
        '{"version": 1, "plugins": {"eawf@eawf": '
        '[{"scope": "user", "installPath": "/somewhere/eawf/0.6.7", "version": "0.6.7"}]}}'
    )
    home = _make_nested_home(tmp_path, manifest=manifest)
    conflict = detect_marketplace_install(home=home)
    assert conflict is not None
    assert str(conflict.plugin_dir) == "/somewhere/eawf/0.6.7"


def test_manifest_without_eawf_falls_back_to_directories(tmp_path: Path) -> None:
    """A manifest naming other plugins does not mask an on-disk eawf tree."""
    manifest = '{"version": 1, "plugins": {"other@other": [{"installPath": "/x"}]}}'
    home = _make_nested_home(tmp_path, manifest=manifest)
    assert detect_marketplace_install(home=home) is not None


def test_unreadable_manifest_falls_back_to_directories(tmp_path: Path) -> None:
    """Malformed JSON degrades to the directory walk rather than raising."""
    home = _make_nested_home(tmp_path, manifest="{not json")
    assert detect_marketplace_install(home=home) is not None


def test_returns_none_when_nested_roots_hold_no_eawf(tmp_path: Path) -> None:
    """Boundary: the nested roots exist but hold only unrelated plugins."""
    plugins = tmp_path / ".claude" / "plugins"
    (plugins / "cache" / "caveman").mkdir(parents=True)
    (plugins / "marketplaces" / "other").mkdir(parents=True)
    assert detect_marketplace_install(home=tmp_path) is None
