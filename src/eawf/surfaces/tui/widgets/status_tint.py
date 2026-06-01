"""Shared status-tint rendering helpers for the Eä TUI widgets.

The visual-enrichment layer (the roadmap tree's status-tinted glyphs, the
EU / burn bars' colour bands) reads concrete ``#rrggbb`` hex when it
renders into a Rich-parsed context — a :class:`~textual.widgets.Tree`
node label or a :class:`~textual.widgets.DataTable` ``str`` cell — because
those contexts go through :meth:`rich.text.Text.from_markup` and cannot
resolve the Textual ``$ok`` / ``$warn`` / ``$err`` / ``$status-*`` palette
vars (they raise ``MarkupError``). The widgets that CAN resolve the vars
(the live status pane, the EUBar widget) keep using the ``$`` form so the
runtime ``/theme`` swap stays a CSS var rebind.

Before this module the Wong deuteranopia-safe fallback hexes were
hardcoded in three places — ``roadmap_tree.STATUS_COLOURS`` (lifecycle
status -> hex), ``eu_bar.DEFAULT_BAND_PALETTE`` (ok/warn/err -> hex), and
the canonical :data:`eawf.surfaces.tui.theme.WONG_VARIABLES` palette. This
module is the single home: it derives both the lifecycle-status tint map
and the band palette from the canonical Wong palette so a palette retune
lands in one place, and the widgets import from here rather than re-typing
the hexes.

Colour is always *additive* on top of a glyph (colour-blind safe): the
caller tints a status glyph or a bar's fill, never replaces the glyph
itself, so the surface stays legible without relying on hue.
"""

from __future__ import annotations

from typing import Final

from eawf.surfaces.tui.theme import WONG_VARIABLES

#: Canonical Wong deuteranopia-safe band hexes (``ok`` / ``warn`` / ``err``),
#: derived from the single :data:`~eawf.surfaces.tui.theme.WONG_VARIABLES`
#: palette so the fallback tint matches the live theme's dark baseline
#: byte-for-byte. The EU / burn bars resolve their colour band to one of
#: these hexes when they render into a Rich-parsed cell.
BAND_HEX: Final[dict[str, str]] = {
    "ok": WONG_VARIABLES["ok"],
    "warn": WONG_VARIABLES["warn"],
    "err": WONG_VARIABLES["err"],
}

#: Lifecycle-status (the string ``.value`` of a phase / iter / wave status
#: enum) -> concrete glyph tint, derived from the canonical Wong
#: ``status-*`` palette. Statuses the Wong palette names directly
#: (``pending`` / ``claimed`` / ``in_progress`` / ``closed`` / ``failed``)
#: take their own hex; the lifecycle-only statuses alias onto the nearest
#: named tint (``planned`` reads as ``pending``, ``active`` as
#: ``in_progress``, ``abandoned`` / ``archived`` as the muted ``pending``
#: grey) so all three enums share one map. Keyed by the string status so a
#: single lookup serves phase / iter / wave rows.
STATUS_COLOURS: Final[dict[str, str]] = {
    "pending": WONG_VARIABLES["status-pending"],
    "planned": WONG_VARIABLES["status-pending"],
    "claimed": WONG_VARIABLES["status-claimed"],
    "in_progress": WONG_VARIABLES["status-in-progress"],
    "active": WONG_VARIABLES["status-in-progress"],
    "closed": WONG_VARIABLES["status-closed"],
    "abandoned": WONG_VARIABLES["status-pending"],
    "archived": WONG_VARIABLES["status-pending"],
    "failed": WONG_VARIABLES["status-failed"],
}


def status_colour(status: object) -> str | None:
    """Return the concrete glyph tint for *status*, or ``None`` when unmapped.

    Reads the status enum member's ``.value`` (the string key the shared
    :data:`STATUS_COLOURS` map carries) so the same lookup serves a phase,
    iter, or wave status. An unmapped status (an enum that drifts past the
    map, or a non-enum input) returns ``None`` so the caller falls back to
    the default uncoloured glyph rather than crashing.

    Args:
        status: A lifecycle status enum member (its ``.value`` keys the
            shared :data:`STATUS_COLOURS` map).

    Returns:
        A concrete ``#rrggbb`` hex string, or ``None`` when *status* has no
        mapped tint.
    """
    value = getattr(status, "value", None)
    if not isinstance(value, str):
        return None
    return STATUS_COLOURS.get(value)


__all__ = [
    "BAND_HEX",
    "STATUS_COLOURS",
    "status_colour",
]
