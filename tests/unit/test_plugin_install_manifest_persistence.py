"""Tests that codex + opencode installers persist to ``.ea/indexes/generated.json``.

Confirms the P28-I02-W01 wiring: each runtime's ``install_plugin``
appends per-region :class:`~eawf.surfaces.render.manifest.ManifestEntry`
rows to the shared manifest at ``<target>/.ea/indexes/generated.json``,
each carrying ``scope`` populated to the install scope. The shared
manifest is the cross-runtime drift-reconciliation store consumed by
``eawf doctor``.
"""

from __future__ import annotations

from pathlib import Path

from eawf.runtime.runtimes.codex.plugin_install import install_plugin as codex_install_plugin
from eawf.runtime.runtimes.opencode.plugin_install import (
    install_plugin as opencode_install_plugin,
)
from eawf.surfaces.render.manifest import load as load_manifest


def test_codex_install_persists_manifest_with_scope(tmp_path: Path) -> None:
    codex_install_plugin(tmp_path, scope="project")
    manifest_path = tmp_path / ".ea" / "indexes" / "generated.json"
    assert manifest_path.exists()
    manifest = load_manifest(manifest_path)
    # Expect at least the codex manifest + sidecar + config entries +
    # one row per skill / hook.
    codex_entries = [
        e for e in manifest.generated.values() if e.region_id.startswith("plugin.codex.")
    ]
    assert codex_entries, "no codex entries in manifest"
    # Every codex entry must carry scope=project.
    assert all(e.scope == "project" for e in codex_entries)


def test_opencode_install_persists_manifest_with_scope(tmp_path: Path) -> None:
    opencode_install_plugin(
        tmp_path,
        scope="user",
        home=tmp_path / "home",
        opencode_config_dir=str(tmp_path / "oc-config"),
    )
    manifest_path = tmp_path / ".ea" / "indexes" / "generated.json"
    assert manifest_path.exists()
    manifest = load_manifest(manifest_path)
    opencode_entries = [
        e for e in manifest.generated.values() if e.region_id.startswith("plugin.opencode.")
    ]
    assert opencode_entries, "no opencode entries in manifest"
    assert all(e.scope == "user" for e in opencode_entries)
    # Plugin.js + sidecar + config are the three baseline rows.
    region_ids = {e.region_id for e in opencode_entries}
    assert "plugin.opencode.plugin_js" in region_ids
    assert "plugin.opencode.sidecar" in region_ids
    assert "plugin.opencode.config" in region_ids


def test_codex_install_then_user_scope_install_keeps_both(tmp_path: Path) -> None:
    """Installing under both scopes preserves rows for each scope independently.

    The cross-scope-dup detector reads from this shape: same region_id
    under scope=project AND scope=user.
    """
    codex_install_plugin(tmp_path, scope="project")
    codex_install_plugin(tmp_path, scope="user", home=tmp_path / "home")
    manifest = load_manifest(tmp_path / ".ea" / "indexes" / "generated.json")
    project_skills = {
        e.region_id
        for e in manifest.generated.values()
        if e.region_id.startswith("plugin.codex.skill.") and e.scope == "project"
    }
    user_skills = {
        e.region_id
        for e in manifest.generated.values()
        if e.region_id.startswith("plugin.codex.skill.") and e.scope == "user"
    }
    # Both scopes carry overlapping skill region_ids → the
    # cross-scope-dup detector will surface them.
    overlap = project_skills & user_skills
    assert overlap, f"expected overlapping skill region_ids, got {project_skills=} {user_skills=}"


def test_codex_install_dry_run_skips_manifest_write(tmp_path: Path) -> None:
    """A dry_run install must not create the shared manifest file."""
    codex_install_plugin(tmp_path, scope="project", dry_run=True)
    manifest_path = tmp_path / ".ea" / "indexes" / "generated.json"
    assert not manifest_path.exists()
