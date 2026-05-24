"""Retroactive ``ActualSummary`` backfill for historical CLOSED waves.

W25 wired :func:`eawf.workflow.lifecycle.wave.close_wave` to auto-record an
:class:`~eawf.kernel.state.models.ActualSummary` from the open->close wall-clock
span going forward. The waves that closed *before* that wiring landed carry
``opened_at``/``closed_at`` but no actual, so
:func:`~eawf.workflow.estimation.metrics.compute_estimate_actual_variance` and
:func:`~eawf.workflow.estimation.buckets.calibrate_buckets` have no historical
samples to fit against — they report "no data" despite hundreds of closed
waves on disk.

:func:`backfill_actuals` derives the missing actuals retroactively. It is a
**pure** transform over a typed :class:`~eawf.kernel.state.models.State` — no file
IO, no locks, no event append — so the orchestrator runs it through the
canonical writer path (AGENTS rule 4 / D-SUP-01) rather than letting this
module touch ``state.json`` directly.

The derivation reuses the W25 helper
:func:`~eawf.workflow.estimation.buckets.actual_summary_from_timestamps`, anchoring
each actual's ``updated_at`` to the wave's own ``closed_at`` so a wave that
finished months ago lands outside the 90-day calibration / 7-day burn
windows exactly as it would have under the live close path. The transform
is idempotent: a wave that already carries an actual is skipped, so a second
run adds nothing.
"""

from __future__ import annotations

import logging

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import State
from eawf.workflow.estimation.buckets import actual_summary_from_timestamps

logger = logging.getLogger(__name__)


def backfill_actuals(state: State) -> tuple[State, int]:
    """Attach retroactive actuals to CLOSED waves missing one (idempotent).

    Iterates every CLOSED wave and derives an
    :class:`~eawf.kernel.state.models.ActualSummary` from its
    ``opened_at``/``closed_at`` span via
    :func:`~eawf.workflow.estimation.buckets.actual_summary_from_timestamps`,
    anchoring the actual's ``updated_at`` to the wave's ``closed_at`` so the
    historical close lands in the same calibration / burn windows the live
    close path would have placed it. A wave contributes a new actual when:

    1. ``wave.status == WaveStatus.CLOSED``.
    2. ``state.actuals`` does not already carry an entry for ``wave.id`` —
       the idempotence guard, so a re-run never double-writes or alters an
       existing actual.
    3. The helper derives a non-``None`` actual — i.e. both timestamps are
       set and the span is positive. A closed wave with missing/None
       ``opened_at``/``closed_at`` or a non-positive span is skipped
       gracefully rather than raising.

    The transform mutates *state* in place (and returns it) so the caller
    can thread the result straight into the canonical writer; it never reads
    or writes disk itself.

    Args:
        state: Loaded typed :class:`State` snapshot to backfill.

    Returns:
        The mutated state and the count of actuals newly added (``0`` on a
        re-run once every eligible wave already carries one).
    """
    existing = state.actuals or {}
    added = 0
    for wave in state.waves.values():
        if wave.status != WaveStatus.CLOSED:
            continue
        if wave.id in existing:
            continue
        if wave.closed_at is None:
            continue
        actual = actual_summary_from_timestamps(wave, now=wave.closed_at)
        if actual is None:
            continue
        existing[wave.id] = actual
        added += 1
    # Only attach the dict when a write actually happened, so a state with
    # nothing to backfill keeps ``actuals == None`` rather than gaining an
    # empty dict (additive, no shape churn on a no-op run).
    if added:
        state.actuals = existing
    logger.info(f"backfill_actuals added={added} closed_total={_closed_count(state)}")
    return state, added


def _closed_count(state: State) -> int:
    """Return the number of CLOSED waves (for the backfill log line only)."""
    return sum(1 for wave in state.waves.values() if wave.status == WaveStatus.CLOSED)


__all__ = ["backfill_actuals"]
