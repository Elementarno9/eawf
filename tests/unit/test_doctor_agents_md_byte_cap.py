"""Tests for the AGENTS.md byte-cap doctor diagnostic.

Two layers are exercised:

- The pure measurement helpers in
  :mod:`eawf.surfaces.render.agents_md` (:func:`block_byte_spans`,
  :func:`measure_agents_md_byte_cap`) — boundary + error paths.
- The blocking doctor check
  :func:`eawf.observability.doctor.checks.check_agents_md_byte_cap` — a
  small doc passes; an over-cap doc fails, naming the dropped render blocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.observability.doctor import checks
from eawf.surfaces.render import regions
from eawf.surfaces.render.agents_md import (
    block_byte_spans,
    measure_agents_md_byte_cap,
)
from eawf.surfaces.render.regions import RegionParseError


def _doc_with_blocks(*ids: str, body: str = "line one\nline two") -> str:
    """Build an AGENTS.md-shaped doc with one managed region per id.

    Each block is emitted through :func:`regions.replace_region` so the BEGIN
    marker carries a correctly computed hash and the document parses cleanly.
    """
    text = ""
    for block_id in ids:
        text = regions.replace_region(text, id=block_id, version="1.0", body=body)
    return text


# ---- pure helpers: measure_agents_md_byte_cap / block_byte_spans -----------


def test_measure_byte_cap_empty_doc() -> None:
    """An empty document is zero bytes with no blocks past any positive cap."""
    report = measure_agents_md_byte_cap("", cap=100)
    assert report.total_bytes == 0
    assert report.dropped_block_ids == []
    assert not report.over_cap


def test_measure_byte_cap_plain_text_no_regions() -> None:
    """Text with no managed regions measures its byte total and drops nothing."""
    text = "just prose, no markers\n"
    report = measure_agents_md_byte_cap(text, cap=8)
    assert report.total_bytes == len(text.encode("utf-8"))
    assert report.dropped_block_ids == []
    # Over the cap by total bytes even though no *block* is dropped.
    assert report.over_cap


def test_measure_byte_cap_names_blocks_past_cut() -> None:
    """The report names exactly the blocks whose BEGIN starts at/after the cap."""
    doc = _doc_with_blocks("alpha", "beta", "gamma")
    spans = block_byte_spans(doc)
    assert [s.id for s in spans] == ["alpha", "beta", "gamma"]

    # Cap that lands exactly on the last block's start byte: gamma is dropped
    # (start_byte >= cap), alpha/beta are not.
    cap = spans[-1].start_byte
    report = measure_agents_md_byte_cap(doc, cap=cap)
    assert report.dropped_block_ids == ["gamma"]
    assert report.over_cap


def test_measure_byte_cap_boundary_off_by_one() -> None:
    """A block starting one byte before the cap survives; at the cap it drops."""
    doc = _doc_with_blocks("head", "tail")
    tail_start = block_byte_spans(doc)[-1].start_byte

    just_under = measure_agents_md_byte_cap(doc, cap=tail_start + 1)
    assert just_under.dropped_block_ids == []

    at_boundary = measure_agents_md_byte_cap(doc, cap=tail_start)
    assert at_boundary.dropped_block_ids == ["tail"]


def test_measure_byte_cap_all_blocks_fit_under_generous_cap() -> None:
    """A cap above the whole document drops nothing and is not over-cap."""
    doc = _doc_with_blocks("only")
    report = measure_agents_md_byte_cap(doc, cap=1_000_000)
    assert report.dropped_block_ids == []
    assert not report.over_cap


def test_block_byte_spans_counts_utf8_bytes_not_chars() -> None:
    """Byte offsets exceed char offsets when multibyte prose precedes a block.

    A caller comparing block positions against a *byte* cap must not use the
    character offsets that :func:`regions.find_regions` reports directly.
    """
    prefix = "café中\n"  # é = 2 bytes, 中 = 3 bytes; longer in bytes than chars
    doc = prefix + _doc_with_blocks("x")
    span = block_byte_spans(doc)[0]
    assert span.start_byte == len(prefix.encode("utf-8"))
    assert span.start_byte > len(prefix)  # byte offset outruns the char offset


def test_measure_byte_cap_rejects_nonpositive_cap() -> None:
    """A non-positive cap fails fast with ValueError (the API contract)."""
    with pytest.raises(ValueError, match="cap must be positive"):
        measure_agents_md_byte_cap("x", cap=0)
    with pytest.raises(ValueError, match="cap must be positive"):
        measure_agents_md_byte_cap("x", cap=-5)


def test_measure_byte_cap_propagates_region_parse_error() -> None:
    """A malformed marker block surfaces as RegionParseError, not silence."""
    broken = "<!-- BEGIN EAWF:managed id=x version=1.0 hash=0123456789abcdef -->\nbody\n"
    with pytest.raises(RegionParseError):
        measure_agents_md_byte_cap(broken, cap=100)


# ---- doctor check: check_agents_md_byte_cap --------------------------------


def test_codex_project_doc_byte_cap_is_the_measured_boundary() -> None:
    """The cap is Codex's measured truncation point, not a padded budget.

    A probe run had its last received rule end mid-sentence exactly at byte
    32768; a control run that raised only the cap received the complete final
    rule. Pinning the constant here keeps a future "give it some headroom"
    edit from silently reintroducing a cap the consumer does not honour.
    """
    assert checks.CODEX_PROJECT_DOC_BYTE_CAP == 32768


def test_check_under_cap_passes(tmp_path: Path) -> None:
    """A small on-disk AGENTS.md is under the default cap -> ok."""
    doc = _doc_with_blocks("small")
    (tmp_path / "AGENTS.md").write_text(doc, encoding="utf-8")

    result = checks.check_agents_md_byte_cap(workspace=tmp_path)
    assert result.name == "agents_md_byte_cap"
    assert result.status == "ok"
    assert "within" in (result.detail or "")


def test_check_over_cap_fails_naming_dropped_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An over-cap AGENTS.md yields a blocking fail naming the dropped blocks."""
    doc = _doc_with_blocks("keep", "cut_one", "cut_two")
    (tmp_path / "AGENTS.md").write_text(doc, encoding="utf-8")

    # Pin the cap onto the first block that must fall past the cut, so the two
    # trailing blocks are dropped and the leading one survives.
    spans = block_byte_spans(doc)
    cut_start = next(s.start_byte for s in spans if s.id == "cut_one")
    monkeypatch.setattr(checks, "CODEX_PROJECT_DOC_BYTE_CAP", cut_start)

    result = checks.check_agents_md_byte_cap(workspace=tmp_path)
    assert result.status == "fail"
    detail = result.detail or ""
    assert "cut_one" in detail
    assert "cut_two" in detail
    assert "keep" not in detail


def test_check_no_agents_md_is_ok(tmp_path: Path) -> None:
    """No AGENTS.md at the anchor -> ok (nothing to measure)."""
    result = checks.check_agents_md_byte_cap(workspace=tmp_path)
    assert result.status == "ok"
    assert "no AGENTS.md" in (result.detail or "")


def test_check_no_anchor_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable workspace anchor -> ok, never a spurious fail."""
    monkeypatch.setattr(checks, "_resolve_anchor", lambda _ws: None)
    result = checks.check_agents_md_byte_cap(workspace=None)
    assert result.status == "ok"
    assert "no workspace anchor" in (result.detail or "")


def test_check_malformed_markers_over_cap_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken marker cannot name blocks, but the over-cap verdict stands."""
    broken = "<!-- BEGIN EAWF:managed id=x version=1.0 hash=0123456789abcdef -->\nbody\n"
    (tmp_path / "AGENTS.md").write_text(broken, encoding="utf-8")
    monkeypatch.setattr(checks, "CODEX_PROJECT_DOC_BYTE_CAP", 10)

    result = checks.check_agents_md_byte_cap(workspace=tmp_path)
    assert result.status == "fail"
    assert "malformed markers" in (result.detail or "")
