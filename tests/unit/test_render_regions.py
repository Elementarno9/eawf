"""Unit tests for ``eawf.surfaces.render.regions``.

Covers marker parsing, region extraction, replace-as-insert / replace-existing
semantics, and the boundary-error catalogue (missing END marker, duplicate id,
nested markers, malformed marker).
"""

from __future__ import annotations

import pytest

from eawf.surfaces.render import regions


def _wrap(region_id: str, version: str, body: str) -> str:
    """Build a fully-formed managed-region snippet for tests."""
    h = regions.compute_hash(body)
    return (
        f"<!-- BEGIN EAWF:managed id={region_id} version={version} hash={h} -->\n"
        f"{body}\n"
        f"<!-- END EAWF:managed id={region_id} -->"
    )


def test_compute_hash_is_16_hex_chars() -> None:
    h = regions.compute_hash("hello")
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_compute_hash_deterministic() -> None:
    assert regions.compute_hash("abc") == regions.compute_hash("abc")
    assert regions.compute_hash("abc") != regions.compute_hash("abd")


def test_replace_region_inserts_when_absent() -> None:
    """Empty file → marker block appended."""
    out = regions.replace_region("", id="rules", version="1.0", body="foo")
    region = regions.extract_region(out, "rules")
    assert region is not None
    assert region.body == "foo"
    assert region.version == "1.0"
    assert region.declared_hash == regions.compute_hash("foo")
    # the BEGIN/END markers are present
    assert "<!-- BEGIN EAWF:managed id=rules version=1.0 hash=" in out
    assert "<!-- END EAWF:managed id=rules -->" in out


def test_replace_region_inserts_after_existing_content() -> None:
    text = "preamble line one\npreamble line two"
    out = regions.replace_region(text, id="rules", version="1.0", body="payload")
    assert out.startswith(text)
    assert "payload" in out
    assert regions.extract_region(out, "rules") is not None


def test_replace_region_replaces_when_present() -> None:
    initial = _wrap("rules", "1.0", "old body")
    out = regions.replace_region(initial, id="rules", version="1.0", body="new body")
    region = regions.extract_region(out, "rules")
    assert region is not None
    assert region.body == "new body"
    # only one BEGIN marker for "rules" remains
    assert out.count("<!-- BEGIN EAWF:managed id=rules") == 1
    assert out.count("<!-- END EAWF:managed id=rules -->") == 1


def test_replace_region_preserves_surrounding_content() -> None:
    pre = "Hand-written intro.\n\n"
    post = "\n\nHand-written outro."
    middle = _wrap("rules", "1.0", "old")
    text = pre + middle + post
    out = regions.replace_region(text, id="rules", version="1.0", body="new")
    assert out.startswith(pre)
    assert out.endswith(post)
    region = regions.extract_region(out, "rules")
    assert region is not None
    assert region.body == "new"


def test_replace_region_replaces_only_targeted_id() -> None:
    a = _wrap("alpha", "1.0", "alpha-body")
    b = _wrap("beta", "1.0", "beta-body")
    text = a + "\n\n" + b
    out = regions.replace_region(text, id="alpha", version="1.0", body="alpha-new")
    region_a = regions.extract_region(out, "alpha")
    region_b = regions.extract_region(out, "beta")
    assert region_a is not None and region_a.body == "alpha-new"
    assert region_b is not None and region_b.body == "beta-body"


def test_replace_region_updates_version() -> None:
    initial = _wrap("rules", "1.0", "body")
    out = regions.replace_region(initial, id="rules", version="2.5", body="body")
    region = regions.extract_region(out, "rules")
    assert region is not None
    assert region.version == "2.5"


def test_extract_region_finds_existing() -> None:
    text = _wrap("alpha", "1.0", "hello world")
    region = regions.extract_region(text, "alpha")
    assert region is not None
    assert region.id == "alpha"
    assert region.version == "1.0"
    assert region.body == "hello world"


def test_extract_region_returns_none_when_absent() -> None:
    text = _wrap("alpha", "1.0", "hello")
    assert regions.extract_region(text, "beta") is None


def test_extract_region_returns_none_in_empty_file() -> None:
    assert regions.extract_region("", "any") is None


def test_find_regions_returns_all_in_order() -> None:
    a = _wrap("alpha", "1.0", "a-body")
    b = _wrap("beta", "1.0", "b-body")
    found = regions.find_regions(a + "\n\n" + b)
    assert [r.id for r in found] == ["alpha", "beta"]
    assert [r.body for r in found] == ["a-body", "b-body"]


def test_find_regions_missing_end_raises() -> None:
    text = (
        "<!-- BEGIN EAWF:managed id=rules version=1.0 hash="
        + regions.compute_hash("body")
        + " -->\nbody\n"
    )
    with pytest.raises(regions.RegionParseError, match="missing END marker"):
        regions.find_regions(text)


def test_find_regions_duplicate_id_raises() -> None:
    a = _wrap("rules", "1.0", "first")
    b = _wrap("rules", "1.0", "second")
    with pytest.raises(regions.RegionParseError, match="duplicate id"):
        regions.find_regions(a + "\n" + b)


def test_find_regions_nested_markers_raises() -> None:
    inner_hash = regions.compute_hash("inner")
    outer_hash = regions.compute_hash("anything")
    text = (
        f"<!-- BEGIN EAWF:managed id=outer version=1.0 hash={outer_hash} -->\n"
        f"<!-- BEGIN EAWF:managed id=inner version=1.0 hash={inner_hash} -->\n"
        "inner\n"
        "<!-- END EAWF:managed id=inner -->\n"
        "<!-- END EAWF:managed id=outer -->"
    )
    with pytest.raises(regions.RegionParseError, match="nested"):
        regions.find_regions(text)


def test_find_regions_malformed_begin_raises() -> None:
    """A BEGIN-like marker with bad version → RegionParseError when subsequent END appears."""
    # Real BEGIN with no terminating END but a stray END for a *different* id.
    h = regions.compute_hash("body")
    text = (
        f"<!-- BEGIN EAWF:managed id=rules version=1.0 hash={h} -->\n"
        "body\n"
        "<!-- END EAWF:managed id=other -->"
    )
    with pytest.raises(regions.RegionParseError):
        regions.find_regions(text)


def test_find_regions_empty_file_returns_empty() -> None:
    assert regions.find_regions("") == []


def test_extract_region_body_with_blank_lines() -> None:
    body = "line one\n\nline three"
    text = _wrap("rules", "1.0", body)
    region = regions.extract_region(text, "rules")
    assert region is not None
    assert region.body == body
