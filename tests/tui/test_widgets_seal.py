"""Tests for the ASCII-art Seal brand mark.

The Seal is the project's brand mark, rendered as a deterministic half-block
TEXT block in the theme accent (no terminal graphics protocol): the half-block
glyphs render in ``$accent`` while the empty halves stay unstyled so the cell's
``$surface`` shows through. The art is theme-portable on one colour and renders
identically in CI, a pipe, and a live terminal -- so the goldens stay stable.
"""

from __future__ import annotations

from textual.widgets import Static

from eawf.surfaces.tui.widgets.seal import (
    SEAL_ART_CLASS,
    SEAL_ART_ID,
    SEAL_ART_LINES,
    seal_art_markup,
    seal_art_widget,
)

# --------------------------------------------------------------------------
# The ASCII-art Seal -- deterministic accent-on-surface TEXT brand mark
# that the hero surfaces render (no graphics protocol, theme-portable on one
# accent colour).
# --------------------------------------------------------------------------

#: The only glyphs the half-block art is allowed to carry -- the three half-block
#: shapes plus the empty-half space. Anything else is a transcription error.
_ART_GLYPHS = frozenset("▀▄█ ")


def test_seal_art_lines_are_nineteen_rows() -> None:
    # The art is exactly the 19 rows of the operator-approved 42-wide seal.
    assert len(SEAL_ART_LINES) == 19


def test_seal_art_lines_only_carry_half_block_glyphs() -> None:
    # Every row is pure half-block art: only ▀▄█ + spaces, no stray glyph that
    # would mis-render the mark or trip a markup parse.
    for row in SEAL_ART_LINES:
        stray = set(row) - _ART_GLYPHS
        assert not stray, f"row {row!r} carries non-art glyphs {stray!r}"


def test_seal_art_max_width_is_forty_two() -> None:
    # Every row is padded to the full 42 cells (symmetric block) -- the box the
    # hero CSS sizes around, so the disc centres within the Static.
    assert max(len(row) for row in SEAL_ART_LINES) == 42
    assert all(len(row) == 42 for row in SEAL_ART_LINES)


def test_seal_art_center_row_is_non_empty() -> None:
    # The vertical centre of the disc carries the star knockout, never a blank
    # row -- a blank centre would mean the art collapsed to an empty frame.
    center = SEAL_ART_LINES[len(SEAL_ART_LINES) // 2]
    assert center.strip(), "the centre art row must not be blank"
    assert any(glyph in center for glyph in "▀▄█")


def test_seal_art_markup_wraps_the_block_in_the_accent() -> None:
    # The markup wraps the WHOLE block in a single [$accent]...[/] span so the
    # half-block glyphs render in the theme accent while the empty halves stay
    # unstyled (the cell $surface shows through -> theme-portable).
    markup = seal_art_markup()
    assert markup.startswith("[$accent]")
    assert markup.endswith("[/]")
    # The raw art block sits verbatim inside the single accent span.
    inner = markup[len("[$accent]") : -len("[/]")]
    assert inner == "\n".join(SEAL_ART_LINES)
    # Exactly one accent span -- not one per line (the empty halves must show the
    # surface, so the block is wrapped once, not glyph-by-glyph).
    assert markup.count("[$accent]") == 1
    assert markup.count("[/]") == 1


def test_seal_art_widget_is_static_with_stable_id_and_class() -> None:
    # The factory hands back a Static carrying the stable id + the hero-sizing
    # class so the empty-state CSS can box + center it. It never returns None
    # and never touches a graphics protocol.
    widget = seal_art_widget()
    assert isinstance(widget, Static)
    assert widget.id == SEAL_ART_ID
    assert SEAL_ART_CLASS in widget.classes
