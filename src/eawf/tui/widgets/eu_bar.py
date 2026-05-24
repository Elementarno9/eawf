"""``EUBar`` — a 5-cell effort-unit progress bar (widget catalog).

A fixed-width 5-cell glyph bar that renders ``consumed_eu / total_eu``
as filled (``#``) vs empty (``-``) cells, colour-coded by the consumed
fraction:

* ``ok``   when consumed ≤ 80 % of total,
* ``warn`` when consumed ≤ 100 % of total,
* ``err``  when consumed > 100 % of total (over budget).

A trailing right-aligned percentage label sits after the bar. The widget
is a leaf :class:`~textual.widgets.Static` so it composes inline inside a
:class:`~eawf.tui.widgets.roadmap_tree.RoadmapTree` row and stands
alone in any pane. It is driven by two plain floats — :meth:`set_eu` — so
it stays decoupled from the :class:`~eawf.kernel.state.models.State` schema; the
caller maps state onto the consumed / total pair.

The fill maths use the same banker's-rounding-free integer cell count the
glyph bar needs (round-half-up via ``+ 0.5``) so a 1 % consumption still
lights the first cell rather than rounding away to an empty bar. The bar
glyphs are coloured via Textual content markup against the ``theme.tcss``
palette vars (``$ok`` / ``$warn`` / ``$err``) — never hardcoded hex — so
the runtime theme swap stays a CSS var rebind.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Literal

from textual.reactive import reactive
from textual.widgets import Static

#: Bar render mode: ``"braille"`` uses the Braille-Patterns dot-matrix
#: fill (2x horizontal sub-resolution per cell); ``"ascii"`` is the
#: ``#``/``-`` fallback for fonts lacking Braille coverage or
#: ``ui.glyphs=ascii``. The active mode lives on
#: :attr:`eawf.tui.app.EaApp.render_mode`; every renderer below honours it
#: via a ``mode`` argument so a single flip rerenders every bar.
RenderMode = Literal["braille", "ascii"]

#: Default mode for the pure render helpers when no caller threads one
#: through. Kept ``"ascii"`` so a caller not yet wired to
#: :attr:`eawf.tui.app.EaApp.render_mode` (a tree / status-pane row a
#: consuming wave has not yet updated) keeps the safe ``#``/``-`` set
#: rather than emitting Braille on a font that may lack coverage. The
#: live :class:`EUBar` widget and the App reactive seed ``"braille"``
#: (the operator pick) explicitly and propagate the flip.
DEFAULT_RENDER_MODE: RenderMode = "ascii"

#: Number of glyph cells in the bar. Fixed at 5
#: (``5-cell glyph bar``); the inline roadmap variant relies on this
#: constant width so tree rows align.
BAR_CELLS: int = 5

#: Filled-cell glyph (Nerd-Font-always per the brief glyph set; the
#: ``--plain`` ASCII fallback uses the same ``#`` so no swap is needed).
GLYPH_FULL: str = "#"

#: Empty-cell glyph.
GLYPH_EMPTY: str = "-"

#: Braille Patterns block base code point (U+2800 — the all-dots-off
#: cell). Every Braille glyph in the bar is this base OR-ed with the dot
#: bits for the sub-columns that are filled.
BRAILLE_BASE: int = 0x2800

#: Dot-bit mask lighting every dot in a Braille cell's LEFT column
#: (dots 1·2·3·7). OR-ing this onto :data:`BRAILLE_BASE` fills the left
#: half of one cell — the first of the two horizontal sub-columns.
BRAILLE_LEFT_COL: int = 0x47

#: Dot-bit mask lighting every dot in a Braille cell's RIGHT column
#: (dots 4·5·6·8). OR-ing this onto :data:`BRAILLE_BASE` fills the right
#: half of one cell — the second of the two horizontal sub-columns.
BRAILLE_RIGHT_COL: int = 0xB8

#: Horizontal sub-columns each Braille cell encodes. The bar resolves to
#: ``BAR_CELLS * BRAILLE_SUBCOLS`` fillable sub-units -- 2x the cell count.
BRAILLE_SUBCOLS: int = 2

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

#: Fixed field width for the right-aligned ``done/total`` completion
#: counter — three digits, a slash, three digits (``###/###``). The whole
#: counter is right-aligned into this field so every completion bar's
#: counter lands in the same columns. The width is a minimum: a 4-digit
#: total overflows the field gracefully rather than truncating.
_COUNTER_FIELD_WIDTH: int = 7

#: Fixed field width for the right-aligned size-bar bucket label. Buckets
#: are one (``S`` / ``M`` / ``L``) or two (``XS`` / ``XL``) characters, so
#: right-justifying every label into a 2-cell field keeps the rendered bar
#: string a constant length across all buckets. That matters because the
#: roadmap tree pins the whole bar string flush-right
#: (:func:`~eawf.tui.widgets.roadmap_tree._pin_bar_right`): an unpadded
#: label would make a 2-char-bucket row one cell longer, shifting its glyph
#: run one column left of the 1-char rows. Mirrors :data:`_COUNTER_FIELD_WIDTH`.
_BUCKET_FIELD_WIDTH: int = 2


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


def _braille_subcells(fraction: float, *, width: int) -> int:
    """Return the filled sub-column count for *fraction* of a Braille bar.

    A Braille bar of *width* cells resolves to ``width *
    BRAILLE_SUBCOLS`` fillable horizontal sub-columns (2x the cell count).
    The fraction is clamped into ``[0, 1]`` and rounded half-up against
    the sub-column grid, so the first sub-column lights once the fraction
    reaches half a sub-column (``0.5 / (width * BRAILLE_SUBCOLS)``).

    Args:
        fraction: Consumed / total ratio (over-budget clamps to full;
            negative clamps to empty).
        width: Bar cell count (≥ 0).

    Returns:
        Integer sub-column count in ``[0, width * BRAILLE_SUBCOLS]``.
    """
    clamped = min(max(fraction, 0.0), 1.0)
    subcols = width * BRAILLE_SUBCOLS
    return int(clamped * subcols + 0.5)


def render_bar_braille(fraction: float, *, width: int = BAR_CELLS) -> str:
    """Render a Braille dot-matrix fill bar for *fraction*.

    Fills left-to-right via the Braille Patterns block (U+2800-U+28FF) at
    2x horizontal sub-resolution: each of the *width* cells encodes two
    horizontal sub-columns (the cell's left dot column then its right dot
    column), so the bar resolves to ``width * BRAILLE_SUBCOLS`` fillable
    sub-units. A whole cell to the left of the fill front is the all-dots
    glyph U+28FF; a half-filled cell shows only its left column
    (U+2847); an empty cell is U+2800.

    This is the glyph string only — it carries no colour. The caller wraps
    it in a status-tint span (see :func:`render_bar_braille_markup`) so the
    bar hue matches the row's lifecycle/status glyph hue.

    Args:
        fraction: Consumed / total ratio (over-budget clamps to a full
            bar; negative clamps to empty). The colour band carries the
            over-budget signal, not the glyph run.
        width: Bar cell count. Defaults to :data:`BAR_CELLS`.

    Returns:
        A Braille glyph run of exactly *width* characters.
    """
    filled = _braille_subcells(fraction, width=width)
    full_cells, half = divmod(filled, BRAILLE_SUBCOLS)
    cells = [chr(BRAILLE_BASE | BRAILLE_LEFT_COL | BRAILLE_RIGHT_COL)] * full_cells
    if half:
        cells.append(chr(BRAILLE_BASE | BRAILLE_LEFT_COL))
    cells += [chr(BRAILLE_BASE)] * (width - len(cells))
    return "".join(cells)


def _ascii_glyphs(filled: int, width: int) -> str:
    """Return the ``#``/``-`` ASCII fill run of *width* cells."""
    return GLYPH_FULL * filled + GLYPH_EMPTY * (width - filled)


def _mode_glyphs(fraction: float, *, width: int, mode: RenderMode) -> str:
    """Return the glyph run for *fraction* in the active *mode*.

    The single fork between the Braille and ASCII fill so every bar
    renderer below honours :attr:`eawf.tui.app.EaApp.render_mode` through
    one code path — a single flip rerenders the whole tree, status pane,
    and tables.

    Args:
        fraction: Consumed / total ratio.
        width: Bar cell count.
        mode: ``"braille"`` or ``"ascii"``.

    Returns:
        The Braille or ASCII glyph run, *width* cells wide.
    """
    if mode == "braille":
        return render_bar_braille(fraction, width=width)
    return _ascii_glyphs(_fill_cells_width(fraction, width), width)


def _fill_cells_width(fraction: float, width: int) -> int:
    """Return the filled ASCII cell count for *fraction* over *width* cells.

    The width-parametrised twin of :func:`_fill_cells` (which is fixed at
    :data:`BAR_CELLS`); shares the same clamp + round-half-up maths.

    Args:
        fraction: Consumed / total ratio (clamped to ``[0, 1]``).
        width: Bar cell count.

    Returns:
        Integer cell count in ``[0, width]``.
    """
    clamped = min(max(fraction, 0.0), 1.0)
    return int(clamped * width + 0.5)


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


def _bar_parts(consumed_eu: float, total_eu: float, *, mode: RenderMode) -> tuple[str, int, str]:
    """Return ``(glyphs, pct, band_var)`` for a consumed / total pair.

    Single source of the fill maths shared by the plain + markup
    renderers. A zero *total_eu* yields an empty bar at ``0%`` rather than
    dividing by zero. The glyph run honours *mode* (Braille vs ASCII) so
    every caller flips with :attr:`eawf.tui.app.EaApp.render_mode`.

    Args:
        consumed_eu: Effort units consumed so far (≥ 0).
        total_eu: Total estimated effort units (≥ 0).
        mode: Active render mode (``"braille"`` or ``"ascii"``).

    Returns:
        The glyph run, the integer percentage, and the palette colour var.
    """
    fraction = consumed_eu / total_eu if total_eu > 0 else 0.0
    glyphs = _mode_glyphs(fraction, width=BAR_CELLS, mode=mode)
    pct = int(fraction * 100 + 0.5)
    return glyphs, pct, band_var(fraction)


def render_bar_plain(
    consumed_eu: float, total_eu: float, *, mode: RenderMode = DEFAULT_RENDER_MODE
) -> str:
    """Render the bar + trailing percent as an uncoloured plain string.

    The colour-free counterpart to :func:`render_bar_markup`, for
    embedding the bar inside a Rich-markup context (e.g. a
    :class:`~textual.widgets.Tree` node label, which is parsed by Rich and
    does not resolve the Textual ``$`` palette vars). The glyph itself
    carries the fill signal; the host row's status glyph carries colour.

    Args:
        consumed_eu: Effort units consumed so far (≥ 0).
        total_eu: Total estimated effort units (≥ 0).
        mode: Active render mode (``"braille"`` or ``"ascii"``). Threaded
            from :attr:`eawf.tui.app.EaApp.render_mode`.

    Returns:
        A plain string of the form ``#####  120%`` (ASCII) or
        ``⣿⣿⣿⣿⣿  120%`` (Braille).
    """
    glyphs, pct, _ = _bar_parts(consumed_eu, total_eu, mode=mode)
    return f"{glyphs}  {pct}%"


def render_eu_bar_plain(
    consumed_eu: float, total_eu: float, *, mode: RenderMode = DEFAULT_RENDER_MODE
) -> str:
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
        mode: Active render mode (``"braille"`` or ``"ascii"``).

    Returns:
        The plain bar string, or :data:`EMPTY_STATE` when *total_eu* is
        non-positive.
    """
    if total_eu <= 0:
        return EMPTY_STATE
    return render_bar_plain(consumed_eu, total_eu, mode=mode)


def render_completion_bar(
    done: int,
    total: int,
    *,
    width: int = 10,
    mode: RenderMode = DEFAULT_RENDER_MODE,
) -> str:
    """Render a ``done / total`` completion ratio bar with a count suffix.

    The populated progress signal for an iter or a phase: the share of its
    child waves that are closed. Plain text only (no Rich markup) so the
    same string drops into a Rich-parsed tree label and into the modal.

    The ``done/total`` counter is right-aligned into a fixed
    :data:`_COUNTER_FIELD_WIDTH`-cell field (``###/###``), so every bar's
    counter lands in the same columns regardless of each row's digit count.
    The width is a minimum: a 4-digit total overflows the field gracefully
    rather than truncating.

    Args:
        done: Completed child count (e.g. closed waves). Negative inputs
            clamp to ``0``.
        total: Total child count. ``<= 0`` yields :data:`EMPTY_STATE` (an
            entity with no children has no completion to show).
        width: Bar cell count. Defaults to ``10`` (one cell per 10 %).
        mode: Active render mode (``"braille"`` or ``"ascii"``).

    Returns:
        A plain string of the form ``#####-----      3/6`` (50 %, 3 of 6
        done, counter right-aligned in a 7-cell field), or :data:`EMPTY_STATE`
        when *total* is non-positive.
    """
    if total <= 0:
        return EMPTY_STATE
    done_clamped = min(max(done, 0), total)
    fraction = done_clamped / total
    glyphs = _mode_glyphs(fraction, width=width, mode=mode)
    counter = f"{done_clamped}/{total}"
    return f"{glyphs}  {counter:>{_COUNTER_FIELD_WIDTH}}"


def render_size_bar(bucket: str, *, width: int = 5, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Render an effort-bucket size bar (``XS``..``XL`` → 1..5 filled cells).

    The populated signal for a wave: its t-shirt effort bucket. The bucket
    maps to a filled-cell count via :data:`_BUCKET_CELLS`; an unrecognised
    bucket yields :data:`EMPTY_STATE` (the wave has no size to show).

    The bucket label is right-justified into a fixed
    :data:`_BUCKET_FIELD_WIDTH`-cell field so the rendered string is a
    constant length across all buckets — a tree row that pins the bar
    flush-right then keeps every bar's glyph run in the same column
    regardless of whether the bucket is one char (``S`` / ``M`` / ``L``) or
    two (``XS`` / ``XL``).

    Args:
        bucket: The effort-bucket label (``"XS"`` / ``"S"`` / ``"M"`` /
            ``"L"`` / ``"XL"``). Any other value yields the empty state.
        width: Bar cell count. Defaults to ``5`` (one cell per bucket
            step). Buckets fill at most ``width`` cells.
        mode: Active render mode (``"braille"`` or ``"ascii"``).

    Returns:
        A plain string of the form ``###--   M`` / ``#----  XS`` (label
        right-aligned in a 2-cell field), or :data:`EMPTY_STATE` for an
        unknown bucket.
    """
    cells = _BUCKET_CELLS.get(bucket)
    if cells is None:
        return EMPTY_STATE
    filled = min(cells, width)
    fraction = filled / width if width > 0 else 0.0
    glyphs = _mode_glyphs(fraction, width=width, mode=mode)
    return f"{glyphs}  {bucket:>{_BUCKET_FIELD_WIDTH}}"


def render_bar_markup(
    consumed_eu: float, total_eu: float, *, mode: RenderMode = DEFAULT_RENDER_MODE
) -> str:
    """Render the bar + trailing percent as a Textual content-markup string.

    Pure helper so the glyph string, colour band, and percentage are
    unit-testable without mounting the widget. The glyph run is wrapped in
    a ``[$ok|$warn|$err]…[/]`` content-markup span so Textual resolves the
    colour against the ``theme.tcss`` palette at render time — the bar hue
    equals the row's status hue (status-tinted).

    Use :func:`render_bar_plain` instead when embedding the bar in a
    Rich-markup context (Tree labels), which cannot resolve the palette
    vars.

    Args:
        consumed_eu: Effort units consumed so far (≥ 0).
        total_eu: Total estimated effort units (≥ 0).
        mode: Active render mode (``"braille"`` or ``"ascii"``). Threaded
            from :attr:`eawf.tui.app.EaApp.render_mode`.

    Returns:
        A content-markup string of the form ``[$ok]#####[/]  120%`` (ASCII)
        or ``[$ok]⣿⣿⣿⣿⣿[/]  120%`` (Braille).
    """
    glyphs, pct, band = _bar_parts(consumed_eu, total_eu, mode=mode)
    return f"[{band}]{glyphs}[/]  {pct}%"


#: Fallback EU-burn band colours (Wong dark palette), used when the active
#: theme cannot be resolved — e.g. an unmounted test harness. The live tint
#: comes from the active Theme's ``variables`` map at render time.
DEFAULT_BAND_PALETTE: dict[str, str] = {
    "ok": "#009e73",
    "warn": "#e69f00",
    "err": "#d55e00",
}


def render_bar_rich(
    consumed_eu: float,
    total_eu: float,
    *,
    mode: RenderMode = DEFAULT_RENDER_MODE,
    palette: Mapping[str, str] | None = None,
) -> str:
    """Render the bar + percent as Rich-parseable hex-tinted markup.

    The Rich-context counterpart to :func:`render_bar_markup`: the colour
    band is resolved to a concrete ``#rrggbb`` from *palette* and emitted as
    a ``[#rrggbb]…[/]`` span, which Rich's markup parser understands. Use
    this — not :func:`render_bar_markup` — inside a Rich-parsed cell such as
    a :class:`textual.widgets.DataTable` ``str`` cell, which renders through
    :meth:`rich.text.Text.from_markup` and so cannot resolve the Textual
    ``$ok`` / ``$warn`` / ``$err`` palette vars (they raise ``MarkupError``).

    Args:
        consumed_eu: Effort units consumed so far (≥ 0).
        total_eu: Total estimated effort units (≥ 0).
        mode: Active render mode (``"braille"`` or ``"ascii"``).
        palette: Maps band keys (``"ok"`` / ``"warn"`` / ``"err"``) to a hex
            colour. Falls back to :data:`DEFAULT_BAND_PALETTE` when omitted
            or missing a key.

    Returns:
        A Rich-markup string of the form ``[#009e73]⣿⣿⣿⣿⣿[/]  120%``.
    """
    glyphs, pct, band = _bar_parts(consumed_eu, total_eu, mode=mode)
    key = band.removeprefix("$")
    colours = palette or DEFAULT_BAND_PALETTE
    hex_colour = colours.get(key, DEFAULT_BAND_PALETTE[key])
    return f"[{hex_colour}]{glyphs}[/]  {pct}%"


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

    #: Active fill mode. Seeded ``"braille"`` (the operator pick); the
    #: App's :meth:`eawf.tui.app.EaApp.watch_render_mode` propagates a
    #: coverage-probe / ``ui.glyphs`` flip here. Watched so the propagated
    #: flip repaints the bar in the other glyph set.
    render_mode: reactive[RenderMode] = reactive[RenderMode]("braille")

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

    def watch_render_mode(self) -> None:
        """Repaint when the fill mode flips (Braille ↔ ASCII)."""
        self._repaint()

    def _repaint(self) -> None:
        """Re-render the bar from the current reactive values."""
        self.update(render_bar_markup(self.consumed_eu, self.total_eu, mode=self.render_mode))


__all__ = [
    "BAR_CELLS",
    "BRAILLE_BASE",
    "BRAILLE_LEFT_COL",
    "BRAILLE_RIGHT_COL",
    "BRAILLE_SUBCOLS",
    "DEFAULT_BAND_PALETTE",
    "DEFAULT_RENDER_MODE",
    "EMPTY_STATE",
    "GLYPH_EMPTY",
    "GLYPH_FULL",
    "OK_THRESHOLD",
    "WARN_THRESHOLD",
    "EUBar",
    "RenderMode",
    "band_var",
    "render_bar_braille",
    "render_bar_markup",
    "render_bar_plain",
    "render_bar_rich",
    "render_completion_bar",
    "render_eu_bar_plain",
    "render_size_bar",
]
