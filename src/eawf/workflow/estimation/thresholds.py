"""Shared over-budget band thresholds and the wave time-budget projection.

This module is the single home for the consumed-fraction band boundaries
that both the TUI effort gauge (``eu_bar``) and the daemon stale-wave
advisory read, so a gauge band and a modal advisory can never drift apart:
they classify against the SAME two constants here.

* :data:`OK_BAND_CEILING` (0.80) is the inclusive upper bound of the
  ``ok`` band -- at or below it the gauge is green and no advisory fires.
* :data:`OVER_BUDGET_CEILING` (1.00) is the inclusive upper bound of the
  ``warn`` band -- above it the gauge is ``err`` (over budget).

It also owns :func:`wave_budget_minutes`, the one wave-id ->
pessimistic-minutes projection (estimate row first, else the
effort-bucket EU default), so every elapsed-time reader -- the gauge, the
digest publisher, and the stale detector -- shares one budget semantics
instead of re-deriving it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

#: Inclusive upper bound of the ``ok`` consumed-fraction band. At or below
#: this the gauge renders green and the stale advisory stays quiet; above
#: it the band is ``warn``. Canonical home -- ``eu_bar.OK_THRESHOLD``
#: aliases this so the gauge and the stale modal share one boundary.
OK_BAND_CEILING: Final[float] = 0.80

#: Inclusive upper bound of the ``warn`` consumed-fraction band. At or
#: below this (but above :data:`OK_BAND_CEILING`) the gauge is amber;
#: above it the band is ``err`` (over budget). Canonical home --
#: ``eu_bar.WARN_THRESHOLD`` aliases this.
OVER_BUDGET_CEILING: Final[float] = 1.00

#: The three over-budget bands, smallest-fraction first. ``ok`` is below
#: any advisory; ``warn`` is the 0.8x soft-over advisory; ``err`` is the
#: 1.0x hard-over advisory.
OverBudgetBand = Literal["ok", "warn", "err"]


def classify_band(fraction: float) -> OverBudgetBand:
    """Return the over-budget band for a consumed *fraction*.

    Shares the exact ``<=`` boundary semantics the gauge colour uses
    (:func:`eawf.surfaces.tui.widgets.eu_bar.band_var`), so a fraction that
    paints the gauge amber classifies ``warn`` here and a fraction that
    paints it red classifies ``err`` -- the gauge hue and the advisory band
    are the same decision.

    Args:
        fraction: Consumed / budget ratio (``>= 0``; may exceed ``1.0``
            when over budget).

    Returns:
        ``"ok"`` at or below :data:`OK_BAND_CEILING`, ``"warn"`` at or
        below :data:`OVER_BUDGET_CEILING`, else ``"err"``.
    """
    if fraction <= OK_BAND_CEILING:
        return "ok"
    if fraction <= OVER_BUDGET_CEILING:
        return "warn"
    return "err"


def wave_budget_minutes(state: State, wave_id: str) -> float | None:
    """Return a wave's pessimistic time budget in minutes, or ``None``.

    The single wave-id -> budget-minutes projection shared by the gauge,
    the digest elapsed publisher, and the stale-wave advisory. Prefers an
    explicit estimate row's ``pessimistic_minutes`` and falls back to the
    effort-bucket EU default (``wave_estimate_eu * EU_MINUTES``). Returns
    ``None`` when the wave is absent, has no effort bucket, and has no
    positive estimate -- the caller then has no budget to band against and
    leans on the absolute backstop instead.

    Args:
        state: Loaded typed state snapshot (read-only).
        wave_id: The wave whose time budget is wanted.

    Returns:
        The pessimistic budget in minutes (``> 0``), or ``None`` when no
        budget can be projected.
    """
    estimates = state.estimates or {}
    estimate = estimates.get(wave_id)
    if estimate is not None and estimate.pessimistic_minutes > 0:
        return estimate.pessimistic_minutes
    wave = state.waves.get(wave_id)
    if wave is None or wave.effort_bucket is None:
        return None
    from eawf.workflow.estimation.buckets import EU_MINUTES, wave_estimate_eu

    minutes = wave_estimate_eu(wave) * EU_MINUTES
    return minutes if minutes > 0 else None


__all__ = [
    "OK_BAND_CEILING",
    "OVER_BUDGET_CEILING",
    "OverBudgetBand",
    "classify_band",
    "wave_budget_minutes",
]
