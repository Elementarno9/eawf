"""Parent, criteria, and repo-wide capacity guards for wave claims."""

from __future__ import annotations

from typing import Final

from eawf.kernel.state.enums import IterStatus, PhaseStatus, WaveStatus
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import Iter, State, Wave
from eawf.workflow.lifecycle._errors import (
    LifecycleError,
    LifecycleGuardCode,
    LifecycleGuardError,
)
from eawf.workflow.lifecycle.iter_ import _validate_iter_activation

CLAIM_PARENT_ITER_MISSING: Final[LifecycleGuardCode] = "claim_parent_iter_missing"
CLAIM_PARENT_PHASE_MISSING: Final[LifecycleGuardCode] = "claim_parent_phase_missing"
CLAIM_PARENT_PHASE_NOT_ACTIVE: Final[LifecycleGuardCode] = "claim_parent_phase_not_active"
CLAIM_PARENT_ITER_TERMINAL: Final[LifecycleGuardCode] = "claim_parent_iter_terminal"
CLAIM_ACTIVE_ITER_CONFLICT: Final[LifecycleGuardCode] = "claim_active_iter_conflict"
CLAIM_CRITERIA_EMPTY: Final[LifecycleGuardCode] = "claim_criteria_empty"
CLAIM_PARALLEL_LIMIT_REACHED: Final[LifecycleGuardCode] = "claim_parallel_limit_reached"
SPAWN_WAVE_NOT_CLAIMED: Final[LifecycleGuardCode] = "spawn_wave_not_claimed"

ACTIVE_WAVE_STATUSES: Final[frozenset[WaveStatus]] = frozenset(
    {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
)


def validate_claim_parent(state: State, wave: Wave) -> Iter:
    """Return the executable parent iter, or raise a coded claim guard.

    Real parent rows are authoritative; ``state.current`` pointers are only
    indexes and may be stale. A PLANNED iter runs the same transition and
    sibling-conflict validation as :func:`activate_iter`, without mutating it.

    Args:
        state: State holding the wave hierarchy.
        wave: Wave proposed for claim.

    Returns:
        The PLANNED or ACTIVE parent iter.

    Raises:
        LifecycleGuardError: When a parent is missing, the phase is not ACTIVE,
            the iter is terminal, or another sibling iter is ACTIVE.
    """
    parent_iter = state.iters.get(wave.iter_id)
    if parent_iter is None:
        raise LifecycleGuardError(
            CLAIM_PARENT_ITER_MISSING,
            wave.id,
            f"cannot claim wave {wave.id!r}: parent iter {wave.iter_id!r} does not exist",
        )
    parent_phase = state.phases.get(parent_iter.phase_id)
    if parent_phase is None:
        raise LifecycleGuardError(
            CLAIM_PARENT_PHASE_MISSING,
            wave.id,
            f"cannot claim wave {wave.id!r}: parent phase {parent_iter.phase_id!r} does not exist",
        )
    if parent_phase.status is not PhaseStatus.ACTIVE:
        raise LifecycleGuardError(
            CLAIM_PARENT_PHASE_NOT_ACTIVE,
            wave.id,
            f"cannot claim wave {wave.id!r}: parent phase {parent_phase.id!r} is "
            f"{parent_phase.status.value!r}, not active",
        )
    if parent_iter.status not in {IterStatus.PLANNED, IterStatus.ACTIVE}:
        raise LifecycleGuardError(
            CLAIM_PARENT_ITER_TERMINAL,
            wave.id,
            f"cannot claim wave {wave.id!r}: parent iter {parent_iter.id!r} is "
            f"{parent_iter.status.value!r}",
        )
    if parent_iter.status is IterStatus.PLANNED:
        try:
            _validate_iter_activation(state, parent_iter, allow_concurrent=False)
        except ValueError as exc:
            raise LifecycleGuardError(
                CLAIM_ACTIVE_ITER_CONFLICT,
                wave.id,
                f"cannot claim wave {wave.id!r}: {exc}",
            ) from exc
    return parent_iter


def validate_claim_criteria(wave: Wave) -> None:
    """Reject execution of a wave with no success criteria."""
    if wave.success_criteria:
        return
    raise LifecycleGuardError(
        CLAIM_CRITERIA_EMPTY,
        wave.id,
        f"cannot claim wave {wave.id!r}: success criteria are empty",
    )


def active_wave_ids(state: State) -> list[str]:
    """Return repo-wide CLAIMED/IN_PROGRESS ids from authoritative statuses."""
    return sorted(
        (wave.id for wave in state.waves.values() if wave.status in ACTIVE_WAVE_STATUSES),
        key=natural_key,
    )


def validate_claim_capacity(state: State, wave: Wave, *, max_parallel_waves: int) -> None:
    """Reject a first claim when the repo-wide active-wave pool is full."""
    if max_parallel_waves < 1:
        raise ValueError(f"max_parallel_waves must be >= 1: {max_parallel_waves!r}")
    active = active_wave_ids(state)
    if len(active) < max_parallel_waves:
        return
    raise LifecycleGuardError(
        CLAIM_PARALLEL_LIMIT_REACHED,
        wave.id,
        f"cannot claim wave {wave.id!r}: repo-wide active-wave limit "
        f"{max_parallel_waves} reached by {active}",
    )


def validate_spawn_wave(state: State, wave_id: str) -> Wave:
    """Return a wave eligible for process spawn, or reject before side effects.

    The real parent rows are authoritative. Current-pointer drift never grants
    spawn permission, and a PENDING wave must first pass the atomic live-claim
    transaction so this boundary observes CLAIMED/IN_PROGRESS.

    Args:
        state: State read at the live-spawn boundary.
        wave_id: Wave proposed for process creation.

    Returns:
        The eligible wave.

    Raises:
        LifecycleError: When *wave_id* is unknown.
        LifecycleGuardError: With ``spawn_wave_not_claimed`` when the wave or
            either parent row is not execution-active.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    parent_iter = state.iters.get(wave.iter_id)
    parent_phase = state.phases.get(parent_iter.phase_id) if parent_iter is not None else None
    eligible = (
        wave.status in ACTIVE_WAVE_STATUSES
        and parent_iter is not None
        and parent_iter.status is IterStatus.ACTIVE
        and parent_phase is not None
        and parent_phase.status is PhaseStatus.ACTIVE
    )
    if eligible:
        return wave
    iter_status = parent_iter.status.value if parent_iter is not None else "missing"
    phase_status = parent_phase.status.value if parent_phase is not None else "missing"
    raise LifecycleGuardError(
        SPAWN_WAVE_NOT_CLAIMED,
        wave.id,
        f"cannot spawn wave {wave.id!r}: wave status={wave.status.value!r}, "
        f"parent iter status={iter_status!r}, parent phase status={phase_status!r}; "
        "require active parents and claimed or in-progress wave",
    )


__all__ = [
    "ACTIVE_WAVE_STATUSES",
    "CLAIM_ACTIVE_ITER_CONFLICT",
    "CLAIM_CRITERIA_EMPTY",
    "CLAIM_PARALLEL_LIMIT_REACHED",
    "CLAIM_PARENT_ITER_MISSING",
    "CLAIM_PARENT_ITER_TERMINAL",
    "CLAIM_PARENT_PHASE_MISSING",
    "CLAIM_PARENT_PHASE_NOT_ACTIVE",
    "SPAWN_WAVE_NOT_CLAIMED",
    "active_wave_ids",
    "validate_claim_capacity",
    "validate_claim_criteria",
    "validate_claim_parent",
    "validate_spawn_wave",
]
