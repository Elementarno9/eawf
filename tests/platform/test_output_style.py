"""Tests for the lean/explain house output-style presets (P30-I03-W04).

Pins the W04 success criteria:

* :class:`OutputStyle` resolves to ``lean`` by default (the config-chain
  ``output.style`` leaf and the directive renderer both fall back to it).
* The ``explain`` directive renders the verbose ``why_present`` /
  ``jargon_defined`` clauses that the ``lean`` directive lacks.
* An unknown ``output.style`` token raises :class:`ValidationError` at the
  profile-load boundary.
* :class:`OutputStylePreset` is strict (``extra="forbid"``).
* The ``lean`` directive is built of readable sentences with no all-caps
  fragment markers, pinning the not-caveman-caps clause.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from eawf.platform.profiles.clarity import (
    DEFAULT_OUTPUT_STYLE,
    NEWCOMER_TEST_DIMENSIONS,
    OUTPUT_STYLE_PRESETS,
    OutputStyle,
    OutputStylePreset,
    render_style_directive,
)
from eawf.platform.profiles.models import OutputBlock, ProfileBody

#: Matches two or more consecutive ALL-CAPS words (length >= 2 each) such as
#: the "WRITE FOR" / "STATE THE" fragment markers a caveman-style directive
#: would use. A single all-caps token (an acronym) is allowed; a run of two is
#: the shorthand-fragment signature the not-caveman-caps clause forbids.
_ALL_CAPS_FRAGMENT = re.compile(r"\b[A-Z]{2,}\b(?:\s+\b[A-Z]{2,}\b)+")


# ---- default resolution is lean -------------------------------------------


def test_default_output_style_is_lean() -> None:
    """The canonical default style resolves to :attr:`OutputStyle.LEAN`."""
    assert DEFAULT_OUTPUT_STYLE is OutputStyle.LEAN


def test_output_block_default_style_is_lean() -> None:
    """A freshly-constructed :class:`OutputBlock` defaults to ``lean``."""
    assert OutputBlock().style is OutputStyle.LEAN


def test_profile_body_omitting_output_resolves_lean() -> None:
    """A profile body that omits ``output:`` resolves to the lean block."""
    body = ProfileBody.model_validate({"name": "x"})
    assert body.output.style is OutputStyle.LEAN


def test_render_style_directive_default_is_lean() -> None:
    """:func:`render_style_directive` defaults to the lean directive."""
    assert render_style_directive() == render_style_directive(OutputStyle.LEAN)


# ---- explain directive carries the verbose clauses lean lacks --------------


def test_explain_directive_has_why_jargon_clauses_lean_lacks() -> None:
    """The explain directive expands why/jargon clauses absent from lean."""
    lean = render_style_directive(OutputStyle.LEAN)
    explain = render_style_directive(OutputStyle.EXPLAIN)
    # The verbose "because ..." motivation clauses are the explain-only delta.
    assert "because" not in lean
    assert "because" in explain
    # The expansion lands specifically on the why_present + jargon_defined
    # rows, so the explain directive is strictly longer than lean.
    assert len(explain) > len(lean)


def test_explain_preset_clauses_differ_only_on_why_and_jargon() -> None:
    """Explain expands exactly the why_present + jargon_defined rows."""
    lean = OUTPUT_STYLE_PRESETS[OutputStyle.LEAN].clauses
    explain = OUTPUT_STYLE_PRESETS[OutputStyle.EXPLAIN].clauses
    differing = {key for key in lean if lean[key] != explain[key]}
    assert differing == {"why_present", "jargon_defined"}


def test_directive_covers_every_newcomer_dimension() -> None:
    """Each rendered directive carries one bullet per scored dimension."""
    for style in OutputStyle:
        directive = render_style_directive(style)
        bullet_lines = [line for line in directive.splitlines() if line.startswith("- ")]
        assert len(bullet_lines) == len(NEWCOMER_TEST_DIMENSIONS)


# ---- unknown style raises ValidationError at config load -------------------


def test_unknown_output_style_raises_validation_error_at_load() -> None:
    """An unknown ``output.style`` token fails the profile load."""
    with pytest.raises(ValidationError) as exc_info:
        ProfileBody.model_validate({"name": "x", "output": {"style": "verbose"}})
    assert "verbose" in str(exc_info.value)


def test_unknown_output_block_style_raises_validation_error() -> None:
    """An unknown style on :class:`OutputBlock` directly raises too."""
    with pytest.raises(ValidationError):
        OutputBlock.model_validate({"style": "caveman"})


# ---- OutputStylePreset is extra-forbid -------------------------------------


def test_output_style_preset_rejects_extra_key() -> None:
    """Strict Pydantic v2 -- an extra key on the preset raises."""
    with pytest.raises(ValidationError):
        OutputStylePreset.model_validate(
            {"style": "lean", "clauses": {"audience_fit": "x"}, "bogus": 1},
        )


def test_output_block_rejects_extra_key() -> None:
    """Strict Pydantic v2 -- an extra key on :class:`OutputBlock` raises."""
    with pytest.raises(ValidationError):
        OutputBlock.model_validate({"style": "lean", "tone": "snarky"})


def test_output_style_preset_requires_non_empty_clauses() -> None:
    """An empty clause map fails the ``min_length=1`` bound."""
    with pytest.raises(ValidationError):
        OutputStylePreset.model_validate({"style": "lean", "clauses": {}})


# ---- lean directive has no all-caps fragment markers (not-caveman-caps) ----


def test_lean_directive_has_no_all_caps_fragments() -> None:
    """The lean directive reads as sentences, not all-caps caveman fragments."""
    lean = render_style_directive(OutputStyle.LEAN)
    match = _ALL_CAPS_FRAGMENT.search(lean)
    assert match is None, f"lean directive carries an all-caps fragment: {match!r}"


def test_explain_directive_has_no_all_caps_fragments() -> None:
    """The explain directive is also readable prose, not caveman fragments."""
    explain = render_style_directive(OutputStyle.EXPLAIN)
    assert _ALL_CAPS_FRAGMENT.search(explain) is None


def test_all_caps_fragment_regex_actually_catches_caveman_prose() -> None:
    """The guard regex fires on genuine all-caps fragment shorthand."""
    assert _ALL_CAPS_FRAGMENT.search("WRITE FOR NEWCOMER NOT INSIDER") is not None
    # A single acronym is not a fragment run and must not trip the guard.
    assert _ALL_CAPS_FRAGMENT.search("cite with dense [N] markers") is None


def test_lean_preset_clauses_are_sentence_cased() -> None:
    """Every lean clause is a capitalized sentence, not an all-caps marker."""
    lean = OUTPUT_STYLE_PRESETS[OutputStyle.LEAN].clauses
    for key, clause in lean.items():
        assert _ALL_CAPS_FRAGMENT.search(clause) is None, key
        # A readable sentence starts upper and is not wholly upper-cased.
        assert clause != clause.upper(), key
