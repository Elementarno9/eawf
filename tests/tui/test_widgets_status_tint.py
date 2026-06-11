"""Unit tests for the shared ``widgets/status_tint.py`` rendering helper.

Covers the status-tint map (every lifecycle status across the three enums
maps to a concrete hex), the canonical-palette-sourced invariant (the
fallback hexes derive from the single Wong palette, not a re-typed copy),
the band palette, and :func:`status_colour`'s mapped / unmapped / non-enum
paths.
"""

from __future__ import annotations

import pytest

from eawf.kernel.state.enums import IterStatus, PhaseStatus, WaveStatus
from eawf.surfaces.tui.theme import WONG_VARIABLES
from eawf.surfaces.tui.widgets.status_tint import (
    BAND_HEX,
    SELECTION_TINT,
    STATUS_COLOURS,
    status_colour,
)

_HEX_RE = "#0123456789abcdefABCDEF"


# --------------------------------------------------------------------------
# STATUS_COLOURS — completeness across the three lifecycle enums
# --------------------------------------------------------------------------


def test_status_colours_covers_every_wave_status() -> None:
    # Every WaveStatus value resolves to a tint so no wave row renders an
    # untinted glyph.
    for status in WaveStatus:
        assert status.value in STATUS_COLOURS


def test_status_colours_covers_every_phase_status() -> None:
    for status in PhaseStatus:
        assert status.value in STATUS_COLOURS


def test_status_colours_covers_every_iter_status() -> None:
    for status in IterStatus:
        assert status.value in STATUS_COLOURS


def test_status_colours_values_are_hex() -> None:
    # Tree node labels are Rich-parsed and need concrete hex, never a $var.
    for colour in STATUS_COLOURS.values():
        assert colour.startswith("#")
        assert len(colour) == 7  # #rrggbb
        assert all(ch in _HEX_RE for ch in colour)


# --------------------------------------------------------------------------
# Canonical-palette-sourced invariant — DRY (one home for the hexes)
# --------------------------------------------------------------------------


def test_status_colours_sourced_from_wong_palette() -> None:
    # The named statuses take their hex straight from the canonical Wong
    # status-* palette; the lifecycle-only statuses alias onto the nearest
    # named tint. This is the DRY contract: the hexes live in one place.
    assert STATUS_COLOURS["pending"] == WONG_VARIABLES["status-pending"]
    assert STATUS_COLOURS["claimed"] == WONG_VARIABLES["status-claimed"]
    assert STATUS_COLOURS["in_progress"] == WONG_VARIABLES["status-in-progress"]
    assert STATUS_COLOURS["closed"] == WONG_VARIABLES["status-closed"]
    assert STATUS_COLOURS["failed"] == WONG_VARIABLES["status-failed"]


def test_status_colours_aliases_track_named_tints() -> None:
    # planned reads as pending, active as in_progress, abandoned/archived as
    # the muted pending grey — all sourced from the same canonical hexes.
    assert STATUS_COLOURS["planned"] == STATUS_COLOURS["pending"]
    assert STATUS_COLOURS["active"] == STATUS_COLOURS["in_progress"]
    assert STATUS_COLOURS["abandoned"] == STATUS_COLOURS["pending"]
    assert STATUS_COLOURS["archived"] == STATUS_COLOURS["pending"]


def test_selection_tint_is_the_brand_accent_dim_hex() -> None:
    # The selected / focused row's highlight rectangle is the brand-book
    # accent-dim ("selection, fills, focus rings"), pinned as a concrete hex so
    # every selectable pane resolves one canonical green-family selection tint.
    assert SELECTION_TINT == "#0c5a44"
    assert SELECTION_TINT.startswith("#")
    assert len(SELECTION_TINT) == 7  # #rrggbb
    assert all(ch in _HEX_RE for ch in SELECTION_TINT)


def test_band_hex_sourced_from_wong_palette() -> None:
    # The ok/warn/err burn-band fallback hexes derive from the same single
    # Wong palette, not a re-typed copy.
    assert {
        "ok": WONG_VARIABLES["ok"],
        "warn": WONG_VARIABLES["warn"],
        "err": WONG_VARIABLES["err"],
    } == BAND_HEX


# --------------------------------------------------------------------------
# status_colour — mapped / unmapped / non-enum lookup paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (WaveStatus.IN_PROGRESS, WONG_VARIABLES["status-in-progress"]),
        (WaveStatus.CLOSED, WONG_VARIABLES["status-closed"]),
        (WaveStatus.FAILED, WONG_VARIABLES["status-failed"]),
        (PhaseStatus.ACTIVE, WONG_VARIABLES["status-in-progress"]),
        (IterStatus.PLANNED, WONG_VARIABLES["status-pending"]),
    ],
)
def test_status_colour_returns_mapped_hex(status: object, expected: str) -> None:
    assert status_colour(status) == expected


def test_status_colour_unmapped_string_returns_none() -> None:
    # An object whose .value is a string the map does not carry falls back
    # to the default uncoloured glyph (None), never a crash.
    class _Stray:
        value = "no-such-status"

    assert status_colour(_Stray()) is None


def test_status_colour_non_enum_returns_none() -> None:
    # A non-enum input (no .value, or a non-string .value) returns None.
    assert status_colour(None) is None
    assert status_colour("pending") is None  # bare str has no .value attr

    class _IntValue:
        value = 3

    assert status_colour(_IntValue()) is None


def test_status_colour_every_enum_member_resolves() -> None:
    # The integration contract: status_colour resolves a concrete hex for
    # every member of all three lifecycle enums (no row goes untinted).
    for enum in (WaveStatus, PhaseStatus, IterStatus):
        for status in enum:
            assert status_colour(status) is not None
