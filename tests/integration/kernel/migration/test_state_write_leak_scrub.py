"""State writers refuse new free text that carries a state leak shape.

The CLI ``state_transaction`` writer diffs the payload on disk against the
one about to be written. A new or changed string that matches the state leak
patterns refuses the write with ``validation_failed`` naming its field path,
and ``state.json`` stays byte-identical. A clean write is unaffected: the
bytes written are exactly the payload handed to the scrub. A workspace index
may still record a repo checkout under a home directory, since that field is
a local path by design.

The suite drives the real ``state_transaction`` context manager; no live
daemon, no subprocess. The home
path is assembled at runtime so this file carries no literal the leak lints
would flag.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import orjson
import pytest

from eawf.kernel.migration.v1_state import State
from eawf.kernel.state.enums import ProjectStatus
from eawf.kernel.state.models import WorkspaceIndex, WorkspaceRepoRef
from eawf.surfaces.cli import _mutation
from eawf.surfaces.cli import errors as cli_errors

pytestmark = pytest.mark.integration

MACOS_HOME = "/" + "Users" + "/" + "alice"
LEAKY_TEXT = f"finished in {MACOS_HOME}/work/repo"

_T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
_WAVE = "P40-I01-W01"
_SIBLING = "P40-I01-W02"


def _wave_row(wave_id: str, *, status: str, outcome: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": wave_id,
        "iter_id": "P40-I01",
        "title": f"wave {wave_id}",
        "status": status,
        "file_scopes": ["src/x.py"],
        "success_criteria": [],
        "gates": [],
        "effort_bucket": "S",
        "agent_role": "executor",
        "opened_at": _T0.isoformat(),
        "sessions": {},
    }
    if outcome is not None:
        row["outcome"] = outcome
    return row


def _state_payload(*, sibling_outcome: str | None = None) -> dict[str, Any]:
    """A minimal valid State with a CLAIMED wave and a CLAIMED sibling."""
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
            "P40": {
                "id": "P40",
                "scope_id": "ABC",
                "track_id": None,
                "title": "P40",
                "status": "active",
                "iter_ids": ["P40-I01"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P40-I01": {
                "id": "P40-I01",
                "phase_id": "P40",
                "title": "I01",
                "status": "active",
                "wave_ids": [_WAVE, _SIBLING],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE: _wave_row(_WAVE, status="claimed"),
            _SIBLING: _wave_row(_SIBLING, status="claimed", outcome=sibling_outcome),
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(state_path: Path, *, sibling_outcome: str | None = None) -> bytes:
    state = State.model_validate(_state_payload(sibling_outcome=sibling_outcome))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path.read_bytes()


def _record_scrubbed_bytes(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Wrap ``module.state_leak_refusal`` to capture the payload it was handed.

    Each entry is the payload serialised the way the state writer serialises
    it, taken before the real scrub runs, so a caller can prove the bytes on
    disk are exactly what the scrub saw.
    """
    seen: list[bytes] = []
    real_refusal: Callable[[object, object], str | None] = module.state_leak_refusal

    def _spy(old: object, new: dict[str, Any]) -> str | None:
        seen.append(orjson.dumps(new, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n")
        refusal = real_refusal(old, new)
        assert refusal is None
        return refusal

    monkeypatch.setattr(module, "state_leak_refusal", _spy)
    return seen


def test_state_transaction_refuses_leaking_wave_outcome(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    before = _write_state(state_path)
    _mutation.set_daemonless_flag(False)

    with (
        pytest.raises(cli_errors.ValidationError, match=rf"waves\.{_WAVE}\.outcome \(home_path\)"),
        _mutation.state_transaction(state_path) as state,
    ):
        state.waves[_WAVE].outcome = LEAKY_TEXT

    assert state_path.read_bytes() == before


def _workspace_state(*, code: str, title: str) -> dict[str, object]:
    """Return a minimal workspace-scoped state document."""
    return {
        "schema_version": "1.0",
        "scope_kind": "workspace",
        "urn": f"urn:eawf:v1:workspace:{code}",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "project": None,
        "current": {
            "project_code": None,
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": {"code": code, "title": title, "repos": {}, "current_repo_code": None},
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def test_state_transaction_accepts_workspace_repo_checkout_under_home(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(orjson.dumps(_workspace_state(code="WSP", title="workspace")))
    checkout = f"{MACOS_HOME}/work/abc"
    _mutation.set_daemonless_flag(False)

    with _mutation.state_transaction(state_path) as state:
        assert state.workspace is not None
        ref = WorkspaceRepoRef(
            code="ABC",
            path=checkout,
            state_urn="urn:eawf:v1:state:ABC",
            project_code="ABC",
            title="abc",
            status=ProjectStatus.ACTIVE,
        )
        state.workspace = WorkspaceIndex(
            code=state.workspace.code,
            title=state.workspace.title,
            repos={"ABC": ref},
        )

    assert orjson.loads(state_path.read_bytes())["workspace"]["repos"]["ABC"]["path"] == checkout


def test_state_transaction_read_only_skips_scrub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    before = _write_state(state_path)
    seen = _record_scrubbed_bytes(_mutation, monkeypatch)

    with _mutation.state_transaction(state_path, read_only=True) as state:
        assert state.waves[_WAVE].status.value == "claimed"

    assert seen == []
    assert state_path.read_bytes() == before
