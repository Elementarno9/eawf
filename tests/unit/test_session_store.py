"""Unit tests for session.store: start / checkpoint / close + dual-session rejection."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
)
from eawf.kernel.state.models import State
from eawf.runtime.session.store import (
    SessionConflict,
    SessionNotFound,
    append_event,
    checkpoint,
    close_session,
    reconcile_orphaned_sessions,
    start_session,
)
from eawf.workflow.evidence._io import load_state


def _make_state() -> State:
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def test_start_session_creates_active_record(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    result = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
    )
    assert result.session.id.startswith("SES-")
    assert result.session.status is AgentSessionStatus.ACTIVE
    assert result.session.id in state.agent_sessions
    assert result.session.id in state.current.active_session_ids
    # Event row appended.
    lines = events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["payload"]["event_type"] == "session.start"


def test_start_session_rejects_duplicate_scope_runtime(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
    )
    with pytest.raises(SessionConflict, match="active session already exists"):
        start_session(
            state=state,
            events_path=events,
            role=AgentSessionRole.PLANNER,
            scope_id="QR",
            runtime="claude",
        )


def test_start_session_allows_different_runtime(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    a = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
    )
    b = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="opencode",
    )
    assert a.session.id != b.session.id
    assert {a.session.id, b.session.id} <= set(state.agent_sessions)


def test_start_session_allows_after_close(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    a = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 10, tzinfo=UTC),
    )
    close_session(
        state=state,
        events_path=events,
        session_id=a.session.id,
        status=AgentSessionStatus.CLOSED,
    )
    b = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 11, tzinfo=UTC),
    )
    assert b.session.status is AgentSessionStatus.ACTIVE


def test_checkpoint_changes_status_to_checkpointed(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
    )
    result = checkpoint(
        state=state,
        events_path=events,
        session_id=started.session.id,
        artifact_ids=["ART-001"],
        file_globs=["src/**/*.py"],
    )
    assert result.session.status is AgentSessionStatus.CHECKPOINTED
    assert "ART-001" in result.session.artifact_ids


def test_checkpoint_unknown_session_raises(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    with pytest.raises(SessionNotFound):
        checkpoint(
            state=state,
            events_path=events,
            session_id="SES-NOPE",
        )


def test_checkpoint_dedupes_artifact_ids(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
    )
    checkpoint(
        state=state,
        events_path=events,
        session_id=started.session.id,
        artifact_ids=["ART-001"],
    )
    result = checkpoint(
        state=state,
        events_path=events,
        session_id=started.session.id,
        artifact_ids=["ART-001"],
        # uses different timestamp so envelope id differs
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )
    # ART-001 should not be duplicated in artifact_ids.
    assert result.session.artifact_ids.count("ART-001") == 1


def test_close_session_terminal_status(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
    )
    result = close_session(
        state=state,
        events_path=events,
        session_id=started.session.id,
        status=AgentSessionStatus.CLOSED,
        summary="Wrapped up the work",
    )
    assert result.session.status is AgentSessionStatus.CLOSED
    assert result.session.ended_at is not None
    assert result.session.summary == "Wrapped up the work"
    assert started.session.id not in state.current.active_session_ids


def test_close_session_unknown_raises(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    with pytest.raises(SessionNotFound):
        close_session(
            state=state,
            events_path=events,
            session_id="SES-MISSING",
            status=AgentSessionStatus.CLOSED,
        )


def test_close_session_rejects_non_terminal_status(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
    )
    with pytest.raises(ValueError, match="terminal status"):
        close_session(
            state=state,
            events_path=events,
            session_id=started.session.id,
            status=AgentSessionStatus.ACTIVE,
        )


def test_append_event_writes_jsonl(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    moment = datetime(2026, 5, 8, tzinfo=UTC)
    env = append_event(
        events_path=events,
        event_id="EVT-001",
        event_type="test.event",
        actor="test",
        command="test command",
        args_hash="abc",
        status="ok",
        message="hello",
        scope_id="QR",
        occurred_at=moment,
    )
    assert env.id == "EVT-001"
    lines = events.read_text(encoding="utf-8").splitlines()
    parsed = json.loads(lines[0])
    assert parsed["payload"]["event_type"] == "test.event"


def test_close_session_clears_from_current_active(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
    )
    assert started.session.id in state.current.active_session_ids
    close_session(
        state=state,
        events_path=events,
        session_id=started.session.id,
        status=AgentSessionStatus.FAILED,
    )
    assert started.session.id not in state.current.active_session_ids


def test_explicit_now_used_in_started_at(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    moment = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    result = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=moment,
    )
    assert result.session.started_at == moment


def test_session_id_includes_role_and_runtime(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    moment = datetime(2026, 5, 8, 12, tzinfo=UTC)
    result = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.RESEARCHER,
        scope_id="QR",
        runtime="claude",
        now=moment,
    )
    assert "researcher" in result.session.id
    assert "claude" in result.session.id


def test_close_after_failed_state_supports_replay(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="generic",
    )
    close_session(
        state=state,
        events_path=events,
        session_id=started.session.id,
        status=AgentSessionStatus.FAILED,
        summary="Crashed on commit",
    )
    # Re-close is idempotent in the sense that we can call it again to a
    # different terminal status; tested for fault recovery scenarios.
    close_session(
        state=state,
        events_path=events,
        session_id=started.session.id,
        status=AgentSessionStatus.STALE,
        now=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert state.agent_sessions[started.session.id].status is AgentSessionStatus.STALE


def _seed_state_path(tmp_path: Path, sessions: dict[str, str]) -> Path:
    """Write a ``state.json`` seeded with ``{sid: status-value}`` agent sessions.

    ``current.active_session_ids`` mirrors on-disk reality: only sessions whose
    status is ``active`` are listed. Returns the written ``state.json`` path.
    """
    agent_sessions = {
        sid: {
            "id": sid,
            "role": "executor",
            "runtime": f"claude-{idx}",
            "scope_id": "QR",
            "status": status,
            "claimed_wave_ids": [],
            "worktree_ids": [],
            "artifact_ids": [],
            "started_at": "2026-05-08T00:00:00Z",
            "ended_at": None,
            "summary": None,
        }
        for idx, (sid, status) in enumerate(sessions.items())
    }
    active_ids = [sid for sid, status in sessions.items() if status == "active"]
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": active_ids,
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": agent_sessions,
        "plugins": {},
        "indexes": {},
    }
    state = State.model_validate(payload)
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


def test_reconcile_orphaned_sessions_flips_active_to_stale(tmp_path: Path) -> None:
    state_path = _seed_state_path(tmp_path, {"SES-A": "active"})
    events = tmp_path / "events.jsonl"

    flipped = reconcile_orphaned_sessions(state_path, events)

    assert flipped == 1
    # Persisted: reload from disk and assert the flip stuck.
    reloaded = load_state(state_path)
    assert reloaded.agent_sessions["SES-A"].status is AgentSessionStatus.STALE
    assert reloaded.agent_sessions["SES-A"].ended_at is not None
    assert reloaded.agent_sessions["SES-A"].summary == "orphaned by daemon restart"
    # Dropped from the active pointer set.
    assert "SES-A" not in reloaded.current.active_session_ids
    # A session.close event was appended for the orphan.
    lines = events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["payload"]["event_type"] == "session.close"
    assert parsed["id"] == "SES-A-close"


def test_reconcile_orphaned_sessions_leaves_terminal_untouched(tmp_path: Path) -> None:
    state_path = _seed_state_path(
        tmp_path,
        {
            "SES-ACTIVE": "active",
            "SES-CLOSED": "closed",
            "SES-FAILED": "failed",
            "SES-STALE": "stale",
            "SES-CKPT": "checkpointed",
        },
    )
    events = tmp_path / "events.jsonl"

    flipped = reconcile_orphaned_sessions(state_path, events)

    # Only the one ACTIVE session is flipped.
    assert flipped == 1
    reloaded = load_state(state_path)
    assert reloaded.agent_sessions["SES-ACTIVE"].status is AgentSessionStatus.STALE
    assert reloaded.agent_sessions["SES-CLOSED"].status is AgentSessionStatus.CLOSED
    assert reloaded.agent_sessions["SES-FAILED"].status is AgentSessionStatus.FAILED
    assert reloaded.agent_sessions["SES-STALE"].status is AgentSessionStatus.STALE
    assert reloaded.agent_sessions["SES-CKPT"].status is AgentSessionStatus.CHECKPOINTED
    # Exactly one close event -- terminal rows did not emit one.
    lines = events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_reconcile_orphaned_sessions_no_sessions_is_noop(tmp_path: Path) -> None:
    state_path = _seed_state_path(tmp_path, {})
    events = tmp_path / "events.jsonl"
    mtime_before = state_path.stat().st_mtime_ns

    flipped = reconcile_orphaned_sessions(state_path, events)

    assert flipped == 0
    # No write (no-op) and no event file created.
    assert state_path.stat().st_mtime_ns == mtime_before
    assert not events.exists()


def test_reconcile_orphaned_sessions_idempotent(tmp_path: Path) -> None:
    state_path = _seed_state_path(tmp_path, {"SES-A": "active", "SES-B": "active"})
    events = tmp_path / "events.jsonl"

    first = reconcile_orphaned_sessions(state_path, events)
    second = reconcile_orphaned_sessions(state_path, events)

    assert first == 2
    assert second == 0
    reloaded = load_state(state_path)
    assert reloaded.agent_sessions["SES-A"].status is AgentSessionStatus.STALE
    assert reloaded.agent_sessions["SES-B"].status is AgentSessionStatus.STALE
    # Only the first run appended close events (one per orphan); the second is a no-op.
    lines = events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
