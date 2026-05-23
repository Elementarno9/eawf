"""Integration: dispatch runner flips the wave to IN_PROGRESS at its head.

Exercises the head transition of
:func:`eawf.daemon.dispatch_runner.run_dispatch` (and the
:func:`eawf.daemon.dispatch_runner._mark_wave_in_progress` helper that
drives it) against a real ``state.json`` on a tmp filesystem.

The load-bearing assertion is the wave's success criterion: a dispatched
wave that starts the run in :data:`~eawf.state.enums.WaveStatus.CLAIMED`
is persisted as :data:`~eawf.state.enums.WaveStatus.IN_PROGRESS` once
``run_dispatch`` drives it. The flip routes through the daemon canonical
state writer (:func:`eawf.state.writer.atomic_write_json_locked` under the
``state.json`` portalock), so the persisted status is indistinguishable
from one written by the daemon's ``state.mutate`` path.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.daemon.dispatch_runner import (
    DispatchTokens,
    _mark_wave_in_progress,
    run_dispatch,
)
from eawf.daemon.methods import MethodContext
from eawf.evidence._io import load_state
from eawf.state.enums import WaveStatus
from eawf.state.models import State

pytestmark = pytest.mark.integration

_WAVE_ID = "P27-I03-W11"
_SESSION_ID = "SES-executor"


def _state_payload(*, wave_status: str) -> dict[str, Any]:
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
            "subproject_id": None,
            "phase_id": "P27",
            "iter_id": "P27-I03",
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
                "iter_ids": ["P27-I03"],
                "outcome_ids": [],
                "opened_at": "2026-05-23T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P27-I03": {
                "id": "P27-I03",
                "phase_id": "P27",
                "title": "Build-out",
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
                "iter_id": "P27-I03",
                "title": "Set wave to in_progress at implementation-start",
                "status": wave_status,
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/daemon/dispatch_runner.py"],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": _SESSION_ID,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
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


def _write_state(tmp_path: Path, *, wave_status: str) -> Path:
    """Serialise a valid :class:`State` whose wave carries *wave_status*."""
    state = State.model_validate(_state_payload(wave_status=wave_status))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path | None) -> MethodContext:
    """Daemon context wired to *state_path* (+ sibling event log) or stateless."""
    event_path = state_path.parent / "store" / "event.jsonl" if state_path is not None else None
    return MethodContext(
        started_at="2026-05-23T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.3.0",
        bus=None,
        event_path=event_path,
        state_path=state_path,
    )


def _tokens() -> DispatchTokens:
    return DispatchTokens(
        input_tokens=1200,
        output_tokens=340,
        cache_creation_input_tokens=8000,
        cache_read_input_tokens=64000,
    )


def _persisted_status(state_path: Path) -> WaveStatus:
    return load_state(state_path).waves[_WAVE_ID].status


def test_run_dispatch_flips_claimed_wave_to_in_progress(tmp_path: Path) -> None:
    """A CLAIMED wave is persisted as IN_PROGRESS once ``run_dispatch`` drives it."""
    state_path = _write_state(tmp_path, wave_status="claimed")
    ctx = _ctx(state_path)
    assert _persisted_status(state_path) is WaveStatus.CLAIMED

    run_dispatch(
        ctx,
        wave_id=_WAVE_ID,
        primary_runtime="claude",
        fallback_runtime="codex",
        model="claude-opus-4-7",
        pricing_version="2026.05.17",
        primary_error=None,
        tokens=_tokens(),
        cost_usd=Decimal("0.05"),
    )

    assert _persisted_status(state_path) is WaveStatus.IN_PROGRESS


def test_run_dispatch_in_progress_wave_stays_in_progress(tmp_path: Path) -> None:
    """An already-IN_PROGRESS wave is left untouched (idempotent head transition)."""
    state_path = _write_state(tmp_path, wave_status="in_progress")
    ctx = _ctx(state_path)

    run_dispatch(
        ctx,
        wave_id=_WAVE_ID,
        primary_runtime="claude",
        fallback_runtime="codex",
        model="claude-opus-4-7",
        pricing_version="2026.05.17",
        primary_error=None,
        tokens=_tokens(),
        cost_usd=Decimal("0.05"),
    )

    assert _persisted_status(state_path) is WaveStatus.IN_PROGRESS


def test_run_dispatch_stateless_context_skips_head_transition(tmp_path: Path) -> None:
    """A stateless context (no ``state_path``) drives the dispatch without faulting."""
    ctx = _ctx(None)
    ctx.event_path = tmp_path / "store" / "event.jsonl"

    result = run_dispatch(
        ctx,
        wave_id=_WAVE_ID,
        primary_runtime="claude",
        fallback_runtime="codex",
        model="claude-opus-4-7",
        pricing_version="2026.05.17",
        primary_error=None,
        tokens=_tokens(),
        cost_usd=Decimal("0.05"),
    )

    assert result.runtime == "claude"
    assert result.switched is False


def test_mark_wave_in_progress_returns_true_on_flip(tmp_path: Path) -> None:
    """The helper reports a persisted flip for a CLAIMED wave."""
    state_path = _write_state(tmp_path, wave_status="claimed")
    ctx = _ctx(state_path)

    assert _mark_wave_in_progress(ctx, wave_id=_WAVE_ID) is True
    assert _persisted_status(state_path) is WaveStatus.IN_PROGRESS


def test_mark_wave_in_progress_skips_without_state_path() -> None:
    """The helper is a no-op (returns ``False``) when no state is wired."""
    ctx = _ctx(None)
    assert _mark_wave_in_progress(ctx, wave_id=_WAVE_ID) is False


def test_mark_wave_in_progress_skips_unknown_wave(tmp_path: Path) -> None:
    """An absent wave is skipped (returns ``False``) rather than raising."""
    state_path = _write_state(tmp_path, wave_status="claimed")
    ctx = _ctx(state_path)
    assert _mark_wave_in_progress(ctx, wave_id="P27-I03-W99") is False


def test_mark_wave_in_progress_skips_pending_wave(tmp_path: Path) -> None:
    """A not-yet-claimed wave is skipped — the claim-gate owns that precondition."""
    state_path = _write_state(tmp_path, wave_status="pending")
    ctx = _ctx(state_path)
    assert _mark_wave_in_progress(ctx, wave_id=_WAVE_ID) is False
    assert _persisted_status(state_path) is WaveStatus.PENDING
