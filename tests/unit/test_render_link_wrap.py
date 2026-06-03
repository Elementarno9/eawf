"""Tests for clickable reference wrapping."""

from __future__ import annotations

from eawf.platform.artifacts.references import (
    Citation,
    citation_numbers_in_text,
    validate_dense_citation_refs,
)
from eawf.surfaces.render.link_wrap import (
    LINK_PATTERNS,
    REFERENCE_KINDS,
    iter_refs,
    linkify_citations,
    linkify_text,
    tooltip_summary,
)


def test_link_pattern_catalog_has_one_regex_per_reference_kind() -> None:
    assert len(LINK_PATTERNS) == 14
    assert {pattern.kind for pattern in LINK_PATTERNS} == set(REFERENCE_KINDS)


def test_iter_refs_prefers_wave_over_parent_lifecycle_refs() -> None:
    refs = iter_refs("open P28-I03-W35")
    assert [(ref.kind, ref.target) for ref in refs] == [("wave", "P28-I03-W35")]


def test_iter_refs_keeps_spec_ref_whole() -> None:
    refs = iter_refs("see spec:P28-I03-W35")
    assert [(ref.kind, ref.target) for ref in refs] == [("spec", "P28-I03-W35")]


def test_linkify_text_emits_per_type_action_markup() -> None:
    markup = linkify_text("open P28-I03-W35")
    assert "app.open_wave_ref('P28-I03-W35')" in markup
    assert "[u]P28-I03-W35[/]" in markup


def test_tooltip_summary_lists_refs() -> None:
    assert tooltip_summary("P28-I03-W35 and H01-02") == "wave P28-I03-W35\nhypothesis H01-02"


# ---- inline citation linkify (W14) ------------------------------------------


def test_linkify_citations_rewrites_bare_marker_to_anchor_link() -> None:
    assert linkify_citations("Finding [1] and [2].") == (
        r"Finding [\[1\]](#ref-1) and [\[2\]](#ref-2)."
    )


def test_linkify_citations_leaves_image_alt_text_untouched() -> None:
    assert linkify_citations("See ![1](img.png) here.") == "See ![1](img.png) here."


def test_linkify_citations_leaves_markdown_link_text_untouched() -> None:
    assert linkify_citations("see [1](https://example.org) link") == (
        "see [1](https://example.org) link"
    )


def test_linkify_citations_is_idempotent() -> None:
    once = linkify_citations("Finding [1].")
    assert linkify_citations(once) == once


def test_linkify_citations_handles_multi_digit_markers() -> None:
    assert linkify_citations("ref [12] here") == r"ref [\[12\]](#ref-12) here"


def test_dense_citation_validation_passes_on_rewritten_body() -> None:
    citations = [
        Citation(n=1, ref="src/eawf/a.py:10"),
        Citation(n=2, ref="docs/b.md"),
    ]
    rewritten = linkify_citations("Loader [1] and the doc [2].")
    # The widened marker grammar counts the linkified form, so validation
    # (which requires every row to be used) passes on the rewritten prose.
    assert citation_numbers_in_text(rewritten) == [1, 2]
    validate_dense_citation_refs(rewritten, citations)
