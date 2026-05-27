"""Earned-autonomy ceremony-mode recommender.

The recommender is deliberately read-only: it derives the current
operator-confirmed streak from ``State`` at call time rather than storing
another counter in ``state.json``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AgentSessionRole, WaveStatus
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import State, Wave

CEREMONY_SCHEMA_VERSION: Literal[1] = 1
CeremonyMode = Literal["A", "B", "C"]

MODE_A_COUNTER_THRESHOLD = 3
MODE_B_COUNTER_THRESHOLD = 1


class CeremonyRecommendation(BaseModel):
    """Read-only ceremony recommendation for one dispatch context."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = CEREMONY_SCHEMA_VERSION
    mode: CeremonyMode
    operator_confirmed_counter: int = Field(ge=0)
    closed_wave_count: int = Field(ge=0)
    considered_wave_ids: list[str] = Field(default_factory=list)
    operator_confirmed_wave_ids: list[str] = Field(default_factory=list)
    reason: str


def compute_ceremony(state: State, *, wave_id: str | None = None) -> CeremonyRecommendation:
    """Recommend ceremony mode A/B/C from the current state snapshot.

    Args:
        state: Validated state snapshot. The function never mutates it.
        wave_id: Optional target wave. When supplied, the counter is scoped
            to prior closed waves in the same phase and excludes the target
            wave itself. ``None`` computes across all closed waves.

    Returns:
        A strict :class:`CeremonyRecommendation` whose
        ``operator_confirmed_counter`` is the current consecutive streak of
        closed waves confirmed by an operator session.

    Raises:
        KeyError: When ``wave_id`` is unknown or its wave → iter → phase
            chain is broken.
    """
    target_phase_id = _phase_id_for_wave(state, wave_id) if wave_id is not None else None
    closed_waves = _closed_waves(state, wave_id=wave_id, phase_id=target_phase_id)
    confirmed_wave_ids = _operator_confirmed_streak(state, closed_waves)
    counter = len(confirmed_wave_ids)
    mode, reason = _recommend_mode(counter)
    return CeremonyRecommendation(
        mode=mode,
        operator_confirmed_counter=counter,
        closed_wave_count=len(closed_waves),
        considered_wave_ids=[wave.id for wave in closed_waves],
        operator_confirmed_wave_ids=confirmed_wave_ids,
        reason=reason,
    )


def _phase_id_for_wave(state: State, wave_id: str) -> str:
    wave = state.waves.get(wave_id)
    if wave is None:
        raise KeyError(f"unknown wave: {wave_id!r}")
    it = state.iters.get(wave.iter_id)
    if it is None:
        raise KeyError(
            f"wave {wave.id!r} references unknown iter {wave.iter_id!r}; cannot resolve phase"
        )
    if it.phase_id not in state.phases:
        raise KeyError(
            f"iter {it.id!r} references unknown phase {it.phase_id!r}; cannot resolve phase"
        )
    return it.phase_id


def _closed_waves(
    state: State,
    *,
    wave_id: str | None,
    phase_id: str | None,
) -> list[Wave]:
    closed = [
        wave
        for wave in state.waves.values()
        if wave.status == WaveStatus.CLOSED
        and wave.closed_at is not None
        and wave.id != wave_id
        and (phase_id is None or _wave_phase_id(state, wave) == phase_id)
    ]
    closed.sort(key=lambda wave: (wave.closed_at, natural_key(wave.id)), reverse=True)
    return closed


def _wave_phase_id(state: State, wave: Wave) -> str | None:
    it = state.iters.get(wave.iter_id)
    if it is None:
        return None
    return it.phase_id


def _operator_confirmed_streak(state: State, closed_waves: list[Wave]) -> list[str]:
    confirmed: list[str] = []
    for wave in closed_waves:
        if not _is_operator_confirmed(state, wave):
            break
        confirmed.append(wave.id)
    return confirmed


def _is_operator_confirmed(state: State, wave: Wave) -> bool:
    if wave.claim_session_id is None:
        return False
    session = state.agent_sessions.get(wave.claim_session_id)
    return session is not None and session.role == AgentSessionRole.OPERATOR


def _recommend_mode(counter: int) -> tuple[CeremonyMode, str]:
    if counter >= MODE_A_COUNTER_THRESHOLD:
        return (
            "A",
            "operator-confirmed streak meets mode A threshold; floor-only ceremony recommended",
        )
    if counter >= MODE_B_COUNTER_THRESHOLD:
        return (
            "B",
            "operator-confirmed streak present but below mode A threshold; "
            "standard ceremony recommended",
        )
    return "C", "no operator-confirmed streak; high ceremony recommended"


__all__ = [
    "CEREMONY_SCHEMA_VERSION",
    "MODE_A_COUNTER_THRESHOLD",
    "MODE_B_COUNTER_THRESHOLD",
    "CeremonyMode",
    "CeremonyRecommendation",
    "compute_ceremony",
]
