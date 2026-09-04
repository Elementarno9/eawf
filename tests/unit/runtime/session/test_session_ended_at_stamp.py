"""REL-009: ``ended_at`` is stamped at process exit, not at later reconciliation.

Before this seam the only writer of :attr:`AgentSession.ended_at` was
:func:`~eawf.runtime.session.store.reconcile_orphaned_sessions`, which runs at
the *next* daemon boot. A session that really ended at 14:02 was therefore
recorded as ending whenever the daemon happened to restart, so every duration
derived from it was wrong by minutes or days and the telemetry duration
distribution was unusable.

These tests pin the fixed contract against the on-disk ``state.json``:

- the ``session_end`` and ``agent_end`` hook paths both stamp ``ended_at`` at
  the event's own instant, and the row leaves ``active_session_ids``;
- the boot-time reconcile then finds nothing left to flip, which is the
  observable difference between "stamped at exit" and "stamped at reconcile";
- ambiguous, absent, and already-terminal cases are non-blocking no-ops.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus, StoreKind
from eawf.kernel.state.models import AgentSession, State
from eawf.kernel.store.paths import store_path
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.runner import stamp_session_end_on_exit
from eawf.runtime.session.store import reconcile_orphaned_sessions, stamp_session_end_at_exit

pytestmark = pytest.mark.unit

_STARTED = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
_EXITED = _STARTED + timedelta(minutes=17)
_SCOPE = "P31-I01-W08"

_FIXTURE_STATE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


def _session(session_id: str, *, runtime_session_id: str | None, scope_id: str) -> AgentSession:
    return AgentSession(
        id=session_id,
        role=AgentSessionRole.EXECUTOR,
        runtime="claude",
        scope_id=scope_id,
        status=AgentSessionStatus.ACTIVE,
        started_at=_STARTED,
        runtime_session_id=runtime_session_id,
    )


def _write_state(tmp_path: Path, *sessions: AgentSession) -> Path:
    """Write a ``.ea/state.json`` carrying *sessions* as ACTIVE rows."""
    state = State.model_validate(orjson.loads(_FIXTURE_STATE.read_bytes()))
    for session in sessions:
        state.agent_sessions[session.id] = session
        state.current.active_session_ids.append(session.id)
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))
    return state_path


def _reload(state_path: Path) -> State:
    return State.model_validate(orjson.loads(state_path.read_bytes()))


def _exit_event(event_type: HookEventType, *, provider_session_id: str | None) -> HookEvent:
    payloads: dict[str, dict[str, object]] = {}
    if provider_session_id is not None:
        payloads["claude"] = {"session_id": provider_session_id}
    return HookEvent(
        event_type=event_type,
        scope_id=_SCOPE,
        runtime="claude",
        occurred_at=_EXITED,
        payloads=payloads,
    )


@pytest.mark.parametrize(
    "event_type",
    [HookEventType.SESSION_END, HookEventType.AGENT_END],
)
def test_hook_path_stamps_ended_at_at_process_exit(
    tmp_path: Path, event_type: HookEventType
) -> None:
    """Both exit hook paths write ``ended_at`` at the event's own instant."""
    state_path = _write_state(
        tmp_path, _session("SES-1", runtime_session_id="vendor-1", scope_id=_SCOPE)
    )

    result = stamp_session_end_on_exit(
        _exit_event(event_type, provider_session_id="vendor-1"), repo_root=tmp_path
    )

    assert result.block is False
    assert "session.end_stamp ok" in result.output
    state = _reload(state_path)
    assert state.agent_sessions["SES-1"].ended_at == _EXITED
    assert state.agent_sessions["SES-1"].status is AgentSessionStatus.CLOSED
    assert state.current.active_session_ids == []


def test_exit_stamped_ended_at_survives_the_boot_reconcile(tmp_path: Path) -> None:
    """The reconcile finds nothing to flip once the exit path already stamped.

    This is the whole REL-009 claim: the end instant belongs to the exit, so a
    later daemon boot must not be the thing that decides it.
    """
    state_path = _write_state(
        tmp_path, _session("SES-1", runtime_session_id="vendor-1", scope_id=_SCOPE)
    )
    events_path = store_path(state_path, StoreKind.EVENT)

    stamped = stamp_session_end_at_exit(
        state_path, events_path, runtime_session_id="vendor-1", now=_EXITED
    )
    flipped = reconcile_orphaned_sessions(state_path, events_path)

    assert stamped == "SES-1"
    assert flipped == 0
    assert _reload(state_path).agent_sessions["SES-1"].ended_at == _EXITED


def test_stamp_ended_at_appends_a_close_event(tmp_path: Path) -> None:
    """The exit stamp leaves a ``session.close`` row in the event store."""
    state_path = _write_state(
        tmp_path, _session("SES-1", runtime_session_id="vendor-1", scope_id=_SCOPE)
    )
    events_path = store_path(state_path, StoreKind.EVENT)

    stamp_session_end_at_exit(state_path, events_path, runtime_session_id="vendor-1", now=_EXITED)

    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    payloads = [orjson.loads(line)["payload"] for line in lines]
    assert [row["event_type"] for row in payloads] == ["session.close"]


def test_stamp_ended_at_resolves_the_sole_live_session_without_vendor_id(tmp_path: Path) -> None:
    """Boundary: a runtime that supplies no session id still resolves one live row."""
    state_path = _write_state(tmp_path, _session("SES-1", runtime_session_id=None, scope_id=_SCOPE))

    stamped = stamp_session_end_at_exit(
        state_path, store_path(state_path, StoreKind.EVENT), now=_EXITED
    )

    assert stamped == "SES-1"


def test_stamp_ended_at_narrows_ambiguous_live_sessions_by_scope(tmp_path: Path) -> None:
    """Two live sessions are disambiguated by the exiting process's scope."""
    state_path = _write_state(
        tmp_path,
        _session("SES-1", runtime_session_id=None, scope_id=_SCOPE),
        _session("SES-2", runtime_session_id=None, scope_id="P31-I01-W09"),
    )

    stamped = stamp_session_end_at_exit(
        state_path, store_path(state_path, StoreKind.EVENT), scope_id=_SCOPE, now=_EXITED
    )

    assert stamped == "SES-1"
    assert _reload(state_path).agent_sessions["SES-2"].ended_at is None


def test_stamp_ended_at_declines_when_the_target_is_ambiguous(tmp_path: Path) -> None:
    """Error path: two live rows and no discriminator stamps neither row.

    Guessing here would write a wrong ``ended_at`` onto a session that is still
    running, poisoning its duration permanently; declining leaves the boot
    reconcile as the fallback.
    """
    state_path = _write_state(
        tmp_path,
        _session("SES-1", runtime_session_id=None, scope_id=_SCOPE),
        _session("SES-2", runtime_session_id=None, scope_id=_SCOPE),
    )

    stamped = stamp_session_end_at_exit(
        state_path, store_path(state_path, StoreKind.EVENT), now=_EXITED
    )

    state = _reload(state_path)
    assert stamped is None
    assert all(session.ended_at is None for session in state.agent_sessions.values())


def test_stamp_ended_at_is_idempotent_across_a_double_exit(tmp_path: Path) -> None:
    """A runtime firing both ``session_end`` and ``agent_end`` stamps once."""
    state_path = _write_state(
        tmp_path, _session("SES-1", runtime_session_id="vendor-1", scope_id=_SCOPE)
    )
    events_path = store_path(state_path, StoreKind.EVENT)

    first = stamp_session_end_at_exit(
        state_path, events_path, runtime_session_id="vendor-1", now=_EXITED
    )
    second = stamp_session_end_at_exit(
        state_path,
        events_path,
        runtime_session_id="vendor-1",
        now=_EXITED + timedelta(minutes=5),
    )

    assert (first, second) == ("SES-1", None)
    assert _reload(state_path).agent_sessions["SES-1"].ended_at == _EXITED


def test_stamp_ended_at_on_missing_state_is_a_no_op(tmp_path: Path) -> None:
    """Boundary: no ``state.json`` yet is a clean no-op, never a crash."""
    state_path = tmp_path / ".ea" / "state.json"

    assert stamp_session_end_at_exit(state_path, tmp_path / "event.jsonl", now=_EXITED) is None


def test_stamp_ended_at_on_empty_session_table_is_a_no_op(tmp_path: Path) -> None:
    """Boundary: an empty session table resolves no target."""
    state_path = _write_state(tmp_path)

    assert (
        stamp_session_end_at_exit(state_path, store_path(state_path, StoreKind.EVENT), now=_EXITED)
        is None
    )


def test_stamp_ended_at_rejects_a_non_terminal_status(tmp_path: Path) -> None:
    """Error path: ``ACTIVE`` is not an end state, so it fails fast."""
    state_path = _write_state(
        tmp_path, _session("SES-1", runtime_session_id="vendor-1", scope_id=_SCOPE)
    )

    with pytest.raises(ValueError, match="terminal status"):
        stamp_session_end_at_exit(
            state_path,
            store_path(state_path, StoreKind.EVENT),
            status=AgentSessionStatus.ACTIVE,
        )


def test_hook_ended_at_stamp_on_unreachable_state_does_not_block(tmp_path: Path) -> None:
    """Error path: an exit hook over a repo with no ``.ea/`` degrades cleanly."""
    result = stamp_session_end_on_exit(
        _exit_event(HookEventType.SESSION_END, provider_session_id="vendor-1"),
        repo_root=tmp_path / "absent",
    )

    assert result.block is False
    assert "skipped" in result.output
