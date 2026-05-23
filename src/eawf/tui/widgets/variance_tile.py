"""``VarianceTile`` — the C06 MetricsModal estimate-actual variance tile.

A leaf :class:`~textual.widgets.Static` that renders the C09 §5.9.6 M26
``eawf_estimate_actual_variance_pct`` gauge — ``(actual EU - planned EU) /
planned EU * 100`` — as a single signed-percentage value, colour-banded by
how far the work drifted from the estimate:

* ``ok``   when the absolute variance is within 25 % of the estimate,
* ``warn`` when within 50 %,
* ``err``  when the work ran more than 50 % over / under.

The widget is driven by one nullable float — :meth:`set_variance` — so it
stays decoupled from the :class:`~eawf.estimation.metrics.MetricsSummary`
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

#: Rendered when the variance is unset (no closed waves carry both an
#: estimate and an actual). Surfacing the sentinel — rather than a
#: fabricated 0 % — keeps the empty-state contract honest.
EMPTY_STATE: str = "— no data"

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


class VarianceTile(Static):
    """A colour-banded estimate-actual variance tile (M26).

    Standalone leaf widget: set the percentage via :meth:`set_variance` (or
    the reactive :attr:`variance_pct` directly in tests) and the tile
    repaints. A ``None`` value surfaces the empty-state sentinel. Mounted in
    the C06 ``MetricsModal`` variance cell and anywhere a compact M26 gauge
    is wanted.
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

    def set_variance(self, variance_pct: float | None) -> None:
        """Set the variance percentage and repaint.

        Args:
            variance_pct: The signed M26 variance percentage, or ``None``
                when no closed wave carries both an estimate and an actual.
        """
        self.variance_pct = variance_pct

    def watch_variance_pct(self) -> None:
        """Repaint when the variance value changes."""
        self.update(render_variance_markup(self.variance_pct))


__all__ = [
    "EMPTY_STATE",
    "OK_THRESHOLD_PCT",
    "WARN_THRESHOLD_PCT",
    "VarianceTile",
    "band_var",
    "render_variance_markup",
    "render_variance_plain",
]
