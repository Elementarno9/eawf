"""Seed epoch-1 phase, iter and wave rows straight into a typed ``State``.

The epoch-1 transition writers are gone; tests that still read a frozen
epoch-1 tree (readiness, dispatch rendering, verify floors) build the rows
they need here, with the same field values those writers stamped.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from eawf.kernel.migration.v1_state import State
from eawf.kernel.state.enums import IterStatus, PhaseStatus, WaveStatus
from eawf.kernel.state.models import Iter, Phase, Wave


def seed_phase(state: State, *, phase_id: str, title: str) -> Phase:
    """Insert an ACTIVE phase and point ``current`` at it."""
    project_code = state.project.code if state.project is not None else None
    phase = Phase(
        id=phase_id,
        scope_id=project_code or "unknown",
        track_id=state.current.track_id,
        title=title,
        status=PhaseStatus.ACTIVE,
        iter_ids=[],
        outcome_ids=[],
        opened_at=datetime.now(UTC),
    )
    state.phases[phase_id] = phase
    state.current.phase_id = phase_id
    state.current.iter_id = None
    state.current.active_wave_ids = []
    return phase


def seed_iter(state: State, *, iter_id: str, phase_id: str, title: str) -> Iter:
    """Insert an ACTIVE iter under *phase_id* and point ``current`` at it."""
    it = Iter(
        id=iter_id,
        phase_id=phase_id,
        title=title,
        status=IterStatus.ACTIVE,
        wave_ids=[],
        opened_at=datetime.now(UTC),
    )
    state.iters[iter_id] = it
    phase = state.phases[phase_id]
    if iter_id not in phase.iter_ids:
        phase.iter_ids.append(iter_id)
    state.current.phase_id = phase_id
    state.current.iter_id = iter_id
    state.current.active_wave_ids = []
    return it


def seed_wave(
    state: State,
    *,
    wave_id: str,
    iter_id: str,
    title: str,
    deps: Iterable[str] = (),
    file_scopes: Iterable[str] = (),
    **fields: Any,
) -> Wave:
    """Insert a PENDING wave under *iter_id*, keeping each dep's ``blocks`` index."""
    deps_list = list(deps)
    wave = Wave(
        id=wave_id,
        iter_id=iter_id,
        title=title,
        status=WaveStatus.PENDING,
        deps=deps_list,
        blocks=[],
        file_scopes=list(file_scopes),
        opened_at=datetime.now(UTC),
        **fields,
    )
    state.waves[wave_id] = wave
    it = state.iters[iter_id]
    if wave_id not in it.wave_ids:
        it.wave_ids.append(wave_id)
    for dep in deps_list:
        dep_wave = state.waves[dep]
        if wave_id not in dep_wave.blocks:
            dep_wave.blocks.append(wave_id)
    return wave


def seed_claim(state: State, *, wave_id: str, session_id: str) -> Wave:
    """Mark *wave_id* CLAIMED by *session_id*, as the epoch-1 claim recorded it.

    The session row must already exist; its claimed-wave index and the
    ``current`` active-wave pointer move with the wave.
    """
    wave = state.waves[wave_id]
    wave.status = WaveStatus.CLAIMED
    wave.claim_session_id = session_id
    if wave.claimed_at is None:
        wave.claimed_at = datetime.now(UTC)
    session = state.agent_sessions[session_id]
    if wave_id not in session.claimed_wave_ids:
        session.claimed_wave_ids = [*session.claimed_wave_ids, wave_id]
    if wave_id not in state.current.active_wave_ids:
        state.current.active_wave_ids.append(wave_id)
    return wave
