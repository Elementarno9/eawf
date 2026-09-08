"""Rebuild the telemetry projection over a fixture event log.

Drives the whole session producer end to end: a canonical ``event.jsonl``
store carrying ``session_closed`` events is projected through the real
:class:`~eawf.observability.telemetry.sources.event_jsonl.EventJsonlSource` and
:func:`~eawf.observability.telemetry.projector.rebuild` into a SQLite metrics
store, and the persisted rows are read back.

The fixture log is built here rather than read from the live project store
because the live store carries no session events yet -- a rebuild over it
would be trivially "correct" over an empty corpus, which is the failure this
test exists to catch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.kinds.events.session_closed import SessionClosedPayload
from eawf.observability.telemetry.aggregator import SessionPayloadSchemaError
from eawf.observability.telemetry.models import TelemetrySession
from eawf.observability.telemetry.projector import RebuildMode, SourceSpec, rebuild
from eawf.observability.telemetry.sources.event_jsonl import EventJsonlSource
from eawf.observability.telemetry.store import SqliteMetricsStore
from eawf.observability.telemetry.store.base import AbstractMetricsStore

_OPENED = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
_PROJECT_ID = "proj-fixture"

#: Fixture sessions as ``(session_id, duration)`` pairs -- a short, a medium,
#: and a long session so a wrong duration cannot pass by coincidence.
_FIXTURE_SESSIONS: tuple[tuple[str, timedelta], ...] = (
    ("sess-short", timedelta(minutes=5)),
    ("sess-medium", timedelta(minutes=30)),
    ("sess-long", timedelta(minutes=90, milliseconds=250)),
)


def _session_closed_line(session_id: str, duration: timedelta, *, env_id: str) -> str:
    """Return one JSONL line carrying a typed ``session_closed`` event."""
    payload = SessionClosedPayload(
        timestamp=_OPENED + duration,
        opened_at=_OPENED,
        session_id=session_id,
        runtime="claude",
        session_log_handle=f"urn:eawf:v1:session-log:claude:{session_id}",
        wave_id="W31",
        attempt_id=f"att-{session_id}",
        model="claude-opus",
        end_marker="clean_stop",
        input_tokens=100,
        output_tokens=20,
        cache_creation_input_tokens=5,
        cache_read_input_tokens=50,
    )
    envelope = Envelope(
        id=env_id,
        kind=StoreKind.EVENT,
        scope_id="W31",
        created_at=payload.timestamp,
        updated_at=None,
        summary=f"session_closed {session_id}",
        payload=payload.model_dump(mode="json"),
    )
    return envelope.model_dump_json() + "\n"


def _runtime_switched_line() -> str:
    """Return one JSONL line carrying an incident-bearing event.

    The fixture log interleaves a non-session event so the test also proves
    the session derivation does not displace the incident path that shares
    the same envelope stream.
    """
    envelope = Envelope(
        id="EV-switch",
        kind=StoreKind.EVENT,
        scope_id="W31",
        created_at=_OPENED,
        updated_at=None,
        summary="runtime_switched W31",
        payload={
            "event_type": "runtime_switched",
            "cause": "RUNTIME_TIMEOUT",
            "timestamp": _OPENED.isoformat(),
        },
    )
    return envelope.model_dump_json() + "\n"


def _pre_bump_line() -> str:
    """Return one JSONL line carrying the pre-bump flat session close."""
    payload = EventPayload(
        timestamp=_OPENED,
        event_type="session_closed",
        actor="daemon",
        command="dispatch_runner.close_session",
        args_hash="",
        status="ok",
        message="session closed",
        extras={"session_id": "sess-legacy"},
    ).model_dump(mode="json")
    envelope = Envelope(
        id="EV-legacy",
        kind=StoreKind.EVENT,
        scope_id="W31",
        created_at=_OPENED,
        updated_at=None,
        summary="session_closed sess-legacy",
        payload=payload,
    )
    return envelope.model_dump_json() + "\n"


def _write_event_log(root: Path, body: str) -> Path:
    """Write *body* as the canonical event store under a fresh state dir."""
    state_path = root / ".ea" / "state.json"
    store_dir = state_path.parent / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "event.jsonl").write_text(body, encoding="utf-8")
    return state_path


def _fixture_log_body() -> str:
    """Return the fixture event log: three session closes plus one incident."""
    lines = [
        _session_closed_line(session_id, duration, env_id=f"EV-{session_id}")
        for session_id, duration in _FIXTURE_SESSIONS
    ]
    lines.insert(1, _runtime_switched_line())
    return "".join(lines)


def _open_store(tmp_path: Path) -> AbstractMetricsStore:
    """Return an initialised SQLite metrics store under *tmp_path*."""
    store = SqliteMetricsStore(tmp_path / "telemetry.db")
    store.init_schema()
    return store


def _spec(state_path: Path) -> SourceSpec:
    """Return the event-store source spec the CLI rebuild registers."""
    return SourceSpec(
        source=EventJsonlSource(),
        root=state_path,
        project_id=_PROJECT_ID,
    )


def _stored_sessions(store: AbstractMetricsStore) -> list[TelemetrySession]:
    """Read the persisted session rows back, sorted by session id."""
    rows = store.fetch_all("telemetry_sessions", TelemetrySession)
    typed = [row for row in rows if isinstance(row, TelemetrySession)]
    return sorted(typed, key=lambda row: row.session_id)


def test_rebuild_over_fixture_log_yields_sessions(tmp_path: Path) -> None:
    """A rebuild over the fixture log projects one row per session close."""
    state_path = _write_event_log(tmp_path, _fixture_log_body())
    store = _open_store(tmp_path)
    try:
        report = rebuild(store, [_spec(state_path)], mode=RebuildMode.FULL)
        rows = _stored_sessions(store)
    finally:
        store.close()

    assert report.sessions > 0
    assert report.sessions == len(_FIXTURE_SESSIONS)
    assert report.incidents == 1
    assert len(rows) == len(_FIXTURE_SESSIONS)
    expected = {
        session_id: duration // timedelta(milliseconds=1)
        for session_id, duration in _FIXTURE_SESSIONS
    }
    for row in rows:
        assert row.duration_ms == expected[row.session_id]
        assert row.ended_at is not None
        assert row.started_at is not None
        assert row.duration_ms == (row.ended_at - row.started_at) // timedelta(milliseconds=1)
        assert row.project_id == _PROJECT_ID


def test_rebuild_over_fixture_log_prices_each_session(tmp_path: Path) -> None:
    """Derived rows go through the aggregator, so their cost is priced."""
    state_path = _write_event_log(tmp_path, _fixture_log_body())
    store = _open_store(tmp_path)
    try:
        rebuild(store, [_spec(state_path)], mode=RebuildMode.FULL)
        rows = _stored_sessions(store)
    finally:
        store.close()

    assert all(row.total_cost_usd > 0 for row in rows)


def test_rebuild_over_fixture_log_is_idempotent(tmp_path: Path) -> None:
    """Replaying a full rebuild overwrites rows in place rather than duplicating."""
    state_path = _write_event_log(tmp_path, _fixture_log_body())
    store = _open_store(tmp_path)
    try:
        first = rebuild(store, [_spec(state_path)], mode=RebuildMode.FULL)
        second = rebuild(store, [_spec(state_path)], mode=RebuildMode.FULL)
        rows = _stored_sessions(store)
    finally:
        store.close()

    assert first.sessions == second.sessions == len(_FIXTURE_SESSIONS)
    assert len(rows) == len(_FIXTURE_SESSIONS)


def test_rebuild_incremental_projects_appended_session(tmp_path: Path) -> None:
    """A close event is self-contained, so a tail slice projects its row."""
    state_path = _write_event_log(tmp_path, _fixture_log_body())
    event_log = state_path.parent / "store" / "event.jsonl"
    store = _open_store(tmp_path)
    try:
        rebuild(store, [_spec(state_path)], mode=RebuildMode.FULL)
        with event_log.open("a", encoding="utf-8") as handle:
            handle.write(
                _session_closed_line("sess-appended", timedelta(minutes=7), env_id="EV-appended")
            )
        tail = rebuild(store, [_spec(state_path)], mode=RebuildMode.INCREMENTAL)
        rows = _stored_sessions(store)
    finally:
        store.close()

    assert tail.sessions == 1
    assert {row.session_id for row in rows} == {
        "sess-appended",
        "sess-long",
        "sess-medium",
        "sess-short",
    }


def test_rebuild_over_empty_log_yields_no_sessions(tmp_path: Path) -> None:
    """An empty event store projects nothing and raises nothing."""
    state_path = _write_event_log(tmp_path, "")
    store = _open_store(tmp_path)
    try:
        report = rebuild(store, [_spec(state_path)], mode=RebuildMode.FULL)
        rows = _stored_sessions(store)
    finally:
        store.close()

    assert report.sessions == 0
    assert rows == []


def test_rebuild_over_pre_bump_log_raises(tmp_path: Path) -> None:
    """A pre-bump session close aborts the rebuild instead of projecting nothing."""
    state_path = _write_event_log(tmp_path, _pre_bump_line())
    store = _open_store(tmp_path)
    try:
        with pytest.raises(SessionPayloadSchemaError):
            rebuild(store, [_spec(state_path)], mode=RebuildMode.FULL)
    finally:
        store.close()
