"""A minimal epoch-1 state payload with one in-progress executor wave.

Shared by the budget tests that measure a wave against its ceiling.
"""

from __future__ import annotations

from typing import Any

_WAVE_ID = "P27-I04-W01"
_SESSION_ID = "SES-executor"


def _state_payload(*, wave_status: str = "in_progress", tokens_consumed: int = 0) -> dict[str, Any]:
    """Return the payload with the wave at *wave_status* and *tokens_consumed*."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-05-23T00:00:00Z",
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
            "phase_id": "P27",
            "iter_id": "P27-I04",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [_SESSION_ID],
        },
        "workspace": None,
        "phases": {
            "P27": {
                "id": "P27",
                "scope_id": "EAWF",
                "title": "Observability",
                "status": "active",
                "iter_ids": ["P27-I04"],
                "outcome_ids": [],
                "opened_at": "2026-05-23T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P27-I04": {
                "id": "P27-I04",
                "phase_id": "P27",
                "title": "TUI richer views",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-23T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P27-I04",
                "title": "Live token accrual into tokens_consumed",
                "status": wave_status,
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/budget/service.py"],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": _SESSION_ID,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": tokens_consumed,
                "outcome": None,
                "opened_at": "2026-05-23T00:00:00Z",
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {
            _SESSION_ID: {
                "id": _SESSION_ID,
                "role": "executor",
                "runtime": "claude",
                "scope_id": _WAVE_ID,
                "status": "active",
                "claimed_wave_ids": [_WAVE_ID],
                "worktree_ids": [],
                "artifact_ids": [],
                "started_at": "2026-05-23T00:00:00Z",
                "ended_at": None,
                "summary": None,
            }
        },
        "plugins": {},
        "indexes": {},
    }
