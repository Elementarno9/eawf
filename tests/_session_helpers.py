"""Test helpers for lifecycle paths that require a live claim session."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus
from eawf.kernel.state.models import AgentSession, State, Wave
from eawf.workflow.lifecycle.transitions import claim_wave


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
    **kwargs: Any,
) -> Wave:
    """Seed a matching broad-scope session, then call the real transition."""
    wave = state.waves.get(wave_id)
    if wave is None:
        return claim_wave(state, wave_id=wave_id, session_id=session_id, **kwargs)
    role = wave.agent_role or AgentSessionRole.EXECUTOR
    if session_id not in state.agent_sessions:
        seed_active_session(state, session_id=session_id, role=role)
    return claim_wave(state, wave_id=wave_id, session_id=session_id, **kwargs)


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
