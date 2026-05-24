"""Idempotent state-shape repair migrations for :class:`~eawf.kernel.state.models.State`.

eawf has no ``schema_version``-gated state-migration framework analogous to
:mod:`eawf.kernel.config.migration` (the ``State`` model is read straight through
:func:`eawf.kernel.validate.strict.validate_state` →
:meth:`State.model_validate`). This module provides the typed,
**pure-functional** repair transforms that the daemon (the sole canonical
mutator per AGENTS rule 4) can run as a one-shot state op. Each transform
takes the typed :class:`State`, mutates it in place, and returns a small
report of what it changed so the caller can decide whether a write-back is
warranted.

Every transform here is **idempotent**: re-running it against an
already-repaired state is a no-op (it reports zero changes and leaves the
document byte-identical after re-serialisation).

Current transforms:

- :func:`abandon_orphaned_waves` — abandons non-terminal waves (and the
  non-terminal iters that hold them) that hang under a phase already in a
  terminal status (``archived`` / ``closed``). This catches the
  zombie-PENDING rows a pre-cascade ``archive_phase`` left behind (e.g.
  the 16 ``P21-I01-W01..W16`` waves under the archived ``P21`` phase).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from eawf.kernel.state.enums import IterStatus, PhaseStatus, WaveStatus
from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)

#: Phase statuses that should not own any non-terminal child wave/iter.
_TERMINAL_PHASE_STATUSES: frozenset[PhaseStatus] = frozenset(
    {PhaseStatus.CLOSED, PhaseStatus.ARCHIVED}
)

#: Wave statuses that are already terminal — left untouched by the repair.
_TERMINAL_WAVE_STATUSES: frozenset[WaveStatus] = frozenset(
    {WaveStatus.CLOSED, WaveStatus.FAILED, WaveStatus.ABANDONED}
)

#: Iter statuses that are already terminal — left untouched by the repair.
_TERMINAL_ITER_STATUSES: frozenset[IterStatus] = frozenset(
    {IterStatus.CLOSED, IterStatus.ABANDONED}
)


@dataclass(frozen=True)
class AbandonOrphansReport:
    """Outcome of one :func:`abandon_orphaned_waves` pass.

    Attributes:
        abandoned_wave_ids: Ids of the waves moved to
            :data:`WaveStatus.ABANDONED` this pass (empty when the state
            was already clean).
        abandoned_iter_ids: Ids of the iters moved to
            :data:`IterStatus.ABANDONED` this pass.
    """

    abandoned_wave_ids: tuple[str, ...]
    abandoned_iter_ids: tuple[str, ...]

    @property
    def changed(self) -> bool:
        """``True`` when at least one wave or iter was transitioned."""
        return bool(self.abandoned_wave_ids or self.abandoned_iter_ids)


def abandon_orphaned_waves(state: State) -> AbandonOrphansReport:
    """Abandon non-terminal waves/iters orphaned under a terminal phase.

    Walks every phase in a terminal status (``archived`` / ``closed``)
    and transitions each of its non-terminal child waves to
    :data:`WaveStatus.ABANDONED` and each non-terminal child iter to
    :data:`IterStatus.ABANDONED`, stamping ``closed_at`` so the
    closure-timestamp invariant holds. Waves/iters that are already
    terminal are left untouched, which makes the transform idempotent:
    a second pass finds nothing to change and returns an empty report.

    This is the backfill for the pre-cascade ``archive_phase`` bug that
    flipped only the phase status and left its waves PENDING forever
    (the ``P21-I01-W01..W16`` zombies under archived ``P21``).

    Args:
        state: The typed state to repair in place.

    Returns:
        An :class:`AbandonOrphansReport` listing the ids transitioned.
    """
    terminal_phase_ids = {
        pid for pid, phase in state.phases.items() if phase.status in _TERMINAL_PHASE_STATUSES
    }
    orphan_iter_ids = {iid for iid, it in state.iters.items() if it.phase_id in terminal_phase_ids}
    now = datetime.now(UTC)

    abandoned_waves: list[str] = []
    for wave_id, wave in state.waves.items():
        if wave.iter_id in orphan_iter_ids and wave.status not in _TERMINAL_WAVE_STATUSES:
            wave.status = WaveStatus.ABANDONED
            wave.closed_at = now
            abandoned_waves.append(wave_id)
            if wave_id in state.current.active_wave_ids:
                state.current.active_wave_ids.remove(wave_id)

    abandoned_iters: list[str] = []
    for iter_id in orphan_iter_ids:
        it = state.iters[iter_id]
        if it.status not in _TERMINAL_ITER_STATUSES:
            it.status = IterStatus.ABANDONED
            it.closed_at = now
            abandoned_iters.append(iter_id)

    report = AbandonOrphansReport(
        abandoned_wave_ids=tuple(sorted(abandoned_waves)),
        abandoned_iter_ids=tuple(sorted(abandoned_iters)),
    )
    if report.changed:
        logger.info(
            f"abandon_orphaned_waves waves={len(report.abandoned_wave_ids)} "
            f"iters={len(report.abandoned_iter_ids)}"
        )
    return report


__all__ = [
    "AbandonOrphansReport",
    "abandon_orphaned_waves",
]
