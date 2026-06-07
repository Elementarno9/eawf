"""Unit tests for the Codex marketplace packager (P14-I02-W01 hotfix)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.runtime.runtimes.codex import PublishSource, package_plugin
from eawf.surfaces.render.hooks import HOOK_REGISTRY
from eawf.surfaces.render.skills import SKILL_REGISTRY


def test_package_writes_marketplace_and_plugin_tree(tmp_path: Path) -> None:
    target = tmp_path / "pkg"
    result = package_plugin(target)
    assert (target / ".agents" / "plugins" / "marketplace.json").is_file()
    assert (target / "plugins" / "eawf" / ".codex-plugin" / "plugin.json").is_file()
    assert (target / "plugins" / "eawf" / "skills").is_dir()
    assert (target / "plugins" / "eawf" / "hooks").is_dir()
    # Codex plugin.json has no top-level ``agents`` key — no agents/ dir.
    assert not (target / "plugins" / "eawf" / "agents").exists()
    assert result.marketplace is not None
    assert result.marketplace.action == "created"
    assert result.manifest is not None
    assert result.manifest.action == "created"
    assert len(result.skills) == len(SKILL_REGISTRY)
    assert len(result.hooks) == len(HOOK_REGISTRY)
    # Codex requires each skill on disk as a directory containing SKILL.md.
    plugin_root = target / "plugins" / "eawf"
    for spec in SKILL_REGISTRY:
        skill_dir = plugin_root / "skills" / spec.skill_name
        assert skill_dir.is_dir(), skill_dir
        assert (skill_dir / "SKILL.md").is_file(), skill_dir / "SKILL.md"


def test_marketplace_json_has_required_codex_schema_fields(tmp_path: Path) -> None:
    """Per Codex marketplace schema: name, interface.displayName, plugins[]
    with name/source/policy/category per plugin.

    The default ``package_plugin`` call keeps the ``local`` source so the
    dev ``codex plugin marketplace add ./path`` flow is unchanged — the
    published ``git-subdir`` form is opt-in via ``publish_source``.
    """
    target = tmp_path / "pkg"
    package_plugin(target)
    body = json.loads(
        (target / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert body["name"] == "eawf"
    assert body["interface"]["displayName"]
    assert isinstance(body["plugins"], list)
    plugin_entry = body["plugins"][0]
    assert plugin_entry["name"] == "eawf"
    assert plugin_entry["source"] == {"source": "local", "path": "./plugins/eawf"}
    assert plugin_entry["policy"]["installation"] in {"AVAILABLE", "INSTALLED_BY_DEFAULT"}
    assert plugin_entry["policy"]["authentication"] in {"ON_INSTALL", "ON_FIRST_USE"}
    assert plugin_entry["category"]


def test_marketplace_published_emits_git_subdir_source(tmp_path: Path) -> None:
    """``publish_source=GIT_SUBDIR`` emits the self-hosted pointer: a
    git-subdir source carrying url (from pyproject), path, and a moving ref."""
    target = tmp_path / "pkg"
    package_plugin(target, publish_source=PublishSource.GIT_SUBDIR)
    body = json.loads(
        (target / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    source = body["plugins"][0]["source"]
    assert source["source"] == "git-subdir"
    assert source["path"] == "./plugins/eawf"
    assert source["ref"] == "plugins-dist"
    # ref XOR sha — the packager pins ref (a branch tip), never both.
    assert "sha" not in source
    # url is pulled from pyproject [project.urls].Repository, never invented.
    assert source["url"].startswith("https://")
    assert source["url"].endswith("/eawf")


def test_marketplace_git_subdir_url_from_pyproject() -> None:
    """The git-subdir url resolves to the committed pyproject Repository URL."""
    from eawf.runtime.runtimes.codex.plugin_package import _eawf_repository_url

    assert _eawf_repository_url() == "https://github.com/Elementarno9/eawf"


def test_marketplace_published_pointer_has_no_pii(tmp_path: Path) -> None:
    """Published git-subdir pointer carries no machine path or email."""
    target = tmp_path / "pkg"
    package_plugin(target, publish_source=PublishSource.GIT_SUBDIR)
    serialised = (target / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    assert "@" not in serialised
    assert "/Users/" not in serialised  # pragma: allowlist secret


def test_plugin_manifest_matches_install_renderer(tmp_path: Path) -> None:
    """The packaged plugin.json must match what `plugin install codex`
    emits — same `_render_manifest` source."""
    target = tmp_path / "pkg"
    package_plugin(target)
    pkg_manifest = (target / "plugins" / "eawf" / ".codex-plugin" / "plugin.json").read_bytes()

    from eawf.runtime.runtimes.codex.plugin_install import _render_manifest

    assert pkg_manifest == _render_manifest()


def test_package_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "pkg"
    package_plugin(target)
    snap_mkt = (target / ".agents" / "plugins" / "marketplace.json").read_bytes()
    snap_man = (target / "plugins" / "eawf" / ".codex-plugin" / "plugin.json").read_bytes()
    result = package_plugin(target)
    assert (target / ".agents" / "plugins" / "marketplace.json").read_bytes() == snap_mkt
    assert (target / "plugins" / "eawf" / ".codex-plugin" / "plugin.json").read_bytes() == snap_man
    assert result.marketplace is not None
    assert result.marketplace.action == "unchanged"


def test_package_dry_run_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "pkg"
    result = package_plugin(target, dry_run=True)
    assert result.dry_run is True
    assert not target.exists()


def test_package_rejects_non_empty_unrelated_target(tmp_path: Path) -> None:
    target = tmp_path / "pkg"
    target.mkdir()
    (target / "unrelated.txt").write_text("hi\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a previous eawf package"):
        package_plugin(target)


def test_package_force_overwrites_unrelated_target(tmp_path: Path) -> None:
    target = tmp_path / "pkg"
    target.mkdir()
    (target / "unrelated.txt").write_text("hi\n", encoding="utf-8")
    result = package_plugin(target, force=True)
    assert (target / ".agents" / "plugins" / "marketplace.json").is_file()
    assert result.marketplace is not None


def test_package_accepts_previous_eawf_output_without_force(tmp_path: Path) -> None:
    target = tmp_path / "pkg"
    package_plugin(target)
    second = package_plugin(target)
    assert second.marketplace is not None
    assert second.marketplace.action == "unchanged"


def test_package_does_not_emit_root_marketplace_json(tmp_path: Path) -> None:
    """Codex CLI rejects ``<target>/marketplace.json``; manifest must live
    at ``<target>/.agents/plugins/marketplace.json``."""
    target = tmp_path / "pkg"
    package_plugin(target)
    assert not (target / "marketplace.json").exists()
    assert (target / ".agents" / "plugins" / "marketplace.json").is_file()


def test_package_strips_legacy_root_marketplace_on_rerun(tmp_path: Path) -> None:
    target = tmp_path / "pkg"
    package_plugin(target)
    legacy = target / "marketplace.json"
    legacy.write_text("{}\n", encoding="utf-8")
    package_plugin(target)
    assert not legacy.exists()


def test_package_hooks_files_executable(tmp_path: Path) -> None:
    target = tmp_path / "pkg"
    package_plugin(target)
    hooks_dir = target / "plugins" / "eawf" / "hooks"
    for hook_file in hooks_dir.glob("*.sh"):
        mode = hook_file.stat().st_mode & 0o777
        assert mode & 0o111, f"{hook_file} not executable: {oct(mode)}"
