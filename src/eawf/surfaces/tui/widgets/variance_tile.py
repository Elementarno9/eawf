"""``VarianceTile`` — the C06 MetricsModal estimate-actual variance tile.

A leaf :class:`~textual.widgets.Static` that renders the C09 §5.9.6 M26
``eawf_estimate_actual_variance_pct`` gauge — ``(actual EU - planned EU) /
planned EU * 100`` — as a single signed-percentage value, colour-banded by
how far the work drifted from the estimate:

* ``ok``   when the absolute variance is within 25 % of the estimate,
* ``warn`` when within 50 %,
* ``err``  when the work ran more than 50 % over / under.

The widget is driven by one nullable float — :meth:`set_variance` — so it
stays decoupled from the :class:`~eawf.workflow.estimation.metrics.MetricsSummary`
schema; the caller maps the M26 metric onto the percentage. A ``None``
percentage (no closed waves with both an estimate and an actual) renders the
:data:`EMPTY_STATE` sentinel rather than a fabricated ``0%`` — mirroring the
"surface now, data later" contract the EU bar uses.

The colour bands are emitted as Textual content markup against the
``theme.tcss`` palette vars (``$ok`` / ``$warn`` / ``$err``) — never
hardcoded hex — so a runtime theme swap stays a CSS var rebind.
"""

from __future__ import annotations

from typing import ClassVar

from textual.reactive import reactive
from textual.widgets import Static

from eawf.surfaces.tui.widgets.eu_bar import RenderMode, render_bar_markup

#: Rendered when the variance is unset (no closed waves carry both an
#: estimate and an actual). Surfacing the sentinel — rather than a
#: fabricated 0 % — keeps the empty-state contract honest.
EMPTY_STATE: str = "— no data"

#: Cell count of the unified magnitude bar the tile paints alongside the
#: signed-percent label. Five cells mirror the EU bar's
#: :data:`~eawf.surfaces.tui.widgets.eu_bar.BAR_CELLS` so the variance gauge
#: reads at the same width as the effort gauge.
VARIANCE_BAR_CELLS: int = 5

#: Absolute-variance upper bound (inclusive, percent) for the ``ok`` band.
OK_THRESHOLD_PCT: float = 25.0

#: Absolute-variance upper bound (inclusive, percent) for the ``warn`` band;
#: anything beyond is ``err`` (the work drifted hard from the estimate).
WARN_THRESHOLD_PCT: float = 50.0


def band_var(variance_pct: float) -> str:
    """Return the ``theme.tcss`` palette var for *variance_pct*'s band.

    Bands key off the *absolute* variance so a large under-run is flagged as
    loudly as a large over-run — both signal a mis-calibrated estimate.

    Args:
        variance_pct: The signed M26 variance percentage.

    Returns:
        One of ``"$ok"`` / ``"$warn"`` / ``"$err"`` — palette vars resolved
        by Textual content markup at render time.
    """
    magnitude = abs(variance_pct)
    if magnitude <= OK_THRESHOLD_PCT:
        return "$ok"
    if magnitude <= WARN_THRESHOLD_PCT:
        return "$warn"
    return "$err"


def render_variance_plain(variance_pct: float | None) -> str:
    """Render the variance value as an uncoloured plain string.

    The colour-free counterpart to :func:`render_variance_markup`, for
    embedding in a Rich-markup context that cannot resolve the Textual
    palette vars (e.g. a Tree node label) and for snapshot assertions.

    Args:
        variance_pct: The signed M26 variance percentage, or ``None`` when
            no sample is available.

    Returns:
        A plain string of the form ``+12.5%`` / ``-3.0%``, or
        :data:`EMPTY_STATE` when *variance_pct* is ``None``.
    """
    if variance_pct is None:
        return EMPTY_STATE
    sign = "+" if variance_pct >= 0 else ""
    return f"{sign}{variance_pct:.1f}%"


def render_variance_markup(variance_pct: float | None) -> str:
    """Render the variance value as a colour-banded content-markup string.

    Pure helper so the value, sign, and colour band are unit-testable
    without mounting the widget. The value is wrapped in a
    ``[$ok|$warn|$err]…[/]`` span so Textual resolves the colour against the
    ``theme.tcss`` palette at render time. A ``None`` percentage renders the
    muted :data:`EMPTY_STATE` sentinel.

    Args:
        variance_pct: The signed M26 variance percentage, or ``None``.

    Returns:
        A content-markup string of the form ``[$warn]+12.5%[/]``, or the
        muted empty-state sentinel when *variance_pct* is ``None``.
    """
    if variance_pct is None:
        return f"[$text-muted]{EMPTY_STATE}[/]"
    return f"[{band_var(variance_pct)}]{render_variance_plain(variance_pct)}[/]"


def render_variance_bar_markup(variance_pct: float | None, *, mode: RenderMode = "unicode") -> str:
    """Render the variance magnitude as the unified block cell-bar + label.

    Reuses the unified EU cell-bar
    (:func:`~eawf.surfaces.tui.widgets.eu_bar.render_bar_markup`) to paint the
    *magnitude* of the drift: the absolute variance percent is the bar's
    "consumed" value against a full-scale ceiling of :data:`WARN_THRESHOLD_PCT`
    so a variance at the ``err`` boundary saturates the bar. The eu_bar
    renderer colour-bands the fill by the same consumed fraction, so the bar
    hue rides the ``ok`` / ``warn`` / ``err`` band the variance falls in. The
    signed-percent label follows the bar so the sign + value stay visible.

    A ``None`` percentage renders the muted :data:`EMPTY_STATE` sentinel
    rather than a fabricated empty bar -- the same honest-empty contract the
    unified bar enforces.

    Args:
        variance_pct: The signed M26 variance percentage, or ``None`` when
            no sample is available.
        mode: Active render mode (``"unicode"`` or ``"ascii"``). Threaded
            from :attr:`eawf.surfaces.tui.app.EaApp.render_mode`.

    Returns:
        A content-markup string of the form ``[$warn]██▒▒▒[/]  +30.0%``, or
        the muted empty-state sentinel when *variance_pct* is ``None``.
    """
    if variance_pct is None:
        return f"[$text-muted]{EMPTY_STATE}[/]"
    bar = render_bar_markup(abs(variance_pct), WARN_THRESHOLD_PCT, mode=mode)
    # render_bar_markup emits ``[band]glyphs[/]  pct%``; the variance's own
    # signed percent is the load-bearing label, so split off the closing
    # ``[/]  `` delimiter (whose glyph run may itself contain spaces) and
    # swap the trailing consumed-fraction percent for the signed value.
    band_span, _, _ = bar.rpartition("[/]  ")
    return f"{band_span}[/]  {render_variance_plain(variance_pct)}"


class VarianceTile(Static):
    """A colour-banded estimate-actual variance tile (M26).

    Standalone leaf widget: set the percentage via :meth:`set_variance` (or
    the reactive :attr:`variance_pct` directly in tests) and the tile
    repaints to the unified block cell-bar + signed-percent label. A ``None``
    value surfaces the empty-state sentinel rather than a fabricated bar.
    Mounted in the C06 ``MetricsModal`` variance cell and anywhere a compact
    M26 gauge is wanted.

    The bar follows :attr:`eawf.surfaces.tui.app.EaApp.render_mode`: the tile
    seeds its own :attr:`render_mode` reactive from the app on mount and
    repaints on a flip, so a single unicode <-> ASCII flip rerenders the bar.
    """

    DEFAULT_CSS: ClassVar[str] = """
    VarianceTile {
        height: 1;
        width: auto;
    }
    """

    #: The signed M26 variance percentage, or ``None`` when unset. Watched
    #: so assignment repaints the tile.
    variance_pct: reactive[float | None] = reactive[float | None](None)

    #: Active fill mode. Seeded from :attr:`eawf.surfaces.tui.app.EaApp.render_mode`
    #: on mount; the tile mirrors an app-level flip onto this reactive so a
    #: single unicode <-> ASCII flip repaints the bar. Watched so the mirrored
    #: flip rerenders in the other glyph set.
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def set_variance(self, variance_pct: float | None) -> None:
        """Set the variance percentage and repaint.

        Args:
            variance_pct: The signed M26 variance percentage, or ``None``
                when no closed wave carries both an estimate and an actual.
        """
        self.variance_pct = variance_pct

    def watch_variance_pct(self) -> None:
        """Repaint when the variance value changes."""
        self._repaint()

    def watch_render_mode(self) -> None:
        """Repaint when the fill mode flips (unicode <-> ASCII)."""
        self._repaint()

    def on_mount(self) -> None:
        """Seed the render mode from the app, watch it, and paint the bar."""
        app_mode = getattr(self.app, "render_mode", None)
        if app_mode is not None:
            self.render_mode = app_mode
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_app_render_mode)
        self._repaint()

    def _on_app_render_mode(self, mode: RenderMode) -> None:
        """Mirror an app-level render-mode flip onto this tile's reactive."""
        self.render_mode = mode

    def _repaint(self) -> None:
        """Re-render the unified magnitude bar from the current variance."""
        self.update(render_variance_bar_markup(self.variance_pct, mode=self.render_mode))


__all__ = [
    "EMPTY_STATE",
    "OK_THRESHOLD_PCT",
    "VARIANCE_BAR_CELLS",
    "WARN_THRESHOLD_PCT",
    "VarianceTile",
    "band_var",
    "render_variance_bar_markup",
    "render_variance_markup",
    "render_variance_plain",
]
