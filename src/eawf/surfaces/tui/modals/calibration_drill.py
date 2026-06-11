"""``CalibrationDrillModal`` -- the jury-calibration detail overlay.

Drills into the jury calibration set: a small centred
:class:`~textual.screen.ModalScreen` that renders the **Brier score** and
the **expected calibration error (ECE)** over the jury's graded
predictions, plus the sample count behind them. ``Esc`` closes.

The jury is idle in v0.5 (no graded predictions land yet), so the COMMON
path is honest-empty: when no calibration set is supplied the overlay
renders ``no calibration set yet`` rather than a fabricated zero score. The
numbers appear only once a calibration set is actually bound.

The metric content is assembled by pure module functions
(:func:`render_calibration_lines`) over a typed
:class:`CalibrationSet`, so it is unit-testable without mounting Textual;
the screen is a thin scrollable view over them. The modal holds no domain
logic: it presents a calibration set (or its absence) and renders the
metrics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE
from eawf.surfaces.tui.widgets.sigils import chrome

if TYPE_CHECKING:
    from eawf.observability.eval.jury_validation import JuryValidationReport

logger = logging.getLogger(__name__)

#: Rendered when no jury calibration set is bound -- the COMMON path in
#: v0.5 (the jury is idle, so no graded predictions exist yet). Phrased so
#: the absence reads as "not measured yet", never a fabricated zero score.
NO_CALIBRATION_NOTICE: str = "no calibration set yet"


@dataclass(frozen=True)
class CalibrationSet:
    """A jury calibration set's Brier score + ECE over graded predictions.

    Attributes:
        brier_score: The mean squared error between the jury's predicted
            probabilities and the realised outcomes (lower is better, in
            ``[0, 1]``).
        ece: The expected calibration error -- the gap between predicted
            confidence and observed accuracy, bucket-averaged (lower is
            better, in ``[0, 1]``).
        sample_count: The number of graded predictions behind the metrics
            (the honest n -- a one-sample Brier is not yet a signal).
    """

    brier_score: float
    ece: float
    sample_count: int


def calibration_set_from_report(report: JuryValidationReport) -> CalibrationSet | None:
    """Bind a scored jury-validation report to a real :class:`CalibrationSet`.

    The drill surfaces the Brier score + ECE the jury-validation reducer
    (:func:`~eawf.observability.eval.jury_validation.validate_jury`) computed,
    so its calibration set IS that report's metrics -- never a fabricated zero.
    A report that refused to score (``status is INSUFFICIENT``, every numeric
    field ``None``) yields ``None`` so the drill renders its honest-empty
    notice rather than reading a number out of a starved cohort.

    Args:
        report: The jury-validation report from
            :func:`~eawf.observability.eval.jury_validation.validate_jury`.

    Returns:
        A :class:`CalibrationSet` carrying the report's Brier + ECE + cohort
        ``n`` when the report scored, or ``None`` when the cohort refused to
        score (insufficient signal).
    """
    if report.brier is None or report.ece is None:
        return None
    return CalibrationSet(
        brier_score=report.brier,
        ece=report.ece,
        sample_count=report.n,
    )


def render_calibration_lines(calibration: CalibrationSet | None) -> tuple[str, ...]:
    """Return the calibration-metric lines for *calibration*.

    Renders the Brier score, the ECE, and the sample count when a
    calibration set is bound, so the operator reads both the metrics and the
    n behind them. ``None`` -- the COMMON path while the jury is idle --
    yields a single :data:`NO_CALIBRATION_NOTICE` line rather than a
    fabricated zero score.

    Args:
        calibration: The jury calibration set, or ``None`` when none exists.

    Returns:
        The metric lines, or a one-element tuple carrying the honest-empty
        notice when no calibration set is bound.
    """
    if calibration is None:
        return (NO_CALIBRATION_NOTICE,)
    return (
        f"Brier score {calibration.brier_score:.3f}",
        f"ECE {calibration.ece:.3f}",
        f"samples {calibration.sample_count}",
    )


class CalibrationDrillModal(ModalScreen[None]):
    """Jury-calibration overlay: Brier + ECE over graded predictions (Esc closes).

    Renders the Brier score, the ECE, and the sample count in a scrollable
    card -- or the honest-empty :data:`NO_CALIBRATION_NOTICE` while the jury
    is idle and no calibration set is bound. Built thin over the pure
    :func:`render_calibration_lines` helper so the content is testable
    without Textual.
    """

    #: One calibration drill at a time: a re-fired drill over an already-open
    #: calibration drill is deduped by
    #: :meth:`~eawf.surfaces.tui.app.EaApp.push_modal` on the dedupe key.
    dedupe_singleton: ClassVar[bool] = False

    DEFAULT_CSS: ClassVar[str] = """
    CalibrationDrillModal {
        align: center middle;
    }
    CalibrationDrillModal > #calibration-drill-box {
        width: 60%;
        max-width: 70;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    CalibrationDrillModal .calibration-drill-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    CalibrationDrillModal .calibration-drill-row {
        height: auto;
        color: $text;
    }
    CalibrationDrillModal .calibration-drill-hint {
        color: $text-muted;
        margin-top: 1;
        height: 1;
    }
    """

    #: ``Esc`` closes the drill overlay; the only binding it owns.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(self, calibration: CalibrationSet | None = None) -> None:
        """Construct the calibration drill over the jury *calibration* set.

        Args:
            calibration: The jury calibration set, or ``None`` (the common
                v0.5 path -- the jury is idle) so the modal renders the
                honest-empty notice.
        """
        super().__init__()
        self._calibration = calibration
        #: Dedupe key so the App push chokepoint suppresses a duplicate
        #: calibration drill while one is already open.
        self.dedupe_key = "calibration-drill"

    def compose(self) -> ComposeResult:
        """Yield the scrollable drill card with the calibration metrics."""
        gate = chrome("gate", mode=getattr(self.app, "render_mode", DEFAULT_RENDER_MODE))
        with VerticalScroll(id="calibration-drill-box"):
            yield Static(
                f"[$accent]{gate}[/] jury calibration: Brier + ECE",
                classes="calibration-drill-title",
            )
            for line in render_calibration_lines(self._calibration):
                yield Static(f"  {line}", classes="calibration-drill-row")
            yield Static("[ Esc to close ]", classes="calibration-drill-hint")

    def action_close(self) -> None:
        """Dismiss the calibration drill overlay (``Esc``)."""
        logger.info(f"calibration_drill_close bound={self._calibration is not None}")
        self.dismiss(None)


__all__ = [
    "NO_CALIBRATION_NOTICE",
    "CalibrationDrillModal",
    "CalibrationSet",
    "calibration_set_from_report",
    "render_calibration_lines",
]
