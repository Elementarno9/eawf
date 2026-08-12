"""Unit tests for the indeterminate braille spinner.

Pins :class:`~eawf.surfaces.render.spinner.BrailleSpinner`: the
deterministic eight-frame sequence, the wrap after eight frames, and the
distinctness of the eight single-dot glyphs.
"""

from __future__ import annotations

from eawf.surfaces.render.spinner import (
    BRAILLE_SPINNER_FRAMES,
    SPINNER_PERIOD,
    BrailleSpinner,
)


def test_spinner_period_is_eight() -> None:
    """The cycle has exactly eight frames -- one per braille dot."""
    assert SPINNER_PERIOD == 8
    assert len(BRAILLE_SPINNER_FRAMES) == 8


def test_spinner_frames_are_distinct_braille_glyphs() -> None:
    """Each of the eight frames is a distinct Braille-Patterns glyph."""
    assert len(set(BRAILLE_SPINNER_FRAMES)) == 8
    for frame in BRAILLE_SPINNER_FRAMES:
        assert len(frame) == 1
        assert 0x2800 <= ord(frame) <= 0x28FF


def test_spinner_advance_yields_deterministic_sequence() -> None:
    """The first eight advances yield the frame tuple in order."""
    spinner = BrailleSpinner()
    sequence = tuple(spinner.advance() for _ in range(SPINNER_PERIOD))
    assert sequence == BRAILLE_SPINNER_FRAMES


def test_spinner_first_advance_is_frame_zero() -> None:
    """The very first advance returns the zeroth frame."""
    spinner = BrailleSpinner()
    assert spinner.advance() == BRAILLE_SPINNER_FRAMES[0]


def test_spinner_wraps_after_eight_frames() -> None:
    """The ninth advance wraps back to the zeroth frame."""
    spinner = BrailleSpinner()
    for _ in range(SPINNER_PERIOD):
        spinner.advance()
    assert spinner.advance() == BRAILLE_SPINNER_FRAMES[0]


def test_spinner_two_full_cycles_repeat() -> None:
    """Sixteen advances yield the eight-frame cycle twice."""
    spinner = BrailleSpinner()
    sequence = tuple(spinner.advance() for _ in range(2 * SPINNER_PERIOD))
    assert sequence == BRAILLE_SPINNER_FRAMES + BRAILLE_SPINNER_FRAMES


def test_spinner_frame_property_before_advance_is_frame_zero() -> None:
    """The current frame before any advance is the zeroth frame."""
    spinner = BrailleSpinner()
    assert spinner.frame == BRAILLE_SPINNER_FRAMES[0]


def test_spinner_frame_property_tracks_advance() -> None:
    """The current-frame property reflects the last advance without stepping."""
    spinner = BrailleSpinner()
    spinner.advance()
    spinner.advance()
    assert spinner.frame == BRAILLE_SPINNER_FRAMES[1]
    assert spinner.frame == BRAILLE_SPINNER_FRAMES[1]


def test_spinner_reset_returns_to_frame_zero() -> None:
    """After reset the next advance returns the zeroth frame again."""
    spinner = BrailleSpinner()
    for _ in range(3):
        spinner.advance()
    spinner.reset()
    assert spinner.advance() == BRAILLE_SPINNER_FRAMES[0]
