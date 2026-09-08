"""Prove the runtime itself writes the session-close events the projector reads.

The projector was verified against a hand-authored fixture log, which proves
the read side only: a rebuild over the log the code *actually writes* still
reported zero sessions because nothing emitted the event. These tests drive the
real session-store lifecycle -- open, then each of the terminal paths -- against
a fixture runtime directory under ``tmp_path``, then rebuild over the very log
those calls produced. Nothing here touches the repository's own ``.ea/``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.kinds.event import validate_event_payload
from eawf.kernel.store.kinds.events.session_closed import (
    SESSION_PAYLOAD_SCHEMA_VERSION,
    SessionClosedPayload,
)
from eawf.kernel.store.paths import store_path
from eawf.observability.telemetry.models import TelemetrySession
from eawf.observability.telemetry.projector import RebuildMode, SourceSpec, rebuild
from eawf.observability.telemetry.sources.event_jsonl import EventJsonlSource
from eawf.observability.telemetry.store import SqliteMetricsStore
from eawf.observability.telemetry.store.base import AbstractMetricsStore
from eawf.runtime.daemon.dispatch_runner import emit_session_closed
from eawf.runtime.session.store import (
    close_session,
    reconcile_orphaned_sessions,
    stamp_session_end_at_exit,
    start_session,
    terminalize_session,
)

_OPENED = datetime(2026, 6, 3, 9, 0, tzinfo=UTC)
_PROJECT_ID = "proj-live"
_RUNTIME = "claude-code"
_SESSION_CLOSED = "session_closed"


def _make_state() -> State:
    """Return a minimal valid project state with no sessions."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": "2026-06-03T00:00:00Z",
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
    )


def _fixture_paths(tmp_path: Path) -> tuple[Path, Path]:
    """Return the ``(state_path, event_path)`` pair of a fixture runtime dir.

    The directory is a throwaway ``.ea/`` under *tmp_path*, never the
    repository's own state store.
    """
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return state_path, store_path(state_path, StoreKind.EVENT)


def _write_state(state_path: Path, state: State) -> None:
    """Persist *state* so the on-disk terminal paths can load it."""
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))


def _closed_payloads(event_path: Path) -> list[SessionClosedPayload]:
    """Return every typed ``session_closed`` payload on the event log."""
    if not event_path.is_file():
        return []
    payloads: list[SessionClosedPayload] = []
    for line in event_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)["payload"]
        if payload.get("event_type") == _SESSION_CLOSED:
            payloads.append(SessionClosedPayload.model_validate(payload))
    return payloads


def _open_and_close(
    tmp_path: Path,
    *,
    scope_id: str = "P32-I01-W36",
    duration: timedelta = timedelta(minutes=12),
    status: AgentSessionStatus = AgentSessionStatus.CLOSED,
) -> tuple[Path, State]:
    """Drive one real session open + close, returning the log it wrote."""
    state_path, event_path = _fixture_paths(tmp_path)
    state = _make_state()
    started = start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.EXECUTOR,
        scope_id=scope_id,
        runtime=_RUNTIME,
        now=_OPENED,
    )
    close_session(
        state=state,
        events_path=event_path,
        session_id=started.session.id,
        status=status,
        now=_OPENED + duration,
    )
    _write_state(state_path, state)
    return event_path, state


def _rebuild_sessions(tmp_path: Path) -> tuple[int, list[TelemetrySession]]:
    """Rebuild the telemetry projection over the fixture log and read rows back."""
    state_path, _ = _fixture_paths(tmp_path)
    store: AbstractMetricsStore = SqliteMetricsStore(tmp_path / "telemetry.db")
    store.init_schema()
    try:
        report = rebuild(
            store,
            [SourceSpec(source=EventJsonlSource(), root=state_path, project_id=_PROJECT_ID)],
            mode=RebuildMode.FULL,
        )
        rows = [
            row
            for row in store.fetch_all("telemetry_sessions", TelemetrySession)
            if isinstance(row, TelemetrySession)
        ]
    finally:
        store.close()
    return report.sessions, sorted(rows, key=lambda row: row.session_id)


def test_session_close_emits_event(tmp_path: Path) -> None:
    """A real close writes exactly one typed event carrying the session facts."""
    event_path, state = _open_and_close(tmp_path)
    (session_id,) = state.agent_sessions

    payloads = _closed_payloads(event_path)

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload.session_id == session_id
    assert payload.opened_at == _OPENED
    assert payload.timestamp == _OPENED + timedelta(minutes=12)
    assert payload.runtime == "claude"
    assert payload.payload_schema_version == SESSION_PAYLOAD_SCHEMA_VERSION
    assert payload.end_marker == "clean_stop"
    assert payload.wave_id == "P32-I01-W36"


def test_session_close_emits_event_validating_at_the_write_boundary(tmp_path: Path) -> None:
    """The emitted row passes the store's own typed-payload validation."""
    event_path, _ = _open_and_close(tmp_path)

    rows = [
        json.loads(line)["payload"]
        for line in event_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    typed = [validate_event_payload(row) for row in rows]

    assert any(isinstance(model, SessionClosedPayload) for model in typed)


def test_session_close_emits_event_per_terminating_session(tmp_path: Path) -> None:
    """Two sessions closing produce two events -- one each, no duplicates."""
    state_path, event_path = _fixture_paths(tmp_path)
    state = _make_state()
    ids = []
    for index, scope in enumerate(("P32-I01-W01", "P32-I01-W02")):
        started = start_session(
            state=state,
            events_path=event_path,
            role=AgentSessionRole.EXECUTOR,
            scope_id=scope,
            runtime=_RUNTIME,
            now=_OPENED + timedelta(seconds=index),
        )
        ids.append(started.session.id)
    for index, session_id in enumerate(ids):
        close_session(
            state=state,
            events_path=event_path,
            session_id=session_id,
            status=AgentSessionStatus.CLOSED,
            now=_OPENED + timedelta(minutes=index + 1),
        )
    _write_state(state_path, state)

    payloads = _closed_payloads(event_path)

    assert [payload.session_id for payload in payloads] == ids


def test_session_close_emits_event_on_terminalize_path(tmp_path: Path) -> None:
    """The crash path (terminalize to FAILED) emits too, marked as such."""
    state_path, event_path = _fixture_paths(tmp_path)
    state = _make_state()
    started = start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.AUDITOR,
        scope_id="P32-I01-W36",
        runtime=_RUNTIME,
        now=_OPENED,
    )
    terminalize_session(
        state=state,
        events_path=event_path,
        session_id=started.session.id,
        status=AgentSessionStatus.FAILED,
        summary="auditor attempt failed before report append",
        now=_OPENED + timedelta(minutes=3),
    )
    _write_state(state_path, state)

    payloads = _closed_payloads(event_path)

    assert len(payloads) == 1
    assert payloads[0].end_marker == "other"
    assert payloads[0].timestamp == _OPENED + timedelta(minutes=3)


def test_session_close_emits_event_on_process_exit_path(tmp_path: Path) -> None:
    """The exit-stamp path emits the close event it stamps."""
    state_path, event_path = _fixture_paths(tmp_path)
    state = _make_state()
    started = start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.EXECUTOR,
        scope_id="P32-I01-W36",
        runtime=_RUNTIME,
        now=_OPENED,
    )
    _write_state(state_path, state)

    stamped = stamp_session_end_at_exit(
        state_path,
        event_path,
        scope_id="P32-I01-W36",
        now=_OPENED + timedelta(minutes=8),
    )

    payloads = _closed_payloads(event_path)
    assert stamped == started.session.id
    assert len(payloads) == 1
    assert payloads[0].session_id == started.session.id
    assert payloads[0].end_marker == "clean_stop"


def test_session_close_emits_event_on_reconcile_path(tmp_path: Path) -> None:
    """The daemon-boot orphan reconcile emits an ``away`` close per session."""
    state_path, event_path = _fixture_paths(tmp_path)
    state = _make_state()
    start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.EXECUTOR,
        scope_id="P32-I01-W36",
        runtime=_RUNTIME,
        now=_OPENED,
    )
    _write_state(state_path, state)

    flipped = reconcile_orphaned_sessions(state_path, event_path)

    payloads = _closed_payloads(event_path)
    assert flipped == 1
    assert len(payloads) == 1
    assert payloads[0].end_marker == "away"


def test_session_close_emits_event_for_zero_length_session(tmp_path: Path) -> None:
    """A session that opens and closes on the same instant still emits."""
    event_path, _ = _open_and_close(tmp_path, duration=timedelta(0))

    payloads = _closed_payloads(event_path)

    assert len(payloads) == 1
    assert payloads[0].timestamp == payloads[0].opened_at


def test_session_close_emits_no_event_for_non_wave_scope(tmp_path: Path) -> None:
    """An interactive session still emits, with no wave bound to it."""
    event_path, _ = _open_and_close(tmp_path, scope_id="QR")

    payloads = _closed_payloads(event_path)

    assert len(payloads) == 1
    assert payloads[0].wave_id is None


def test_emit_session_closed_skips_active_session(tmp_path: Path) -> None:
    """A session that has not terminalized has no close to report."""
    state_path, event_path = _fixture_paths(tmp_path)
    state = _make_state()
    started = start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.EXECUTOR,
        scope_id="P32-I01-W36",
        runtime=_RUNTIME,
        now=_OPENED,
    )
    _write_state(state_path, state)

    assert emit_session_closed(event_path, session=started.session) is None
    assert _closed_payloads(event_path) == []


def test_emit_session_closed_skips_unprojectable_runtime(tmp_path: Path) -> None:
    """A runtime outside the closed telemetry set is reported, not coerced."""
    state_path, event_path = _fixture_paths(tmp_path)
    state = _make_state()
    started = start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.EXECUTOR,
        scope_id="P32-I01-W36",
        runtime="not-a-runtime",
        now=_OPENED,
    )
    closed = close_session(
        state=state,
        events_path=event_path,
        session_id=started.session.id,
        status=AgentSessionStatus.CLOSED,
        now=_OPENED + timedelta(minutes=1),
    )
    _write_state(state_path, state)

    assert closed.session.status is AgentSessionStatus.CLOSED
    assert _closed_payloads(event_path) == []


def test_emit_session_closed_skips_close_before_open(tmp_path: Path) -> None:
    """A reversed pair would project a negative duration, so it is not emitted."""
    _, event_path = _fixture_paths(tmp_path)
    state = _make_state()
    started = start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.EXECUTOR,
        scope_id="P32-I01-W36",
        runtime=_RUNTIME,
        now=_OPENED,
    )
    reversed_session = started.session.model_copy(
        update={
            "status": AgentSessionStatus.CLOSED,
            "ended_at": _OPENED - timedelta(minutes=1),
        }
    )

    assert emit_session_closed(event_path, session=reversed_session) is None
    assert _closed_payloads(event_path) == []


def test_emit_session_closed_requires_a_session(tmp_path: Path) -> None:
    """The emitter takes a typed session row, not a loose mapping."""
    _, event_path = _fixture_paths(tmp_path)

    with pytest.raises(AttributeError):
        emit_session_closed(event_path, session={"id": "SES-x"})  # type: ignore[arg-type]


def test_live_rebuild_reports_sessions(tmp_path: Path) -> None:
    """A rebuild over the log the runtime wrote reports a non-zero session count."""
    state_path, event_path = _fixture_paths(tmp_path)
    state = _make_state()
    durations = {
        "P32-I01-W01": timedelta(minutes=5),
        "P32-I01-W02": timedelta(minutes=30),
        "P32-I01-W03": timedelta(minutes=90, milliseconds=250),
    }
    expected_ms: dict[str, int] = {}
    for index, (scope, duration) in enumerate(durations.items()):
        started = start_session(
            state=state,
            events_path=event_path,
            role=AgentSessionRole.EXECUTOR,
            scope_id=scope,
            runtime=_RUNTIME,
            now=_OPENED + timedelta(seconds=index),
        )
        close_session(
            state=state,
            events_path=event_path,
            session_id=started.session.id,
            status=AgentSessionStatus.CLOSED,
            now=_OPENED + timedelta(seconds=index) + duration,
        )
        expected_ms[started.session.id] = duration // timedelta(milliseconds=1)
    _write_state(state_path, state)

    projected, rows = _rebuild_sessions(tmp_path)

    assert projected > 0
    assert projected == len(durations)
    assert {row.session_id for row in rows} == set(expected_ms)
    for row in rows:
        assert row.duration_ms == expected_ms[row.session_id]
        assert row.started_at is not None
        assert row.ended_at is not None
        assert row.duration_ms == (row.ended_at - row.started_at) // timedelta(milliseconds=1)
        assert row.project_id == _PROJECT_ID
        assert row.runtime == "claude"


def test_live_rebuild_reports_sessions_from_every_terminal_path(tmp_path: Path) -> None:
    """Clean close, crash terminalize, exit stamp and boot reconcile all project."""
    state_path, event_path = _fixture_paths(tmp_path)
    state = _make_state()
    clean = start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.EXECUTOR,
        scope_id="P32-I01-W01",
        runtime=_RUNTIME,
        now=_OPENED,
    )
    close_session(
        state=state,
        events_path=event_path,
        session_id=clean.session.id,
        status=AgentSessionStatus.CLOSED,
        now=_OPENED + timedelta(minutes=1),
    )
    crashed = start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.AUDITOR,
        scope_id="P32-I01-W02",
        runtime=_RUNTIME,
        now=_OPENED,
    )
    terminalize_session(
        state=state,
        events_path=event_path,
        session_id=crashed.session.id,
        status=AgentSessionStatus.FAILED,
        now=_OPENED + timedelta(minutes=2),
    )
    exited = start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.EXECUTOR,
        scope_id="P32-I01-W03",
        runtime=_RUNTIME,
        now=_OPENED,
    )
    orphan = start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.EXECUTOR,
        scope_id="P32-I01-W04",
        runtime=_RUNTIME,
        now=_OPENED,
    )
    _write_state(state_path, state)
    stamp_session_end_at_exit(
        state_path,
        event_path,
        scope_id="P32-I01-W03",
        now=_OPENED + timedelta(minutes=3),
    )
    reconcile_orphaned_sessions(state_path, event_path)

    projected, rows = _rebuild_sessions(tmp_path)

    assert projected == 4
    assert {row.session_id for row in rows} == {
        clean.session.id,
        crashed.session.id,
        exited.session.id,
        orphan.session.id,
    }
    assert {row.end_marker for row in rows} == {"clean_stop", "other", "away"}


def test_live_rebuild_reports_no_sessions_without_a_close(tmp_path: Path) -> None:
    """An open session projects nothing -- only a close is a projectable row."""
    state_path, event_path = _fixture_paths(tmp_path)
    state = _make_state()
    start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.EXECUTOR,
        scope_id="P32-I01-W36",
        runtime=_RUNTIME,
        now=_OPENED,
    )
    _write_state(state_path, state)

    projected, rows = _rebuild_sessions(tmp_path)

    assert projected == 0
    assert rows == []
