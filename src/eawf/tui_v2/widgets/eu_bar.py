"""``EUBar`` — a 5-cell effort-unit progress bar (C06 widget catalog).

Per the C06 brief §5.3 widget row: a fixed-width 5-cell glyph bar that
renders ``consumed_eu / total_eu`` as filled (``#``) vs empty (``-``)
cells, colour-coded by the consumed fraction:

* ``ok``   when consumed ≤ 80 % of total,
* ``warn`` when consumed ≤ 100 % of total,
* ``err``  when consumed > 100 % of total (over budget).

A trailing right-aligned percentage label sits after the bar. The widget
is a leaf :class:`~textual.widgets.Static` so it composes inline inside a
:class:`~eawf.tui_v2.widgets.roadmap_tree.RoadmapTree` row and stands
alone in any pane. It is driven by two plain floats — :meth:`set_eu` — so
it stays decoupled from the :class:`~eawf.state.models.State` schema; the
caller maps state onto the consumed / total pair.

The fill maths use the same banker's-rounding-free integer cell count the
glyph bar needs (round-half-up via ``+ 0.5``) so a 1 % consumption still
lights the first cell rather than rounding away to an empty bar. The bar
glyphs are coloured via Textual content markup against the ``theme.tcss``
palette vars (``$ok`` / ``$warn`` / ``$err``) — never hardcoded hex — so
the runtime theme swap stays a CSS var rebind.
"""

from __future__ import annotations

from typing import ClassVar

from textual.reactive import reactive
from textual.widgets import Static

#: Number of glyph cells in the bar. Fixed at 5 per the C06 widget row
#: (``5-cell glyph bar``); the inline roadmap variant relies on this
#: constant width so tree rows align.
BAR_CELLS: int = 5

#: Filled-cell glyph (Nerd-Font-always per the brief glyph set; the
#: ``--plain`` ASCII fallback uses the same ``#`` so no swap is needed).
GLYPH_FULL: str = "#"

#: Empty-cell glyph.
GLYPH_EMPTY: str = "-"

#: Consumed-fraction upper bound (inclusive) for the ``ok`` colour band.
OK_THRESHOLD: float = 0.80

#: Consumed-fraction upper bound (inclusive) for the ``warn`` colour band;
#: anything above is ``err`` (over budget).
WARN_THRESHOLD: float = 1.00


def _fill_cells(fraction: float) -> int:
    """Return the number of filled cells for *fraction* of the bar.

    Clamps the fraction into ``[0, 1]`` for cell counting (an over-budget
    bar still shows all cells filled; the colour band carries the
    over-budget signal). Rounds half-up, so with :data:`BAR_CELLS` cells
    each cell covers ``1 / BAR_CELLS`` of the range and the first cell
    lights once the fraction reaches half a cell (``0.5 / BAR_CELLS``).

    Args:
        fraction: Consumed / total ratio (may exceed 1.0 when over
            budget; may be negative only on malformed input — clamped).

    Returns:
        Integer cell count in ``[0, BAR_CELLS]``.
    """
    clamped = min(max(fraction, 0.0), 1.0)
    return int(clamped * BAR_CELLS + 0.5)


def band_var(fraction: float) -> str:
    """Return the ``theme.tcss`` palette var for *fraction*'s colour band.

    Args:
        fraction: Consumed / total ratio.

    Returns:
        One of ``"$ok"`` / ``"$warn"`` / ``"$err"`` — the palette vars
        defined in ``theme.tcss`` and resolved by Textual content markup.
    """
    if fraction <= OK_THRESHOLD:
        return "$ok"
    if fraction <= WARN_THRESHOLD:
        return "$warn"
    return "$err"


def _bar_parts(consumed_eu: float, total_eu: float) -> tuple[str, int, str]:
    """Return ``(glyphs, pct, band_var)`` for a consumed / total pair.

    Single source of the fill maths shared by the plain + markup
    renderers. A zero *total_eu* yields an empty bar at ``0%`` rather than
    dividing by zero.

    Args:
        consumed_eu: Effort units consumed so far (≥ 0).
        total_eu: Total estimated effort units (≥ 0).

    Returns:
        The glyph run, the integer percentage, and the palette colour var.
    """
    fraction = consumed_eu / total_eu if total_eu > 0 else 0.0
    filled = _fill_cells(fraction)
    glyphs = GLYPH_FULL * filled + GLYPH_EMPTY * (BAR_CELLS - filled)
    pct = int(fraction * 100 + 0.5)
    return glyphs, pct, band_var(fraction)


def render_bar_plain(consumed_eu: float, total_eu: float) -> str:
    """Render the bar + trailing percent as an uncoloured plain string.

    The colour-free counterpart to :func:`render_bar_markup`, for
    embedding the bar inside a Rich-markup context (e.g. a
    :class:`~textual.widgets.Tree` node label, which is parsed by Rich and
    does not resolve the Textual ``$`` palette vars). The glyph itself
    carries the fill signal; the host row's status glyph carries colour.

    Args:
        consumed_eu: Effort units consumed so far (≥ 0).
        total_eu: Total estimated effort units (≥ 0).

    Returns:
        A plain string of the form ``#####  120%``.
    """
    glyphs, pct, _ = _bar_parts(consumed_eu, total_eu)
    return f"{glyphs}  {pct}%"


def render_bar_markup(consumed_eu: float, total_eu: float) -> str:
    """Render the bar + trailing percent as a Textual content-markup string.

    Pure helper so the glyph string, colour band, and percentage are
    unit-testable without mounting the widget. The glyph run is wrapped in
    a ``[$ok|$warn|$err]…[/]`` content-markup span so Textual resolves the
    colour against the ``theme.tcss`` palette at render time.

    Use :func:`render_bar_plain` instead when embedding the bar in a
    Rich-markup context (Tree labels), which cannot resolve the palette
    vars.

    Args:
        consumed_eu: Effort units consumed so far (≥ 0).
        total_eu: Total estimated effort units (≥ 0).

    Returns:
        A content-markup string of the form ``[$ok]#####[/]  120%``.
    """
    glyphs, pct, band = _bar_parts(consumed_eu, total_eu)
    return f"[{band}]{glyphs}[/]  {pct}%"


class EUBar(Static):
    """A 5-cell colour-banded EU progress bar (consumed / total).

    Standalone leaf widget: set the consumed / total pair via
    :meth:`set_eu` (or the reactive :attr:`consumed_eu` / :attr:`total_eu`
    attributes directly in tests) and the bar repaints. Used inline in the
    roadmap tree's wave rows and anywhere a compact effort gauge is wanted.
    """

    DEFAULT_CSS: ClassVar[str] = """
    EUBar {
        height: 1;
        width: auto;
    }
    """

    #: Effort units consumed so far. Watched so assignment repaints.
    consumed_eu: reactive[float] = reactive(0.0)

    #: Total estimated effort units. Watched so assignment repaints.
    total_eu: reactive[float] = reactive(0.0)

    def set_eu(self, consumed_eu: float, total_eu: float) -> None:
        """Set both EU values in one shot and repaint.

        Args:
            consumed_eu: Effort units consumed so far (≥ 0).
            total_eu: Total estimated effort units (≥ 0).
        """
        self.consumed_eu = consumed_eu
        self.total_eu = total_eu

    def watch_consumed_eu(self) -> None:
        """Repaint when the consumed value changes."""
        self._repaint()

    def watch_total_eu(self) -> None:
        """Repaint when the total value changes."""
        self._repaint()

    def _repaint(self) -> None:
        """Re-render the bar from the current reactive values."""
        self.update(render_bar_markup(self.consumed_eu, self.total_eu))


__all__ = [
    "BAR_CELLS",
    "GLYPH_EMPTY",
    "GLYPH_FULL",
    "OK_THRESHOLD",
    "WARN_THRESHOLD",
    "EUBar",
    "band_var",
    "render_bar_markup",
    "render_bar_plain",
]
