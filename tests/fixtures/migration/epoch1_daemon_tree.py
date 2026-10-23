"""A minimal epoch-1 tree for the tests of the daemon's kept v1 methods.

The payload is one active phase and iter holding a CLAIMED wave and a
PENDING sibling, enough for any handler that reads ``state.json``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eawf.kernel.migration.v1_state import State

_T0 = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
_WAVE = "P30-I23-W09"


def state_payload() -> dict[str, Any]:
    """A minimal valid State with one CLAIMED wave and an open sibling row."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "ABC",
                "track_id": None,
                "title": "P30",
                "status": "active",
                "iter_ids": ["P30-I23"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I23": {
                "id": "P30-I23",
                "phase_id": "P30",
                "title": "I23",
                "status": "active",
                "wave_ids": [_WAVE, "P30-I23-W99"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE: {
                "id": _WAVE,
                "iter_id": "P30-I23",
                "title": "split wave close into pre-flight and commit",
                "status": "claimed",
                "file_scopes": ["src/x.py"],
                "success_criteria": [],
                "gates": [],
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "sessions": {},
            },
            "P30-I23-W99": {
                "id": "P30-I23-W99",
                "iter_id": "P30-I23",
                "title": "sibling wave for concurrent-mutation probes",
                "status": "pending",
                "file_scopes": ["src/y.py"],
                "success_criteria": [],
                "gates": [],
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "sessions": {},
            },
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def write_state(state_path: Path) -> None:
    state = State.model_validate(state_payload())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
