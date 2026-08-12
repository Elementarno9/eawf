"""Hardcoded-baseline regression for the lifecycle status-tint hexes.

The green-accent rotation moves ONLY ``accent`` / ``primary``
in the per-theme palettes; the Wong/IBM lifecycle ``status-*`` tints and
the ``ok`` / ``warn`` / ``err`` band hexes stay byte-identical. The
sibling :mod:`tests.tui.test_widgets_status_tint` suite proves the maps
DERIVE from :data:`~eawf.surfaces.tui.theme.WONG_VARIABLES` (the DRY
contract), but that derivation tracks any WONG retune silently -- a future
edit that recolours a Wong ``status-*`` hex would keep those tests green.

This suite is the byte-baseline backstop: it hardcodes the documented
prior hexes so an accidental tint move (in the Wong palette OR in the
status_tint derivation) reds the gate. ``status-claimed`` is asserted to
stay the cool teal ``#56b6c2`` so it reads distinct from the green accent
and the green ``status-closed``.
"""

from __future__ import annotations

from eawf.surfaces.tui.theme import WONG_VARIABLES
from eawf.surfaces.tui.widgets.status_tint import BAND_HEX, STATUS_COLOURS

#: The documented prior Wong deuteranopia-safe lifecycle tints. These MUST
#: NOT change under the green-accent rotation -- only ``accent`` /
#: ``primary`` move. Hardcoded (not derived) so a WONG retune reds this.
_EXPECTED_STATUS_HEX: dict[str, str] = {
    "pending": "#6c6c6c",
    "planned": "#6c6c6c",
    "claimed": "#56b6c2",
    "in_progress": "#e69f00",
    "active": "#e69f00",
    "closed": "#009e73",
    "abandoned": "#6c6c6c",
    "archived": "#6c6c6c",
    "failed": "#d55e00",
}

#: The documented prior Wong burn-band hexes (ok / warn / err). Unchanged
#: by the rotation; hardcoded so a band retune reds this gate.
_EXPECTED_BAND_HEX: dict[str, str] = {
    "ok": "#009e73",
    "warn": "#e69f00",
    "err": "#d55e00",
}


def test_status_colours_byte_identical_to_prior_baseline() -> None:
    """Every lifecycle tint stays byte-identical to the documented baseline."""
    assert STATUS_COLOURS == _EXPECTED_STATUS_HEX


def test_band_hex_byte_identical_to_prior_baseline() -> None:
    """The ok/warn/err band hexes stay byte-identical to the baseline."""
    assert BAND_HEX == _EXPECTED_BAND_HEX


def test_status_claimed_stays_cool_teal() -> None:
    """``status-claimed`` keeps the cool teal, distinct from the green accent.

    The accent rotated teal -> green; if ``status-claimed`` had ridden the
    rotation it would collide with the green ``status-closed`` and the green
    accent. It must stay ``#56b6c2`` so claimed reads cool against closed.
    """
    assert WONG_VARIABLES["status-claimed"] == "#56b6c2"
    assert STATUS_COLOURS["claimed"] == "#56b6c2"
    # And it is genuinely distinct from the (green) closed tint + accent.
    assert STATUS_COLOURS["claimed"] != STATUS_COLOURS["closed"]
    assert STATUS_COLOURS["claimed"] != WONG_VARIABLES["accent"]


def test_accent_primary_rotated_to_green_but_tints_untouched() -> None:
    """The rotation moved accent/primary to green without touching tints.

    Guards the wave's core invariant in one place: ``accent`` == ``primary``
    == the reskin green, while the lifecycle tints + bands are unchanged.
    """
    assert WONG_VARIABLES["accent"] == "#16b384"
    assert WONG_VARIABLES["primary"] == "#16b384"
    # The named tints did not ride the rotation.
    assert WONG_VARIABLES["status-pending"] == "#6c6c6c"
    assert WONG_VARIABLES["status-in-progress"] == "#e69f00"
    assert WONG_VARIABLES["status-closed"] == "#009e73"
    assert WONG_VARIABLES["status-failed"] == "#d55e00"
