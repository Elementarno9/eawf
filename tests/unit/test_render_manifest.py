"""Unit tests for ``eawf.surfaces.render.manifest``.

Covers ``Manifest`` Pydantic schema, ``load`` / ``save_atomic`` round-trip,
``extra="forbid"`` rejection, deterministic byte-stable JSON, and the
tempfile + ``os.replace`` discipline of ``save_atomic``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from eawf.surfaces.render import manifest as manifest_mod
from eawf.surfaces.render.manifest import Manifest, ManifestEntry


def _entry(target: str = "AGENTS.md", region_id: str = "rules") -> ManifestEntry:
    return ManifestEntry(
        target=target,
        region_id=region_id,
        version="1.0",
        hash="0123456789abcdef",
        generator="profile:core",
        generated_at="2026-01-01T00:00:00+00:00",
    )


def test_manifest_load_returns_empty_when_absent(tmp_path: Path) -> None:
    target = tmp_path / "missing" / "generated.json"
    m = manifest_mod.load(target)
    assert isinstance(m, Manifest)
    assert m.generated == {}
    assert m.version == 1


def test_manifest_save_then_load_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "indexes" / "generated.json"
    e = _entry()
    m = Manifest(version=1, generated={"AGENTS.md::rules": e})
    manifest_mod.save_atomic(target, m)

    loaded = manifest_mod.load(target)
    assert loaded == m
    assert loaded.generated["AGENTS.md::rules"].region_id == "rules"


def test_manifest_save_byte_stable(tmp_path: Path) -> None:
    """Two saves of the same manifest must produce identical bytes."""
    target = tmp_path / "generated.json"
    m = Manifest(
        version=1,
        generated={
            "b::two": _entry("b", "two"),
            "a::one": _entry("a", "one"),
        },
    )
    manifest_mod.save_atomic(target, m)
    bytes_first = target.read_bytes()
    target.unlink()
    manifest_mod.save_atomic(target, m)
    bytes_second = target.read_bytes()
    assert bytes_first == bytes_second
    # also: keys are sorted in the on-disk JSON
    parsed = json.loads(bytes_first)
    keys = list(parsed["generated"].keys())
    assert keys == sorted(keys)


def test_manifest_save_atomic_uses_tempfile(tmp_path: Path) -> None:
    """save_atomic should write to a sibling tempfile then os.replace."""
    target = tmp_path / "generated.json"
    m = Manifest(version=1, generated={"AGENTS.md::rules": _entry()})

    with patch("eawf.surfaces.render.manifest.os.replace", wraps=__import__("os").replace) as spy:
        manifest_mod.save_atomic(target, m)

    assert spy.called
    # the source argument to os.replace must be a sibling tempfile, not the target itself
    src_arg = spy.call_args.args[0]
    dst_arg = spy.call_args.args[1]
    assert str(dst_arg) == str(target)
    assert str(src_arg).startswith(str(target) + ".tmp.")


def test_manifest_extra_field_rejected() -> None:
    """extra='forbid' must reject unknown fields on Manifest."""
    with pytest.raises(ValidationError):
        Manifest(version=1, generated={}, extra_key="nope")  # type: ignore[call-arg]


def test_manifest_entry_extra_field_rejected() -> None:
    """extra='forbid' on ManifestEntry."""
    with pytest.raises(ValidationError):
        ManifestEntry(  # type: ignore[call-arg]
            target="t",
            region_id="r",
            version="1.0",
            hash="0123456789abcdef",
            generator="g",
            generated_at="2026-01-01T00:00:00+00:00",
            unknown=True,
        )


def test_manifest_save_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "generated.json"
    m = Manifest(version=1, generated={})
    manifest_mod.save_atomic(target, m)
    assert target.exists()


def test_manifest_load_invalid_json_raises(tmp_path: Path) -> None:
    target = tmp_path / "generated.json"
    target.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError):
        manifest_mod.load(target)


def test_manifest_load_validates_schema(tmp_path: Path) -> None:
    """load() must validate via Pydantic — extra keys rejected on disk."""
    target = tmp_path / "generated.json"
    target.write_text(
        json.dumps({"version": 1, "generated": {}, "stowaway": "boo"}),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        manifest_mod.load(target)
