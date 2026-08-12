"""Unit: the post-spawn dispatch-state re-assertion.

Exercises :func:`eawf.runtime.daemon.methods.agent._reassert_dispatch_state`
against healthy, terminal, and partially reverted dispatch state. Recovery may
repair indexes for a still-claimed wave, but never promotes a PENDING wave back
to execution without a fresh atomic claim.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import AgentSessionRole, WaveStatus
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.agent import _reassert_dispatch_state
from eawf.workflow.lifecycle._errors import LifecycleGuardError

_WAVE_ID = "P28-I03-W57"
_SESSION_ID = "SES-executor-w57"
_STARTED_AT = datetime(2026, 5, 28, 0, 0, 0, tzinfo=UTC)


def _state_payload() -> dict[str, Any]:
    """A minimal ACTIVE-phase state with one claimed, in-flight wave."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-05-28T00:00:00Z",
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "Eawf",
            "description": "",
            "domains": ["workflow"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "track_id": None,
            "phase_id": "P28",
            "iter_id": "P28-I03",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [_SESSION_ID],
        },
        "workspace": None,
        "phases": {
            "P28": {
                "id": "P28",
                "scope_id": "EAWF",
                "title": "v0.4 verify spine",
                "status": "active",
                "iter_ids": ["P28-I03"],
                "outcome_ids": [],
                "opened_at": "2026-05-28T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P28-I03": {
                "id": "P28-I03",
                "phase_id": "P28",
                "title": "Build-out",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-28T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P28-I03",
                "title": "Verify gate",
                "status": "in_progress",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/workflow/verify/dispatch_close.py"],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": _SESSION_ID,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-05-28T00:00:00Z",
                "claimed_at": "2026-05-28T00:00:00Z",
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {
            _SESSION_ID: {
                "id": _SESSION_ID,
                "role": "executor",
                "runtime": "codex",
                "scope_id": _WAVE_ID,
                "status": "active",
                "claimed_wave_ids": [_WAVE_ID],
                "worktree_ids": [],
                "artifact_ids": [],
                "started_at": "2026-05-28T00:00:00Z",
                "ended_at": None,
                "summary": None,
                "agent_principal_id": None,
            }
        },
        "plugins": {},
        "indexes": {},
    }


def _reverted_payload() -> dict[str, Any]:
    """The state a jailed agent left after ``git checkout -- .ea/``.

    The wave reverts to its committed (pre-claim) ``PENDING`` status, the
    executor session vanishes from ``agent_sessions``, and the phase-active
    pointer is cleared -- exactly the smoke-run corruption.
    """
    payload = copy.deepcopy(_state_payload())
    payload["agent_sessions"] = {}
    payload["current"]["active_wave_ids"] = []
    payload["current"]["active_session_ids"] = []
    payload["current"]["phase_id"] = None
    payload["current"]["iter_id"] = None
    payload["waves"][_WAVE_ID]["status"] = "pending"
    payload["waves"][_WAVE_ID]["claim_session_id"] = None
    payload["waves"][_WAVE_ID]["claimed_at"] = None
    return payload


def _write(payload: dict[str, Any], tmp_path: Path) -> Path:
    state = State.model_validate(payload)
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-05-28T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        bus=None,
        event_path=state_path.parent / "store" / "event.jsonl",
        state_path=state_path,
    )


def test_reassert_rejects_reverted_pending_wave_without_side_effects(tmp_path: Path) -> None:
    """A reverted PENDING wave is never promoted by recovery."""
    state_path = _write(_reverted_payload(), tmp_path)
    before = state_path.read_bytes()

    with pytest.raises(LifecycleGuardError) as exc_info:
        _reassert_dispatch_state(
            _ctx(state_path),
            wave_id=_WAVE_ID,
            session_id=_SESSION_ID,
            session_runtime="codex",
            session_role=AgentSessionRole.EXECUTOR,
            session_scope_id=_WAVE_ID,
            session_started_at=_STARTED_AT,
        )

    assert exc_info.value.code == "spawn_wave_not_claimed"
    assert state_path.read_bytes() == before
    assert not (_ctx(state_path).event_path or Path()).exists()


def test_reassert_is_noop_on_healthy_state(tmp_path: Path) -> None:
    """A healthy dispatch state is left byte-for-byte unchanged (idempotent)."""
    state_path = _write(_state_payload(), tmp_path)
    before = state_path.read_text(encoding="utf-8")

    _reassert_dispatch_state(
        _ctx(state_path),
        wave_id=_WAVE_ID,
        session_id=_SESSION_ID,
        session_runtime="codex",
        session_role=AgentSessionRole.EXECUTOR,
        session_scope_id=_WAVE_ID,
        session_started_at=_STARTED_AT,
    )

    assert state_path.read_text(encoding="utf-8") == before


def test_reassert_does_not_resurrect_a_terminal_wave(tmp_path: Path) -> None:
    """A wave closed by close-on-behalf is not dragged back to in-progress."""
    payload = _reverted_payload()
    payload["waves"][_WAVE_ID]["status"] = "closed"
    state_path = _write(payload, tmp_path)

    _reassert_dispatch_state(
        _ctx(state_path),
        wave_id=_WAVE_ID,
        session_id=_SESSION_ID,
        session_runtime="codex",
        session_role=AgentSessionRole.EXECUTOR,
        session_scope_id=_WAVE_ID,
        session_started_at=_STARTED_AT,
    )

    reloaded = State.model_validate_json(state_path.read_text(encoding="utf-8"))
    assert reloaded.waves[_WAVE_ID].status is WaveStatus.CLOSED
    assert _SESSION_ID not in reloaded.agent_sessions


def test_reassert_rebuilds_missing_operator_session_without_wave_role_substitution(
    tmp_path: Path,
) -> None:
    """A missing operator binding returns as OPERATOR, not the wave specialist."""
    payload = _state_payload()
    payload["agent_sessions"] = {}
    payload["current"]["active_session_ids"] = []
    payload["waves"][_WAVE_ID]["agent_role"] = "reviewer"
    state_path = _write(payload, tmp_path)

    _reassert_dispatch_state(
        _ctx(state_path),
        wave_id=_WAVE_ID,
        session_id=_SESSION_ID,
        session_runtime="codex",
        session_role=AgentSessionRole.OPERATOR,
        session_scope_id="P28",
        session_started_at=_STARTED_AT,
    )

    reloaded = State.model_validate_json(state_path.read_text(encoding="utf-8"))
    session = reloaded.agent_sessions[_SESSION_ID]
    assert session.role is AgentSessionRole.OPERATOR
    assert session.scope_id == "P28"
    assert session.claimed_wave_ids == [_WAVE_ID]
    assert reloaded.waves[_WAVE_ID].claim_session_id == _SESSION_ID


def test_reassert_repairs_existing_session_claimed_wave_index(tmp_path: Path) -> None:
    """Reverse-only binding regains the session's forward claimed-wave index."""
    payload = _state_payload()
    payload["waves"][_WAVE_ID]["agent_role"] = "reviewer"
    payload["agent_sessions"][_SESSION_ID]["role"] = "operator"
    payload["agent_sessions"][_SESSION_ID]["scope_id"] = "P28"
    payload["agent_sessions"][_SESSION_ID]["claimed_wave_ids"] = []
    state_path = _write(payload, tmp_path)

    _reassert_dispatch_state(
        _ctx(state_path),
        wave_id=_WAVE_ID,
        session_id=_SESSION_ID,
        session_runtime="codex",
        session_role=AgentSessionRole.OPERATOR,
        session_scope_id="P28",
        session_started_at=_STARTED_AT,
    )

    reloaded = State.model_validate_json(state_path.read_text(encoding="utf-8"))
    session = reloaded.agent_sessions[_SESSION_ID]
    assert session.role is AgentSessionRole.OPERATOR
    assert session.claimed_wave_ids == [_WAVE_ID]
