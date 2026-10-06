"""Test helpers that seed live claim sessions into epoch-1 state."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson

from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus
from eawf.kernel.state.models import AgentSession, State, Wave
from tests._epoch1_seed import seed_claim


def seed_active_session(
    state: State,
    *,
    session_id: str,
    scope_id: str | None = None,
    role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    runtime: str = "test",
) -> AgentSession:
    """Insert one ACTIVE session and its current-pointer index."""
    session = AgentSession(
        id=session_id,
        role=role,
        runtime=runtime,
        scope_id=scope_id or (state.project.code if state.project else state.urn),
        status=AgentSessionStatus.ACTIVE,
        started_at=datetime.now(UTC),
    )
    state.agent_sessions[session_id] = session
    if session_id not in state.current.active_session_ids:
        state.current.active_session_ids.append(session_id)
    return session


def claim_wave_with_session(
    state: State,
    *,
    wave_id: str,
    session_id: str,
) -> Wave:
    """Seed a matching broad-scope session, then mark the wave CLAIMED by it."""
    wave = state.waves[wave_id]
    role = wave.agent_role or AgentSessionRole.EXECUTOR
    if session_id not in state.agent_sessions:
        seed_active_session(state, session_id=session_id, role=role)
    return seed_claim(state, wave_id=wave_id, session_id=session_id)


def seed_active_session_on_disk(
    state_path: Path,
    *,
    session_id: str,
    scope_id: str | None = None,
    role: AgentSessionRole = AgentSessionRole.EXECUTOR,
) -> None:
    """Seed one live session into an integration-test state document."""
    state = State.model_validate(orjson.loads(state_path.read_bytes()))
    seed_active_session(
        state,
        session_id=session_id,
        scope_id=scope_id,
        role=role,
    )
    state_path.write_bytes(
        orjson.dumps(
            state.model_dump(mode="json"),
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        )
    )
