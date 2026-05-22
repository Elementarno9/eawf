"""``EUBar`` — a 5-cell effort-unit progress bar (widget catalog).

A fixed-width 5-cell glyph bar that renders ``consumed_eu / total_eu``
as filled (``#``) vs empty (``-``) cells, colour-coded by the consumed
fraction:

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

#: Number of glyph cells in the bar. Fixed at 5
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

#: Rendered when a bar has no data to show (e.g. an unpopulated EU / token
#: row, a zero-total ratio, or an unknown effort bucket). Surfacing the
#: row with this sentinel — rather than a fabricated 0 % bar — keeps the
#: "surface now, data later" contract honest for not-yet-projected
#: telemetry. Exported so every plain-text bar caller emits the same
#: sentinel.
EMPTY_STATE: str = "— no data"

#: Effort-bucket → filled-cell count for :func:`render_size_bar`. Ordered
#: smallest-to-largest so the bar grows with the bucket (XS lights one
#: cell, XL lights all five).
_BUCKET_CELLS: dict[str, int] = {
    "XS": 1,
    "S": 2,
    "M": 3,
    "L": 4,
    "XL": 5,
}


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


def render_eu_bar_plain(consumed_eu: float, total_eu: float) -> str:
    """Render the EU bar, or :data:`EMPTY_STATE` when *total_eu* is unset.

    Wraps :func:`render_bar_plain` with the empty-state guard the data
    reality demands: estimates / actuals are unpopulated scaffolding, so a
    non-positive *total_eu* must surface :data:`EMPTY_STATE` rather than a
    fabricated 0 % bar. Use this — not the bare :func:`render_bar_plain` —
    for any EU or token row whose total may be zero.

    Args:
        consumed_eu: Effort units consumed so far (≥ 0).
        total_eu: Total estimated effort units (≥ 0). ``<= 0`` yields the
            empty-state sentinel.

    Returns:
        The plain bar string, or :data:`EMPTY_STATE` when *total_eu* is
        non-positive.
    """
    if total_eu <= 0:
        return EMPTY_STATE
    return render_bar_plain(consumed_eu, total_eu)


def render_completion_bar(done: int, total: int, *, width: int = 10) -> str:
    """Render a ``done / total`` completion ratio bar with a count suffix.

    The populated progress signal for an iter or a phase: the share of its
    child waves that are closed. Plain text only (no Rich markup) so the
    same string drops into a Rich-parsed tree label and into the modal.

    Args:
        done: Completed child count (e.g. closed waves). Negative inputs
            clamp to ``0``.
        total: Total child count. ``<= 0`` yields :data:`EMPTY_STATE` (an
            entity with no children has no completion to show).
        width: Bar cell count. Defaults to ``10`` (one cell per 10 %).

    Returns:
        A plain string of the form ``#####-----  3/6`` (50 %, 3 of 6
        done), or :data:`EMPTY_STATE` when *total* is non-positive.
    """
    if total <= 0:
        return EMPTY_STATE
    done_clamped = min(max(done, 0), total)
    fraction = done_clamped / total
    filled = int(fraction * width + 0.5)
    glyphs = GLYPH_FULL * filled + GLYPH_EMPTY * (width - filled)
    return f"{glyphs}  {done_clamped}/{total}"


def render_size_bar(bucket: str, *, width: int = 5) -> str:
    """Render an effort-bucket size bar (``XS``..``XL`` → 1..5 filled cells).

    The populated signal for a wave: its t-shirt effort bucket. The bucket
    maps to a filled-cell count via :data:`_BUCKET_CELLS`; an unrecognised
    bucket yields :data:`EMPTY_STATE` (the wave has no size to show).

    Args:
        bucket: The effort-bucket label (``"XS"`` / ``"S"`` / ``"M"`` /
            ``"L"`` / ``"XL"``). Any other value yields the empty state.
        width: Bar cell count. Defaults to ``5`` (one cell per bucket
            step). Buckets fill at most ``width`` cells.

    Returns:
        A plain string of the form ``###--  M`` (bucket ``M``), or
        :data:`EMPTY_STATE` for an unknown bucket.
    """
    cells = _BUCKET_CELLS.get(bucket)
    if cells is None:
        return EMPTY_STATE
    filled = min(cells, width)
    glyphs = GLYPH_FULL * filled + GLYPH_EMPTY * (width - filled)
    return f"{glyphs}  {bucket}"


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
    "EMPTY_STATE",
    "GLYPH_EMPTY",
    "GLYPH_FULL",
    "OK_THRESHOLD",
    "WARN_THRESHOLD",
    "EUBar",
    "band_var",
    "render_bar_markup",
    "render_bar_plain",
    "render_completion_bar",
    "render_eu_bar_plain",
    "render_size_bar",
]
