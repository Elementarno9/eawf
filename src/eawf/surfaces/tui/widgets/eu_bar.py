"""``EUBar`` — a 5-cell effort-unit progress bar (widget catalog).

A fixed-width 5-cell glyph bar that renders ``consumed_eu / total_eu``
as filled (``#``) vs empty (``-``) cells, colour-coded by the consumed
fraction:

* ``ok``   when consumed ≤ 80 % of total,
* ``warn`` when consumed ≤ 100 % of total,
* ``err``  when consumed > 100 % of total (over budget).

A trailing right-aligned percentage label sits after the bar. The widget
is a leaf :class:`~textual.widgets.Static` so it composes inline inside a
:class:`~eawf.surfaces.tui.widgets.roadmap_tree.RoadmapTree` row and stands
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

from eawf.surfaces.render.bars import render_block_bar
from eawf.surfaces.tui.widgets.status_tint import BAND_HEX
from eawf.workflow.estimation.thresholds import OK_BAND_CEILING, OVER_BUDGET_CEILING

#: Bar render mode: ``"unicode"`` uses the block-eighths fill
#: (:func:`~eawf.surfaces.render.bars.render_block_bar`, one-eighth-cell
#: sub-resolution); ``"ascii"`` is the ``#``/``-`` fallback for fonts
#: lacking block-glyph coverage or ``ui.glyphs=ascii``. The active mode
#: lives on :attr:`eawf.surfaces.tui.app.EaApp.render_mode`; every renderer
#: below honours it via a ``mode`` argument so a single flip rerenders every
#: bar.
RenderMode = Literal["unicode", "ascii"]

#: Default mode for the pure render helpers when no caller threads one
#: through. Kept ``"ascii"`` so a caller not yet wired to
#: :attr:`eawf.surfaces.tui.app.EaApp.render_mode` (a tree / status-pane row a
#: consuming wave has not yet updated) keeps the safe ``#``/``-`` set
#: rather than emitting block glyphs on a font that may lack coverage. The
#: live :class:`EUBar` widget and the App reactive seed ``"unicode"``
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

#: Completion-bar full-fill glyph (U+2588 FULL BLOCK) for the unicode
#: render mode. The closed-share of an iter / phase paints this; the
#: remainder paints :data:`COMPLETION_REMAINDER`.
COMPLETION_FULL: str = "█"

#: Completion-bar remainder glyph (U+2592 MEDIUM SHADE) for the unicode
#: render mode -- a dim shade so the unfilled tail reads as track rather
#: than empty space.
COMPLETION_REMAINDER: str = "▒"

#: Consumed-fraction upper bound (inclusive) for the ``ok`` colour band.
#: Aliases the canonical :data:`~eawf.workflow.estimation.thresholds.OK_BAND_CEILING`
#: so the gauge band and the daemon stale-wave advisory read one constant
#: -- the gauge hue and the over-budget modal can never drift apart.
OK_THRESHOLD: float = OK_BAND_CEILING

#: Consumed-fraction upper bound (inclusive) for the ``warn`` colour band;
#: anything above is ``err`` (over budget). Aliases the canonical
#: :data:`~eawf.workflow.estimation.thresholds.OVER_BUDGET_CEILING`.
WARN_THRESHOLD: float = OVER_BUDGET_CEILING

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
#: (:func:`~eawf.surfaces.tui.widgets.roadmap_tree._pin_bar_right`): an unpadded
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


def _ascii_glyphs(filled: int, width: int) -> str:
    """Return the ``#``/``-`` ASCII fill run of *width* cells."""
    return GLYPH_FULL * filled + GLYPH_EMPTY * (width - filled)


def _mode_glyphs(fraction: float, *, width: int, mode: RenderMode) -> str:
    """Return the glyph run for *fraction* in the active *mode*.

    The single fork between the unicode block-eighths fill and the ASCII
    fallback so every bar renderer below honours
    :attr:`eawf.surfaces.tui.app.EaApp.render_mode` through one code path -- a
    single flip rerenders the whole tree, status pane, and tables. The
    unicode mode paints the block-eighths bar
    (:func:`~eawf.surfaces.render.bars.render_block_bar`) at one-eighth-cell
    sub-resolution; the ASCII mode keeps the ``#``/``-`` fallback for fonts
    lacking block-glyph coverage.

    *fraction* is clamped into ``[0, 1]`` before the block-eighths render so
    an over-budget bar clamps to a full bar (the colour band carries the
    over-budget signal) and a negative fraction clamps to empty.

    Args:
        fraction: Consumed / total ratio (over-budget clamps to full;
            negative clamps to empty).
        width: Bar cell count.
        mode: ``"unicode"`` (the block-eighths fill) or ``"ascii"``.

    Returns:
        The block-eighths or ASCII glyph run, *width* cells wide.
    """
    if mode == "unicode":
        clamped = min(max(fraction, 0.0), 1.0)
        return render_block_bar(clamped, width=width)
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
    dividing by zero. The glyph run honours *mode* (unicode vs ASCII) so
    every caller flips with :attr:`eawf.surfaces.tui.app.EaApp.render_mode`.

    Args:
        consumed_eu: Effort units consumed so far (≥ 0).
        total_eu: Total estimated effort units (≥ 0).
        mode: Active render mode (``"unicode"`` or ``"ascii"``).

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
        mode: Active render mode (``"unicode"`` or ``"ascii"``). Threaded
            from :attr:`eawf.surfaces.tui.app.EaApp.render_mode`.

    Returns:
        A plain string of the form ``#####  120%`` (ASCII) or
        ``█████  120%`` (unicode).
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
        mode: Active render mode (``"unicode"`` or ``"ascii"``).

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
        mode: Active render mode (``"unicode"`` or ``"ascii"``).

    The unicode fill is a discrete ``█`` (:data:`COMPLETION_FULL`) run over a
    ``▒`` (:data:`COMPLETION_REMAINDER`) track so the unfilled tail reads as
    a dim track rather than blank space; the ASCII fallback keeps the
    ``#``/``-`` glyph set. A full ratio paints ``██████████`` (no remainder).

    Args:
        done: Completed child count (e.g. closed waves). Negative inputs
            clamp to ``0``.
        total: Total child count. ``<= 0`` yields :data:`EMPTY_STATE` (an
            entity with no children has no completion to show).
        width: Bar cell count. Defaults to ``10`` (one cell per 10 %).
        mode: Active render mode (``"unicode"`` or ``"ascii"``).

    Returns:
        A plain string of the form ``#####-----      3/6`` (ASCII, 50 %, 3 of
        6 done, counter right-aligned in a 7-cell field) or
        ``█████▒▒▒▒▒      3/6`` (unicode), or :data:`EMPTY_STATE` when *total*
        is non-positive.
    """
    if total <= 0:
        return EMPTY_STATE
    done_clamped = min(max(done, 0), total)
    fraction = done_clamped / total
    if mode == "unicode":
        filled = _fill_cells_width(fraction, width)
        glyphs = COMPLETION_FULL * filled + COMPLETION_REMAINDER * (width - filled)
    else:
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
        mode: Active render mode (``"unicode"`` or ``"ascii"``).

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
        mode: Active render mode (``"unicode"`` or ``"ascii"``). Threaded
            from :attr:`eawf.surfaces.tui.app.EaApp.render_mode`.

    Returns:
        A content-markup string of the form ``[$ok]#####[/]  120%`` (ASCII)
        or ``[$ok]█████[/]  120%`` (unicode).
    """
    glyphs, pct, band = _bar_parts(consumed_eu, total_eu, mode=mode)
    return f"[{band}]{glyphs}[/]  {pct}%"


#: Fallback EU-burn band colours (Wong dark palette), used when the active
#: theme cannot be resolved — e.g. an unmounted test harness. The live tint
#: comes from the active Theme's ``variables`` map at render time. Sourced
#: from the shared :data:`~eawf.surfaces.tui.widgets.status_tint.BAND_HEX` so the
#: ok/warn/err fallback hexes live in one home alongside the lifecycle
#: status-tint map (DRY: a palette retune lands in a single place).
DEFAULT_BAND_PALETTE: dict[str, str] = dict(BAND_HEX)


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
        mode: Active render mode (``"unicode"`` or ``"ascii"``).
        palette: Maps band keys (``"ok"`` / ``"warn"`` / ``"err"``) to a hex
            colour. Falls back to :data:`DEFAULT_BAND_PALETTE` when omitted
            or missing a key.

    Returns:
        A Rich-markup string of the form ``[#009e73]█████[/]  120%``.
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

    #: Active fill mode. Seeded ``"unicode"`` (the operator pick); the
    #: App's :meth:`eawf.surfaces.tui.app.EaApp.watch_render_mode` propagates a
    #: coverage-probe / ``ui.glyphs`` flip here. Watched so the propagated
    #: flip repaints the bar in the other glyph set.
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

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
        """Repaint when the fill mode flips (unicode <-> ASCII)."""
        self._repaint()

    def _repaint(self) -> None:
        """Re-render the bar from the current reactive values."""
        self.update(render_bar_markup(self.consumed_eu, self.total_eu, mode=self.render_mode))


__all__ = [
    "BAR_CELLS",
    "COMPLETION_FULL",
    "COMPLETION_REMAINDER",
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
    "render_bar_markup",
    "render_bar_plain",
    "render_bar_rich",
    "render_completion_bar",
    "render_eu_bar_plain",
    "render_size_bar",
]
