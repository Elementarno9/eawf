"""Tests: needs_user fork, fleet jury authority, non-lane kill (P30-I17-W09).

Three idle contracts go live in W09:

- C1: the needs_user fork outcome is reachable from a real lane -- the live
  watcher detects an open needs_user pause for the lane's wave and reports
  ``"needs_user"``, so the loop produces a ``NEEDS_USER_SPLIT`` blocking fork.
- C2: the drive resolves jury block authority through the SAME resolver the
  wave-close gate uses, so a high / ui lane auto-closes exactly when the close
  gate would (default-advisory until the jury is calibrated).
- C3: killing a single-wave dispatched session (no fleet run) succeeds via the
  session-pid fallback -- a non-fleet ``eawf dispatch wave`` spawn is killable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import (
    FleetForkReason,
    FleetLane,
    FleetRunState,
    State,
)
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import (
    _has_open_pause,
    _resolve_run_block_authority,
    arm_drive,
    build_liveness_watcher,
    kill_lane,
)
from eawf.runtime.runtimes.cancel import CancelResult
from eawf.workflow.evidence._io import load_state
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import record_pause

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I17-W09"


def _state_payload(
    *, wave_status: str = "in_progress", with_session_pid: int | None = None
) -> dict[str, Any]:
    sessions: dict[str, Any] = {}
    if with_session_pid is not None:
        sessions["1"] = {
            "attempt": 1,
            "runtime": "claude-code",
            "session_id": "ses-x",
            "session_log_handle": "urn:eawf:v1:session-log:claude-code:abc",
            "started_at": "2026-06-11T00:00:00Z",
            "ended_at": None,
            "exit_status": None,
            "subprocess_pid": with_session_pid,
        }
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-11T00:00:00Z",
        "dispatch_paused": False,
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
            "phase_id": "P30",
            "iter_id": "P30-I17",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "title": "Binding pass",
                "status": "active",
                "iter_ids": ["P30-I17"],
                "outcome_ids": [],
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I17": {
                "id": "P30-I17",
                "phase_id": "P30",
                "title": "Autopilot full-wire",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P30-I17",
                "title": "Live wave",
                "status": wave_status,
                "deps": [],
                "blocks": [],
                "file_scopes": [],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": "ses-x",
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
                "sessions": sessions,
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, **kwargs: Any) -> Path:
    state = State.model_validate(_state_payload(**kwargs))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    (state_dir / "store").mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        event_path=state_path.parent / "store" / "event.jsonl",
        state_path=state_path,
    )


def _record_pause(state_path: Path, wave_id: str) -> None:
    record_pause(
        state_path,
        scope_id=wave_id,
        session="urn:eawf:v1:session:ses-x",
        question=UserQuestion(
            question="which approach?",
            options=[UserQuestionOption(label="A"), UserQuestionOption(label="B")],
        ),
    )


def _lane(*, pgid: int | None = 9001) -> FleetLane:
    return FleetLane(
        wave_id=_WAVE_ID,
        attempt=1,
        session_id="ses-x",
        pgid=pgid,
        dispatched_at=datetime(2026, 6, 11, tzinfo=UTC),
    )


# ---- C1: a lane that paused needs_user produces the NEEDS_USER fork ----------


def test_open_pause_detected(tmp_path: Path) -> None:
    """C1: _has_open_pause reads an unresolved needs_user pause for the wave."""
    state_path = _write_state(tmp_path)
    assert _has_open_pause(state_path, _WAVE_ID) is False
    _record_pause(state_path, _WAVE_ID)
    assert _has_open_pause(state_path, _WAVE_ID) is True


def test_live_watcher_reports_needs_user_on_open_pause(tmp_path: Path) -> None:
    """C1: the live watcher reports needs_user when the lane's wave has an open pause."""
    state_path = _write_state(tmp_path)
    _record_pause(state_path, _WAVE_ID)
    ctx = _ctx(state_path)
    watcher = build_liveness_watcher(
        is_alive=lambda pgid: True, poll_seconds=0.0, sleep=lambda _s: None
    )
    assert watcher(ctx, _lane()) == "needs_user"


def test_needs_user_lane_produces_needs_user_split_fork(tmp_path: Path) -> None:
    """C1: a needs_user lane forks NEEDS_USER_SPLIT (a DL-6 blocking fork).

    The loop drives the live watcher over a wave with an open pause; the watcher
    reports needs_user, so the lane is enqueued as a NEEDS_USER_SPLIT fork rather
    than wedging or failing -- the operator-input fork outcome is reachable live.
    """
    state_path = _write_state(tmp_path)
    _record_pause(state_path, _WAVE_ID)
    ctx = _ctx(state_path)
    watcher = build_liveness_watcher(
        is_alive=lambda pgid: True, poll_seconds=0.0, sleep=lambda _s: None
    )

    run = arm_drive(
        ctx,
        frontier=[_WAVE_ID],
        concurrency=1,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=watcher,
        recompute_frontier=False,
    )
    assert run.run_state is FleetRunState.DONE
    reasons = {fork.reason for fork in run.forks}
    assert FleetForkReason.NEEDS_USER_SPLIT in reasons
    # A needs_user pause lands on the blocked safety tally, not a hard failure.
    assert run.counters.blocked >= 1
    assert run.counters.failed == 0


# ---- C2: drive resolves jury block authority via the close-gate resolver -----


def test_resolve_run_block_authority_default_advisory(tmp_path: Path) -> None:
    """C2: the run resolver returns ADVISORY on the empty validation substrate.

    The drive resolves authority through the same _resolve_jury_block_authority
    the close gate uses; the substrate is empty today, so it returns ADVISORY --
    high / ui lanes fork rather than silently auto-closing.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    assert _resolve_run_block_authority(ctx) is BlockAuthority.ADVISORY


def test_resolve_run_block_authority_stateless_advisory() -> None:
    """C2: a stateless context resolves ADVISORY (no substrate to score)."""
    ctx = MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=1,
        protocol_version="1",
        version="0.6.0",
        state_path=None,
    )
    assert _resolve_run_block_authority(ctx) is BlockAuthority.ADVISORY


# ---- C3: killing a single-wave dispatched session (no fleet run) succeeds -----


def test_kill_dispatched_session_with_no_fleet_run_succeeds(tmp_path: Path) -> None:
    """C3: a single-wave dispatched session (no fleet run) is killable via its pid.

    The wave has no fleet lane but its SessionAttempt records a subprocess_pid;
    the kill falls back to that pid and signals its group -- a non-fleet
    dispatched session is reaped rather than reported not-found.
    """
    state_path = _write_state(tmp_path, with_session_pid=54321)
    ctx = _ctx(state_path)
    sent: list[tuple[int, bool]] = []

    import signal as _signal

    def _cancel(pgid: int, *, hard: bool) -> CancelResult:
        sent.append((pgid, hard))
        return CancelResult(
            pgid=pgid, signal_sent=int(_signal.SIGKILL if hard else _signal.SIGTERM), delivered=True
        )

    result = kill_lane(ctx, wave_id=_WAVE_ID, attempt=1, hard=True, cancel=_cancel)
    assert result.killed is True
    assert result.reason is None
    # The session's recorded pid was signalled (the non-lane fallback).
    assert sent == [(54321, True)]


def test_kill_dispatched_session_no_pid_is_unkillable(tmp_path: Path) -> None:
    """C3: a session with no recorded pid returns the unkillable-session not-found."""
    state_path = _write_state(tmp_path, with_session_pid=None)
    # Add a session row without a pid by writing one with subprocess_pid omitted.
    state = load_state(state_path)
    from eawf.kernel.state.models import SessionAttempt

    state.waves[_WAVE_ID].sessions[1] = SessionAttempt(
        attempt=1,
        runtime="claude-code",
        session_id="ses-x",
        session_log_handle="urn:eawf:v1:session-log:claude-code:abc",
        started_at=datetime(2026, 6, 11, tzinfo=UTC),
        subprocess_pid=None,
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    ctx = _ctx(state_path)

    result = kill_lane(ctx, wave_id=_WAVE_ID, attempt=1, hard=True, cancel=lambda *a, **k: None)
    assert result.killed is False
    assert result.reason == "unkillable-session"
