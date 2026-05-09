"""Unit tests for session.recovery: stale promotion at exactly 30 minutes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from eawf.session.recovery import DEFAULT_AGE_MINUTES, recover_sessions
from eawf.session.store import (
    append_event,
    checkpoint,
    close_session,
    start_session,
)
from eawf.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
)
from eawf.state.models import State


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
            "subproject_id": None,
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


def test_default_age_minutes_constant() -> None:
    assert DEFAULT_AGE_MINUTES == 30


def test_recover_session_at_exactly_30_minutes_marks_stale(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
    )
    # Now is exactly 30 minutes after started_at — boundary case.
    now = datetime(2026, 5, 8, 10, 30, tzinfo=UTC)
    report = recover_sessions(state=state, events_path=events, age_minutes=30, now=now)
    assert started.session.id in report.marked_session_ids
    assert state.agent_sessions[started.session.id].status is AgentSessionStatus.STALE


def test_recover_session_under_threshold_skipped(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
    )
    now = datetime(2026, 5, 8, 10, 29, tzinfo=UTC)  # under threshold
    report = recover_sessions(state=state, events_path=events, age_minutes=30, now=now)
    assert started.session.id in report.skipped_session_ids
    assert state.agent_sessions[started.session.id].status is AgentSessionStatus.ACTIVE


def test_recover_skips_already_closed(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
    )
    close_session(
        state=state,
        events_path=events,
        session_id=started.session.id,
        status=AgentSessionStatus.CLOSED,
    )
    report = recover_sessions(
        state=state,
        events_path=events,
        age_minutes=30,
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert started.session.id in report.skipped_session_ids
    assert started.session.id not in report.marked_session_ids


def test_recover_uses_checkpoint_as_heartbeat(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
    )
    # Checkpoint at 10:25 — recent.
    checkpoint(
        state=state,
        events_path=events,
        session_id=started.session.id,
        now=datetime(2026, 5, 8, 10, 25, tzinfo=UTC),
    )
    # Recover at 10:50 — heartbeat (10:25) is 25 minutes old, under 30 m.
    now = datetime(2026, 5, 8, 10, 50, tzinfo=UTC)
    report = recover_sessions(state=state, events_path=events, age_minutes=30, now=now)
    assert started.session.id in report.skipped_session_ids


def test_recover_marks_after_checkpoint_ages(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
    )
    checkpoint(
        state=state,
        events_path=events,
        session_id=started.session.id,
        now=datetime(2026, 5, 8, 10, 30, tzinfo=UTC),
    )
    # Recover at 11:05 — checkpoint (10:30) is 35 minutes old, ≥30.
    now = datetime(2026, 5, 8, 11, 5, tzinfo=UTC)
    report = recover_sessions(state=state, events_path=events, age_minutes=30, now=now)
    assert started.session.id in report.marked_session_ids


def test_recover_emits_session_recover_event(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
    )
    recover_sessions(
        state=state,
        events_path=events,
        age_minutes=30,
        now=datetime(2026, 5, 8, 11, 0, tzinfo=UTC),
    )
    text = events.read_text(encoding="utf-8")
    assert "session.recover" in text
    assert started.session.id in text


def test_recover_clears_active_session_ids(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
    )
    assert started.session.id in state.current.active_session_ids
    recover_sessions(
        state=state,
        events_path=events,
        age_minutes=30,
        now=datetime(2026, 5, 8, 11, 0, tzinfo=UTC),
    )
    assert started.session.id not in state.current.active_session_ids


def test_recover_no_sessions_empty_report(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    report = recover_sessions(
        state=state, events_path=events, age_minutes=30, now=datetime.now(UTC)
    )
    assert report.marked_session_ids == []
    assert report.skipped_session_ids == []


def test_recover_default_threshold(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
    )
    report = recover_sessions(
        state=state,
        events_path=events,
        now=datetime(2026, 5, 8, 10, 0, tzinfo=UTC) + timedelta(minutes=30),
    )
    assert report.age_minutes == 30


def test_recover_checkpointed_session_can_become_stale(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
    )
    checkpoint(
        state=state,
        events_path=events,
        session_id=started.session.id,
        now=datetime(2026, 5, 8, 10, 5, tzinfo=UTC),
    )
    assert state.agent_sessions[started.session.id].status is AgentSessionStatus.CHECKPOINTED
    report = recover_sessions(
        state=state,
        events_path=events,
        age_minutes=30,
        now=datetime(2026, 5, 8, 11, 0, tzinfo=UTC),
    )
    assert started.session.id in report.marked_session_ids
    assert state.agent_sessions[started.session.id].status is AgentSessionStatus.STALE


def test_recover_other_event_types_ignored(tmp_path: Path) -> None:
    state = _make_state()
    events = tmp_path / "events.jsonl"
    started = start_session(
        state=state,
        events_path=events,
        role=AgentSessionRole.EXECUTOR,
        scope_id="QR",
        runtime="claude",
        now=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
    )
    # Append a non-checkpoint event for the same session — should not affect
    # heartbeat calculation.
    append_event(
        events_path=events,
        event_id="EVT-noise",
        event_type="memory.add",
        actor=started.session.id,
        command="memory add",
        args_hash="",
        status="ok",
        message="noise",
        scope_id="QR",
        occurred_at=datetime(2026, 5, 8, 10, 50, tzinfo=UTC),
    )
    report = recover_sessions(
        state=state,
        events_path=events,
        age_minutes=30,
        now=datetime(2026, 5, 8, 11, 0, tzinfo=UTC),
    )
    # Heartbeat = started_at (10:00) + nothing else; 60m > 30m.
    assert started.session.id in report.marked_session_ids
