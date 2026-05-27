"""Tests for clickable reference wrapping."""

from __future__ import annotations

from eawf.surfaces.render.link_wrap import (
    LINK_PATTERNS,
    REFERENCE_KINDS,
    iter_refs,
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
