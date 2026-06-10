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
- :func:`backfill_missing_wave_intents` — attaches a synthetic
  :class:`~eawf.kernel.spec.intent.IntentBrief` to explicit closed waves that
  already carry synced criteria + gates but still have ``intent=None``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from eawf.kernel.spec.intent import IntentBrief
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


@dataclass(frozen=True)
class WaveIntentBackfillRow:
    """One inspected wave from :func:`backfill_missing_wave_intents`.

    Attributes:
        wave_id: Target wave id.
        changed: ``True`` when the row was mutated on this apply pass.
        would_change: ``True`` when the row is eligible for repair. In dry-run
            mode this means the command would mutate it; in apply mode it means
            it was mutated.
        reason: Stable machine-readable outcome reason.
    """

    wave_id: str
    changed: bool
    would_change: bool
    reason: str


@dataclass(frozen=True)
class WaveIntentBackfillReport:
    """Outcome of one :func:`backfill_missing_wave_intents` pass.

    Attributes:
        rows: Per-wave inspection rows in caller-supplied order.
        apply: Whether the pass mutated eligible rows.
    """

    rows: tuple[WaveIntentBackfillRow, ...]
    apply: bool

    @property
    def changed_wave_ids(self) -> tuple[str, ...]:
        """Wave ids mutated during this pass."""
        return tuple(row.wave_id for row in self.rows if row.changed)

    @property
    def pending_wave_ids(self) -> tuple[str, ...]:
        """Wave ids that still need repair after a dry-run/check pass."""
        return tuple(row.wave_id for row in self.rows if row.would_change and not row.changed)

    @property
    def skipped_wave_ids(self) -> tuple[str, ...]:
        """Wave ids that were not eligible for repair."""
        return tuple(row.wave_id for row in self.rows if not row.would_change and not row.changed)

    @property
    def clean(self) -> bool:
        """``True`` when every inspected wave already has intent."""
        return all(row.reason == "already_has_intent" for row in self.rows)

    @property
    def changed(self) -> bool:
        """``True`` when at least one row was mutated."""
        return bool(self.changed_wave_ids)


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


def _bounded(value: str, *, limit: int) -> str:
    """Return a single-line string capped to *limit* characters."""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _intent_for_synced_wave(state: State, wave_id: str) -> IntentBrief:
    """Synthesize metadata-only intent from an already-synced wave row."""
    wave = state.waves[wave_id]
    planned_steps = [
        _bounded(criterion.text, limit=500)
        for criterion in wave.success_criteria[:10]
        if criterion.text.strip()
    ]
    if not planned_steps:
        planned_steps = [f"preserve synced criteria for {wave_id}"]
    return IntentBrief(
        problem=_bounded(f"wave {wave_id} was synced without typed intent", limit=200),
        desired_outcome=_bounded(
            f"{wave.title} keeps auditable criteria with IntentBrief metadata",
            limit=200,
        ),
        priority_rationale=(
            "metadata repair for a closed wave whose spec.sync accepted intent=None"
        ),
        planned_steps=planned_steps,
        risks=["metadata-only repair must not change the closed wave outcome"],
    )


def backfill_missing_wave_intents(
    state: State,
    *,
    wave_ids: Iterable[str],
    apply: bool,
) -> WaveIntentBackfillReport:
    """Backfill missing intent on explicit closed waves with synced gates.

    This recovery transform is intentionally narrow: it never scans all state
    by default, never edits a non-closed wave, and only touches rows that
    already carry both typed criteria and gates. The synthesized intent is
    metadata-only and derives its planned steps from the already-synced
    criterion text so future coverage checks can explain the repair.

    Args:
        state: The typed state to inspect or mutate.
        wave_ids: Explicit target wave ids, in caller-supplied order.
        apply: When ``True``, persist eligible intents into the typed state.
            When ``False``, report what would change without mutation.

    Returns:
        A :class:`WaveIntentBackfillReport` listing every inspected target.
    """
    rows: list[WaveIntentBackfillRow] = []
    seen: set[str] = set()
    for wave_id in wave_ids:
        if wave_id in seen:
            continue
        seen.add(wave_id)
        wave = state.waves.get(wave_id)
        if wave is None:
            rows.append(
                WaveIntentBackfillRow(
                    wave_id=wave_id,
                    changed=False,
                    would_change=False,
                    reason="unknown_wave",
                )
            )
            continue
        if wave.intent is not None:
            rows.append(
                WaveIntentBackfillRow(
                    wave_id=wave_id,
                    changed=False,
                    would_change=False,
                    reason="already_has_intent",
                )
            )
            continue
        if wave.status != WaveStatus.CLOSED:
            rows.append(
                WaveIntentBackfillRow(
                    wave_id=wave_id,
                    changed=False,
                    would_change=False,
                    reason=f"not_closed:{wave.status.value}",
                )
            )
            continue
        if not wave.success_criteria:
            rows.append(
                WaveIntentBackfillRow(
                    wave_id=wave_id,
                    changed=False,
                    would_change=False,
                    reason="missing_success_criteria",
                )
            )
            continue
        if not wave.gates:
            rows.append(
                WaveIntentBackfillRow(
                    wave_id=wave_id,
                    changed=False,
                    would_change=False,
                    reason="missing_gates",
                )
            )
            continue
        if apply:
            intent = _intent_for_synced_wave(state, wave_id)
            wave.__pydantic_validator__.validate_assignment(wave, "intent", intent)
            rows.append(
                WaveIntentBackfillRow(
                    wave_id=wave_id,
                    changed=True,
                    would_change=True,
                    reason="backfilled",
                )
            )
        else:
            rows.append(
                WaveIntentBackfillRow(
                    wave_id=wave_id,
                    changed=False,
                    would_change=True,
                    reason="missing_intent",
                )
            )
    report = WaveIntentBackfillReport(rows=tuple(rows), apply=apply)
    if report.changed:
        logger.info(f"backfill_missing_wave_intents waves={len(report.changed_wave_ids)}")
    return report


__all__ = [
    "AbandonOrphansReport",
    "WaveIntentBackfillReport",
    "WaveIntentBackfillRow",
    "abandon_orphaned_waves",
    "backfill_missing_wave_intents",
]
