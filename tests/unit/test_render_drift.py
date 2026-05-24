"""Unit tests for ``eawf.surfaces.render.drift``.

Hand-edit detection vs untouched-region negative + missing-region detection +
unrelated-target filter.
"""

from __future__ import annotations

from pathlib import Path

from eawf.surfaces.render import regions
from eawf.surfaces.render.drift import detect_drift
from eawf.surfaces.render.manifest import Manifest, ManifestEntry


def _wrap(region_id: str, version: str, body: str) -> str:
    h = regions.compute_hash(body)
    return (
        f"<!-- BEGIN EAWF:managed id={region_id} version={version} hash={h} -->\n"
        f"{body}\n"
        f"<!-- END EAWF:managed id={region_id} -->"
    )


def _make_manifest(target_path: Path, region_id: str, body: str) -> Manifest:
    target_key = target_path.as_posix()
    return Manifest(
        version=1,
        generated={
            f"{target_key}::{region_id}": ManifestEntry(
                target=target_key,
                region_id=region_id,
                version="1.0",
                hash=regions.compute_hash(body),
                generator="profile:core",
                generated_at="2026-01-01T00:00:00+00:00",
            ),
        },
    )


def test_drift_clean_when_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    body = "rendered text"
    target.write_text(_wrap("rules", "1.0", body), encoding="utf-8")
    m = _make_manifest(target, "rules", body)

    reports = detect_drift(target, m)
    assert all(r.kind == "ok" for r in reports)
    assert {r.id for r in reports} == {"rules"}


def test_drift_detects_hand_edit(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    expected_body = "rendered"
    # On-disk body has been hand-edited but the BEGIN marker still claims the
    # *old* declared_hash (this is the hand-edit signature).
    declared_hash = regions.compute_hash(expected_body)
    on_disk = (
        f"<!-- BEGIN EAWF:managed id=rules version=1.0 hash={declared_hash} -->\n"
        "tampered body\n"
        "<!-- END EAWF:managed id=rules -->"
    )
    target.write_text(on_disk, encoding="utf-8")
    m = _make_manifest(target, "rules", expected_body)

    reports = detect_drift(target, m)
    hand = [r for r in reports if r.kind == "hand-edited"]
    assert len(hand) == 1
    assert hand[0].id == "rules"
    assert hand[0].manifest_hash == declared_hash
    assert hand[0].on_disk_hash == regions.compute_hash("tampered body")


def test_drift_detects_missing_region(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("user removed the marker block entirely", encoding="utf-8")
    m = _make_manifest(target, "rules", "rendered")

    reports = detect_drift(target, m)
    missing = [r for r in reports if r.kind == "missing"]
    assert len(missing) == 1
    assert missing[0].id == "rules"
    assert missing[0].on_disk_hash is None


def test_drift_ignores_unrelated_targets(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    other = tmp_path / "OTHER.md"
    target.write_text(_wrap("rules", "1.0", "x"), encoding="utf-8")

    # Manifest contains an entry for OTHER.md that should NOT show up when
    # we ask for drift on AGENTS.md.
    target_key = target.as_posix()
    other_key = other.as_posix()
    m = Manifest(
        version=1,
        generated={
            f"{target_key}::rules": ManifestEntry(
                target=target_key,
                region_id="rules",
                version="1.0",
                hash=regions.compute_hash("x"),
                generator="g",
                generated_at="2026-01-01T00:00:00+00:00",
            ),
            f"{other_key}::stuff": ManifestEntry(
                target=other_key,
                region_id="stuff",
                version="1.0",
                hash="ffffffffffffffff",
                generator="g",
                generated_at="2026-01-01T00:00:00+00:00",
            ),
        },
    )

    reports = detect_drift(target, m)
    assert {r.id for r in reports} == {"rules"}


def test_drift_when_target_file_absent(tmp_path: Path) -> None:
    """If the target file does not exist, every manifest entry is 'missing'."""
    target = tmp_path / "missing.md"
    m = _make_manifest(target, "rules", "body")
    reports = detect_drift(target, m)
    missing = [r for r in reports if r.kind == "missing"]
    assert len(missing) == 1
    assert missing[0].on_disk_hash is None
