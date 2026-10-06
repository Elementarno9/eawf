"""Plugin manifest scope and the cross-scope duplicate detector.

1. :class:`eawf.surfaces.render.manifest.ManifestEntry` carries an optional
   ``scope`` field that survives the load/save round-trip.
2. :func:`eawf.surfaces.cli.commands.plugin.detect_cross_scope_duplicates`
   flags region_ids installed under both ``project`` and ``user`` scope,
   ignoring entries with ``scope=None``.
"""

from __future__ import annotations

from pathlib import Path

from eawf.surfaces.cli.commands.plugin import detect_cross_scope_duplicates
from eawf.surfaces.render.manifest import Manifest, ManifestEntry
from eawf.surfaces.render.manifest import load as load_manifest
from eawf.surfaces.render.manifest import save_atomic as save_manifest_atomic

# ---- ManifestEntry.scope ----------------------------------------------------


def _entry(
    target: str = "p.js",
    region_id: str = "plugin.codex.skill.flow",
    scope: str | None = None,
) -> ManifestEntry:
    return ManifestEntry(
        target=target,
        region_id=region_id,
        version="1.0",
        hash="0123456789abcdef",
        generator="eawf-plugin-codex",
        generated_at="2026-05-27T00:00:00+00:00",
        scope=scope,
    )


def test_manifest_entry_scope_defaults_to_none() -> None:
    entry = _entry()
    assert entry.scope is None


def test_manifest_entry_scope_roundtrip_project(tmp_path: Path) -> None:
    target = tmp_path / "indexes" / "generated.json"
    entry = _entry(scope="project")
    manifest = Manifest(version=1, generated={"k::project": entry})
    save_manifest_atomic(target, manifest)
    loaded = load_manifest(target)
    assert loaded.generated["k::project"].scope == "project"


def test_manifest_entry_scope_roundtrip_user(tmp_path: Path) -> None:
    target = tmp_path / "indexes" / "generated.json"
    entry = _entry(scope="user")
    manifest = Manifest(version=1, generated={"k::user": entry})
    save_manifest_atomic(target, manifest)
    loaded = load_manifest(target)
    assert loaded.generated["k::user"].scope == "user"


# ---- detect_cross_scope_duplicates -----------------------------------------


def test_cross_scope_duplicates_empty_when_no_manifest(tmp_path: Path) -> None:
    assert detect_cross_scope_duplicates(tmp_path) == []


def test_cross_scope_duplicates_ignores_legacy_none_scope(tmp_path: Path) -> None:
    """Pre-P28 entries with scope=None never count as duplicates."""
    target = tmp_path / ".ea" / "indexes" / "generated.json"
    target.parent.mkdir(parents=True)
    manifest = Manifest(
        version=1,
        generated={
            "a::r": _entry(target="a", region_id="r", scope=None),
            "b::r": _entry(target="b", region_id="r", scope=None),
        },
    )
    save_manifest_atomic(target, manifest)
    assert detect_cross_scope_duplicates(tmp_path) == []


def test_cross_scope_duplicates_flags_project_plus_user(tmp_path: Path) -> None:
    target = tmp_path / ".ea" / "indexes" / "generated.json"
    target.parent.mkdir(parents=True)
    manifest = Manifest(
        version=1,
        generated={
            "a::skill.x": _entry(target="a", region_id="plugin.codex.skill.x", scope="project"),
            "b::skill.x": _entry(target="b", region_id="plugin.codex.skill.x", scope="user"),
            "a::skill.y": _entry(target="a", region_id="plugin.codex.skill.y", scope="project"),
        },
    )
    save_manifest_atomic(target, manifest)
    duplicates = detect_cross_scope_duplicates(tmp_path)
    assert duplicates == ["plugin.codex.skill.x"]


def test_cross_scope_duplicates_unreadable_manifest_returns_empty(tmp_path: Path) -> None:
    target = tmp_path / ".ea" / "indexes" / "generated.json"
    target.parent.mkdir(parents=True)
    target.write_text("{not json", encoding="utf-8")
    assert detect_cross_scope_duplicates(tmp_path) == []
