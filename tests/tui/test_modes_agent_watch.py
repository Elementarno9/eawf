"""Tests for the live agent-watch zoom pane (P29-I04-W11, Watch mode digit 8).

The Watch mode zooms one dispatched session: it STREAMS that session's live
events (filtered off the App's shared ``event.subscribe`` fan-out by the wave
id the session scopes to) and offers a **cancel** control that asks the
daemon to stop the spawned child via the ``agent.kill`` RPC. These tests pin
the two halves:

* the pure helpers --
  :func:`~eawf.surfaces.tui.modes.agent_watch.pick_watch_target` (default
  target selection),
  :func:`~eawf.surfaces.tui.modes.agent_watch.is_watched_event` (the
  stream-filter predicate), and
  :func:`~eawf.surfaces.tui.modes.agent_watch.render_watch_header` -- tested
  against directly-built rows so the logic is verified without mounting
  Textual; and
* the mounted pane under a Pilot: digit ``8`` switches to the mode and the
  breadcrumb trails with the ``Watch`` segment; an honest-empty scope (no
  dispatched executor session) renders the "no active dispatched session"
  banner; a seeded scope (an ACTIVE executor session + buffered events)
  streams the session's events filtered to its wave; and the cancel key
  binding exists + the cancel action issues an ``agent.kill`` request and
  surfaces the (placeholder) result honestly.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` (``pilot.pause()``
is CPU-idle-based, not worker-aware) before asserting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import (
    AgentSession,
    CurrentPointers,
    Project,
    State,
)
from eawf.kernel.store.envelope import Envelope
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.agent_watch import (
    CANCEL_IDLE,
    CANCEL_NO_DAEMON,
    CANCEL_NOT_ACTIVE_TEMPLATE,
    EMPTY_NOTICE,
    LOG_NO_HANDLE,
    PAUSE_NO_DAEMON,
    PAUSE_NO_TARGET,
    SESSION_PICKER_ID,
    SESSION_PICKER_ROW_CLASS,
    WATCH_EMPTY_ID,
    WATCH_HEADER_ID,
    WATCH_OUTPUT_ID,
    WATCH_REPLAY_TEMPLATE,
    WATCH_RESULT_ID,
    WATCH_ROW_CLASS,
    WATCH_TILE_CLASS,
    WATCH_TILE_ROW_CLASS,
    AgentWatchModeScreen,
    SessionPicker,
    WatchGrid,
    WatchTarget,
    WatchTile,
    is_watched_event,
    load_output_chunk_batch,
    load_output_chunk_lines,
    pick_watch_target,
    picker_column_widths,
    render_picker_row,
    render_watch_header,
    session_picker_rows,
    tile_dom_id,
    watch_display_label,
)
from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
    toast_messages,
)
from eawf.surfaces.tui.widgets.output_tail import (
    OUTPUT_TAIL_ROW_CLASS,
    OUTPUT_TAIL_WAITING_ID,
    WAITING_NOTICE,
    OutputTail,
)

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: The digit key that switches to the Watch mode.
_WATCH_DIGIT = "8"

#: The wave the seeded executor session scopes to (its ``scope_id``), which
#: the dispatch events also stamp as their own ``scope_id``.
_WAVE = "P01-I01-W01"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home.

    Keeps a stray ``u`` scope switch (and any registry read) deterministic
    and off the operator's real registry.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _session(
    sid: str = "S-1",
    *,
    scope_id: str = _WAVE,
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
    role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    runtime: str = "claude",
    started_at: datetime = _T0,
) -> AgentSession:
    """Build an agent-session row for the watch-target picker."""
    return AgentSession(
        id=sid,
        role=role,
        runtime=runtime,
        scope_id=scope_id,
        status=status,
        started_at=started_at,
    )


def _target(
    *,
    session_id: str = "S-1",
    wave_id: str = _WAVE,
    runtime: str = "claude",
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
    attempt: int = 1,
    agent_role: AgentSessionRole = AgentSessionRole.EXECUTOR,
) -> WatchTarget:
    """Build a directly-constructed watch target for the render helpers."""
    return WatchTarget(
        session_id=session_id,
        wave_id=wave_id,
        runtime=runtime,
        status=status,
        attempt=attempt,
        agent_role=agent_role,
    )


def _event(
    event_id: str,
    *,
    scope_id: str | None = _WAVE,
    summary: str = "live event",
    kind: str = "event",
) -> Envelope:
    """Build a minimal event-kind envelope keyed to *scope_id*."""
    return Envelope(
        id=event_id,
        kind=kind,  # type: ignore[arg-type]
        scope_id=scope_id,
        created_at=datetime(2026, 5, 27, 9, 30, 15, tzinfo=UTC),
        updated_at=None,
        summary=summary,
        payload={"event_type": "test", "status": "ok", "message": "live"},
    )


def _state(*, sessions: dict[str, AgentSession] | None = None) -> State:
    """Build a minimal repo state, optionally with agent sessions."""
    return State.model_validate(
        {
            "schema_version": "1.3",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": Project(
                code="QR",
                slug="quant-research",
                title="Quant Research",
                domains=["quant"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": (
                {sid: s.model_dump(mode="json") for sid, s in sessions.items()}
                if sessions is not None
                else {}
            ),
            "plugins": {},
            "indexes": {},
        }
    )


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


def _recording_client(
    calls: list[tuple[str, dict[str, object]]],
    *,
    response: dict[str, object] | None = None,
) -> Callable[..., object]:
    """Return a ``DaemonClient`` factory recording each RPC into *calls*.

    The fake client records ``(method, params)`` for every ``call`` and returns
    *response* (defaulting to the daemon's placeholder ``agent.kill`` reply), so
    a test can assert exactly which RPC fired without a live daemon.

    Args:
        calls: The list each ``(method, params)`` is appended to.
        response: The dict the fake ``call`` returns; defaults to the daemon's
            placeholder ``{"killed": False, "signal": "term"}`` kill reply.

    Returns:
        A factory accepting any args + kwargs and returning the fake client.
    """
    reply = response if response is not None else {"killed": False, "signal": "term"}

    class _FakeClient:
        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            return reply

    return lambda *_a, **_k: _FakeClient()


# --------------------------------------------------------------------------
# pick_watch_target -- default selection (boundary cases)
# --------------------------------------------------------------------------


def test_pick_watch_target_none_state_returns_none() -> None:
    """An unbound state yields no watch target (honest-empty path)."""
    assert pick_watch_target(None) is None


def test_pick_watch_target_no_sessions_returns_none() -> None:
    """A scope with no agent sessions yields no watch target."""
    assert pick_watch_target(_state()) is None


def test_pick_watch_target_no_executor_returns_none() -> None:
    """A scope with only non-executor sessions yields no watch target."""
    state = _state(
        sessions={"S-1": _session("S-1", role=AgentSessionRole.REVIEWER)},
    )
    assert pick_watch_target(state) is None


def test_pick_watch_target_picks_the_active_executor() -> None:
    """The single ACTIVE executor session is the default watch target."""
    state = _state(sessions={"S-1": _session("S-1")})
    target = pick_watch_target(state)
    assert target is not None
    assert target.session_id == "S-1"
    assert target.wave_id == _WAVE
    assert target.runtime == "claude"
    assert target.status is AgentSessionStatus.ACTIVE


def test_watch_target_label_surfaces_the_spawn_attempt() -> None:
    """The header label carries the spawn attempt so a retry is legible."""
    state = _state(sessions={"S-1": _session("S-1")})
    target = pick_watch_target(state)
    assert target is not None
    # wave / runtime · attempt N -- the attempt makes attempt-1 vs retry legible.
    assert target.label == f"{_WAVE} / claude · attempt {target.attempt}"
    assert "attempt" in target.label


def test_pick_watch_target_prefers_active_over_closed() -> None:
    """An ACTIVE executor wins over a more-recent CLOSED one."""
    state = _state(
        sessions={
            "S-old": _session("S-old", status=AgentSessionStatus.ACTIVE, started_at=_T0),
            "S-new": _session(
                "S-new",
                status=AgentSessionStatus.CLOSED,
                started_at=_T0 + timedelta(hours=1),
            ),
        }
    )
    target = pick_watch_target(state)
    assert target is not None
    assert target.session_id == "S-old"  # ACTIVE preferred despite being older


def test_pick_watch_target_picks_most_recent_active() -> None:
    """Among ACTIVE executors the most-recently-started is picked."""
    state = _state(
        sessions={
            "S-old": _session("S-old", started_at=_T0),
            "S-new": _session("S-new", started_at=_T0 + timedelta(hours=1)),
        }
    )
    target = pick_watch_target(state)
    assert target is not None
    assert target.session_id == "S-new"


def test_pick_watch_target_falls_back_to_closed_executor() -> None:
    """With no ACTIVE executor, the most-recent executor of any status is picked."""
    state = _state(
        sessions={
            "S-1": _session("S-1", status=AgentSessionStatus.CLOSED, started_at=_T0),
            "S-2": _session(
                "S-2",
                status=AgentSessionStatus.FAILED,
                started_at=_T0 + timedelta(hours=1),
            ),
        }
    )
    target = pick_watch_target(state)
    assert target is not None
    assert target.session_id == "S-2"
    assert target.status is AgentSessionStatus.FAILED


# --------------------------------------------------------------------------
# W20: watch ANY spawned agent role (executor / researcher / auditor)
# --------------------------------------------------------------------------


def test_pick_watch_target_selects_researcher_when_no_executor() -> None:
    """A campaign researcher is a valid default watch target (W20).

    With no executor session and one ACTIVE researcher, the picker targets the
    researcher -- the Watch surface streams any spawned agent, not only wave
    executors -- and the target carries the researcher role + its session scope
    (the key the tail filters on), not a wave id.
    """
    state = _state(
        sessions={
            "S-r": _session("S-r", scope_id="CAMP-1-research-ab", role=AgentSessionRole.RESEARCHER),
        }
    )
    target = pick_watch_target(state)
    assert target is not None
    assert target.agent_role is AgentSessionRole.RESEARCHER
    assert target.wave_id == "CAMP-1-research-ab"


def test_pick_watch_target_prefers_active_researcher_over_closed_executor() -> None:
    """An ACTIVE researcher outranks a CLOSED executor for the default target."""
    state = _state(
        sessions={
            "S-exec": _session("S-exec", status=AgentSessionStatus.CLOSED, started_at=_T0),
            "S-r": _session(
                "S-r",
                scope_id="CAMP-1-research-ab",
                role=AgentSessionRole.RESEARCHER,
                started_at=_T0 + timedelta(hours=1),
            ),
        }
    )
    target = pick_watch_target(state)
    assert target is not None
    assert target.session_id == "S-r"
    assert target.agent_role is AgentSessionRole.RESEARCHER


def test_pick_watch_target_ignores_non_watchable_role() -> None:
    """A REVIEWER session is not a spawned streaming child, so it is never picked."""
    state = _state(
        sessions={"S-rev": _session("S-rev", role=AgentSessionRole.REVIEWER)},
    )
    assert pick_watch_target(state) is None


def test_render_watch_header_shows_agent_role_and_short_label() -> None:
    """The header names the role + a SHORT label (W20/W22), not the raw scope.

    A researcher's header reads its domain (parsed from the scope) rather than
    the 40-char ``campaign-{hash}-research-{domain}-{uid}`` string.
    """
    header = render_watch_header(
        _target(
            wave_id="campaign-abc-research-backoff-1a2b3c",
            agent_role=AgentSessionRole.RESEARCHER,
        ),
        mode="ascii",
    )
    assert "researcher" in header
    assert "backoff" in header  # the parsed domain
    assert "campaign-abc" not in header  # the long scope is not shown


def test_watch_display_label_per_role() -> None:
    """The display label shortens each role's scope to its meaningful part (W22)."""
    # Executor: the wave id, verbatim (already short).
    assert watch_display_label(AgentSessionRole.EXECUTOR, "P01-I01-W04") == "P01-I01-W04"
    # Researcher (new format): the domain, parsed out of the scope.
    assert (
        watch_display_label(
            AgentSessionRole.RESEARCHER, "campaign-abc-research-market-structure-1a2b3c"
        )
        == "market-structure"
    )
    # Auditor: the audited wave with a compact tag.
    assert (
        watch_display_label(AgentSessionRole.AUDITOR, "P01-I01-W04::audit") == "P01-I01-W04 audit"
    )


def test_watch_display_label_legacy_researcher_scope_is_total() -> None:
    """A legacy researcher scope with no domain segment degrades, never raises."""
    # No ``-research-`` marker at all: returned verbatim.
    assert watch_display_label(AgentSessionRole.RESEARCHER, "weird-scope") == "weird-scope"


def test_picker_column_widths_size_to_widest_cell() -> None:
    """The roster column widths size to the widest cell so the table aligns (W22)."""
    rows = session_picker_rows(
        _state(
            sessions={
                "S-exec": _session("S-exec", scope_id="P01-I01-W04"),
                "S-r": _session(
                    "S-r",
                    scope_id="campaign-abc-research-delivery-semantics-1a2b3c",
                    role=AgentSessionRole.RESEARCHER,
                ),
            }
        )
    )
    widths = picker_column_widths(rows)
    # role column fits ``researcher`` (10); label column fits the longer domain.
    assert widths.role == len("researcher")
    assert widths.label == len("delivery-semantics")


def test_render_picker_row_pads_role_column_to_align() -> None:
    """Every rendered row pads the role column to one width so roles line up (W22)."""
    rows = session_picker_rows(
        _state(
            sessions={
                "S-exec": _session("S-exec", scope_id="P01-I01-W04"),
                "S-r": _session(
                    "S-r",
                    scope_id="c-abc-research-backoff-1a2b3c",
                    role=AgentSessionRole.RESEARCHER,
                ),
            }
        )
    )
    widths = picker_column_widths(rows)
    # The executor's shorter ``executor`` role is padded to the ``researcher``
    # width so the following label column starts at the same offset in both rows.
    exec_row = next(r for r in rows if r.session_id == "S-exec")
    plain = render_picker_row(exec_row, selected=True, mode="ascii", widths=widths)
    assert "executor  " in plain  # padded past its 8 chars toward researcher's 10


def test_session_picker_seeds_selection_at_initial_session() -> None:
    """The roster seeds its selection on the initial session (W22), not row 0."""
    rows = session_picker_rows(
        _state(
            sessions={
                "S-old": _session("S-old", started_at=_T0),
                "S-new": _session("S-new", started_at=_T0 + timedelta(hours=1)),
            }
        )
    )
    # Newest-first ordering puts S-new at index 0; seeding on S-old lands on 1.
    picker = SessionPicker(rows, mode="ascii", initial_session_id="S-old")
    assert picker._initial_index() == 1
    # No initial pins the newest (top) row.
    assert SessionPicker(rows, mode="ascii")._initial_index() == 0


def test_agent_watch_roster_is_the_default_at_two_active(tmp_path: Path) -> None:
    """W22: 2+ active agents default to the readable roster, not the parity grid.

    Two ACTIVE researchers no longer drop the operator into the surprise
    side-by-side grid -- the browsable roster (SessionPicker) is the default
    multi-agent surface, and the grid is a ``g`` opt-in.
    """
    state = _state(
        sessions={
            "S-1": _session(
                "S-1", scope_id="camp-1-research-prior-art-aa", role=AgentSessionRole.RESEARCHER
            ),
            "S-2": _session(
                "S-2", scope_id="camp-1-research-backoff-bb", role=AgentSessionRole.RESEARCHER
            ),
        }
    )
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(160, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            # Roster is the default; no auto-mounted parity grid.
            assert len(pane.query(WatchGrid)) == 0
            assert pane.query_one(SessionPicker)
            # `g` opts into the grid.
            await pilot.press("g")
            await settle_screen(pilot)
            assert len(pane.query(WatchGrid)) == 1

    asyncio.run(body())


def test_roster_arrows_select_rows_beyond_viewport(tmp_path: Path) -> None:
    """Screen-level arrows keep selecting overflow rows, not scrolling focus."""
    sessions = {
        f"S-{index:02d}": _session(
            f"S-{index:02d}",
            scope_id=f"P01-I01-W{index:02d}",
            started_at=_T0 + timedelta(minutes=index),
        )
        for index in range(1, 18)
    }
    state_path = _write_state(tmp_path, _state(sessions=sessions))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(80, 18)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            picker = pane.query_one(SessionPicker)
            for _ in range(14):
                await pilot.press("down")
            await settle_screen(pilot)
            assert picker.selected == 14

    asyncio.run(body())


def test_late_output_chunk_is_dom_safe_with_picker_and_stale_target(tmp_path: Path) -> None:
    """Late output cannot resolve a removed tail after zoom recomposes to picker."""
    state_path = _write_state(
        tmp_path,
        _state(sessions={"S-1": _session("S-1"), "S-2": _session("S-2")}),
    )

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            assert pane.query(SessionPicker)
            assert not pane.query("#watch-list")
            assert not pane.query(f"#{WATCH_OUTPUT_ID}")
            pane.target = _target()
            await app._on_event(_agent_output_chunk_envelope(_WAVE, ["late chunk"]))
            await settle_screen(pilot)
            assert pane.query(SessionPicker)

    asyncio.run(body())


def test_session_picker_rows_lists_all_watchable_roles() -> None:
    """The roster lists executor + researcher sessions, each carrying its role."""
    state = _state(
        sessions={
            "S-exec": _session("S-exec", started_at=_T0),
            "S-r": _session(
                "S-r",
                scope_id="CAMP-1-research-ab",
                role=AgentSessionRole.RESEARCHER,
                started_at=_T0 + timedelta(hours=1),
            ),
        }
    )
    rows = session_picker_rows(state)
    by_id = {row.session_id: row for row in rows}
    assert by_id["S-exec"].agent_role is AgentSessionRole.EXECUTOR
    assert by_id["S-r"].agent_role is AgentSessionRole.RESEARCHER


# --------------------------------------------------------------------------
# is_watched_event -- the stream filter (boundary cases)
# --------------------------------------------------------------------------


def test_is_watched_event_false_with_no_target() -> None:
    """With no watch target nothing is part of the stream."""
    assert is_watched_event(_event("EV-1"), None) is False


def test_is_watched_event_true_when_scope_matches_wave() -> None:
    """An envelope scoped to the target's wave is part of the stream."""
    assert is_watched_event(_event("EV-1", scope_id=_WAVE), _target()) is True


def test_is_watched_event_false_when_scope_differs() -> None:
    """An envelope scoped to a different wave is filtered out."""
    assert is_watched_event(_event("EV-1", scope_id="P01-I01-W02"), _target()) is False


def test_is_watched_event_false_when_scope_is_none() -> None:
    """An envelope with no scope id is not part of any session's stream."""
    assert is_watched_event(_event("EV-1", scope_id=None), _target()) is False


def test_is_watched_event_rejects_same_scope_different_runtime_session() -> None:
    """Retry rows sharing scope do not leak across runtime sessions."""
    target = replace(_target(), runtime_session_id="runtime-new")
    envelope = _event("EV-old", scope_id=_WAVE).model_copy(
        update={"payload": {"session_id": "runtime-old"}}
    )
    assert is_watched_event(envelope, target) is False


def test_is_watched_event_rejects_same_session_different_attempt() -> None:
    """Numeric attempt identity prevents same-session retry collisions."""
    target = replace(_target(), runtime_session_id="runtime-1", attempt=2)
    envelope = _event("EV-old", scope_id=_WAVE).model_copy(
        update={"payload": {"session_id": "runtime-1", "attempt": 1}}
    )
    assert is_watched_event(envelope, target) is False


# --------------------------------------------------------------------------
# load_output_chunk_lines -- the W53 event-store tail backfill
# --------------------------------------------------------------------------


def _chunk_line(
    wave_id: str,
    *,
    seq: int,
    lines: str,
    session_id: str | None = None,
    attempt: int | None = None,
) -> str:
    """One ``agent.output.chunk`` event-store JSONL row for *wave_id*."""
    import json

    payload: dict[str, object] = {
        "event_type": "agent.output.chunk",
        "wave_id": wave_id,
        "seq": seq,
        "lines": lines,
    }
    if session_id is not None:
        payload["session_id"] = session_id
    if attempt is not None:
        payload["attempt"] = attempt
    return json.dumps(
        {
            "kind": "event",
            "scope_id": wave_id,
            "payload": payload,
        }
    )


def _write_event_store(tmp_path: Path, rows: list[str]) -> Path:
    """Write *rows* as an event.jsonl store and return its path."""
    store = tmp_path / "store"
    store.mkdir(parents=True, exist_ok=True)
    path = store / "event.jsonl"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_load_output_chunk_lines_missing_store_reads_empty(tmp_path: Path) -> None:
    """A missing / unset store yields an empty list, not a crash."""
    assert load_output_chunk_lines(None, _WAVE) == []
    assert load_output_chunk_lines(tmp_path / "store" / "event.jsonl", _WAVE) == []


def test_load_output_chunk_lines_seq_orders_and_splits(tmp_path: Path) -> None:
    """Chunk rows are seq-ordered and each ``lines`` blob splits into rows."""
    path = _write_event_store(
        tmp_path,
        [
            _chunk_line(_WAVE, seq=1, lines="second\nthird"),
            _chunk_line(_WAVE, seq=0, lines="first"),
        ],
    )
    assert load_output_chunk_lines(path, _WAVE) == ["first", "second", "third"]


def test_load_output_chunk_lines_filters_wave_and_non_chunk(tmp_path: Path) -> None:
    """Only the watched wave's chunk rows are kept; other waves + a malformed line drop."""
    path = _write_event_store(
        tmp_path,
        [
            _chunk_line(_WAVE, seq=0, lines="mine"),
            _chunk_line("P01-I01-W99", seq=0, lines="other wave"),
            "{ not json",
        ],
    )
    assert load_output_chunk_lines(path, _WAVE) == ["mine"]


def test_load_output_chunk_lines_caps_to_limit(tmp_path: Path) -> None:
    """The tail keeps only the most recent *limit* lines."""
    path = _write_event_store(
        tmp_path,
        [_chunk_line(_WAVE, seq=0, lines="\n".join(str(n) for n in range(10)))],
    )
    assert load_output_chunk_lines(path, _WAVE, limit=3) == ["7", "8", "9"]


def test_output_chunk_byte_cursor_continues_after_initial_cap(tmp_path: Path) -> None:
    """A 2,000-row initial cap does not freeze later persisted output."""
    path = _write_event_store(
        tmp_path,
        [_chunk_line(_WAVE, seq=0, lines="\n".join(str(n) for n in range(2005)), session_id="r1")],
    )
    initial = load_output_chunk_batch(path, _WAVE, runtime_session_id="r1", limit=2000)
    assert len(initial.lines) == 2000
    assert initial.lines[-1] == "2004"
    path.write_text(
        path.read_text(encoding="utf-8")
        + _chunk_line(_WAVE, seq=1, lines="continued", session_id="r1")
        + "\n",
        encoding="utf-8",
    )
    appended = load_output_chunk_batch(
        path,
        _WAVE,
        runtime_session_id="r1",
        after_byte=initial.byte_cursor,
    )
    assert appended.lines == ("continued",)
    assert appended.byte_cursor > initial.byte_cursor


def test_output_chunk_byte_cursor_retains_partial_only_row(tmp_path: Path) -> None:
    """An unterminated only row remains unread until its newline arrives."""
    row = _chunk_line(_WAVE, seq=0, lines="recovered", session_id="r1")
    split = len(row.encode("utf-8")) // 2
    path = tmp_path / "event.jsonl"
    path.write_bytes(row.encode("utf-8")[:split])

    partial = load_output_chunk_batch(path, _WAVE, runtime_session_id="r1")
    assert partial.lines == ()
    assert partial.byte_cursor == 0

    with path.open("ab") as stream:
        stream.write(row.encode("utf-8")[split:] + b"\n")
    recovered = load_output_chunk_batch(
        path,
        _WAVE,
        runtime_session_id="r1",
        after_byte=partial.byte_cursor,
    )
    assert recovered.lines == ("recovered",)
    assert recovered.byte_cursor == path.stat().st_size


def test_output_chunk_byte_cursor_stops_after_complete_before_partial(tmp_path: Path) -> None:
    """A complete row advances; following partial row is recovered next poll."""
    complete_row = _chunk_line(_WAVE, seq=0, lines="complete", session_id="r1")
    partial_row = _chunk_line(_WAVE, seq=1, lines="later", session_id="r1")
    complete_bytes = complete_row.encode("utf-8") + b"\n"
    split = len(partial_row.encode("utf-8")) // 2
    path = tmp_path / "event.jsonl"
    path.write_bytes(complete_bytes + partial_row.encode("utf-8")[:split])

    first = load_output_chunk_batch(path, _WAVE, runtime_session_id="r1")
    assert first.lines == ("complete",)
    assert first.byte_cursor == len(complete_bytes)

    with path.open("ab") as stream:
        stream.write(partial_row.encode("utf-8")[split:] + b"\n")
    second = load_output_chunk_batch(
        path,
        _WAVE,
        runtime_session_id="r1",
        after_byte=first.byte_cursor,
    )
    assert second.lines == ("later",)
    assert second.byte_cursor == path.stat().st_size


def test_output_chunk_retry_identity_prevents_sequence_collision(tmp_path: Path) -> None:
    """Same-scope retries with seq=0 bind to the selected runtime session."""
    path = _write_event_store(
        tmp_path,
        [
            _chunk_line(_WAVE, seq=0, lines="old retry", session_id="r1", attempt=1),
            _chunk_line(_WAVE, seq=0, lines="new retry", session_id="r2", attempt=2),
        ],
    )
    batch = load_output_chunk_batch(
        path,
        _WAVE,
        runtime_session_id="r2",
        attempt=2,
    )
    assert batch.lines == ("new retry",)
    assert batch.legacy_scope_fallback is False


def test_output_chunk_legacy_scope_only_fallback_is_explicit(tmp_path: Path) -> None:
    """Legacy rows remain readable but expose their weak attribution."""
    path = _write_event_store(tmp_path, [_chunk_line(_WAVE, seq=0, lines="legacy")])
    batch = load_output_chunk_batch(
        path,
        _WAVE,
        runtime_session_id="r2",
        attempt=2,
        preserve_legacy_store_order=True,
    )
    assert batch.lines == ("legacy",)
    assert batch.legacy_scope_fallback is True


# --------------------------------------------------------------------------
# render_watch_header -- empty banner vs target line
# --------------------------------------------------------------------------


def test_render_watch_header_empty_shows_honest_empty_banner() -> None:
    """The empty header leads with the no-active-session banner."""
    body = render_watch_header(None)
    assert EMPTY_NOTICE in body


def test_render_watch_header_target_surfaces_wave_runtime_status() -> None:
    """A target header surfaces the wave, the runtime, and the status."""
    body = render_watch_header(_target())
    assert _WAVE in body
    assert "claude" in body
    assert "active" in body
    assert EMPTY_NOTICE not in body


# --------------------------------------------------------------------------
# Mounted pane -- registration, honest-empty, populated stream, cancel
# --------------------------------------------------------------------------


def test_agent_watch_mode_registers_on_digit_eight(tmp_path: Path) -> None:
    """Digit ``8`` switches to the Watch mode and trails the breadcrumb.

    Pins the registry wiring: the new ModeSpec row registers the mode under
    digit ``8`` (the next free digit), so the digit key switches to an
    :class:`AgentWatchModeScreen` and the header breadcrumb trails with the
    ``Watch`` segment derived from the registry title (the breadcrumb is
    ``scope > code > phase > iter > mode``, so the mode trails).
    """
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)  # -> agent_watch
            await settle_screen(pilot)
            assert isinstance(app.screen, AgentWatchModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            header_row = frame.splitlines()[0]
            assert "Watch" in header_row

    asyncio.run(body())


def test_agent_watch_pane_renders_honest_empty(tmp_path: Path) -> None:
    """A scope with no dispatched session renders the honest-empty banner.

    The load-bearing honesty assertion: a scope with no executor session must
    show "no active dispatched session" rather than implying a live stream.
    """
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)  # -> agent_watch
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            assert pane.target is None
            frame = normalize_snapshot(capture_screen_text(app))
            assert EMPTY_NOTICE in frame

    asyncio.run(body())


def test_agent_watch_pane_resolves_target_from_seeded_session(tmp_path: Path) -> None:
    """The mounted pane resolves its watch target from a seeded executor session."""
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)  # -> agent_watch
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            assert pane.target is not None
            assert pane.target.wave_id == _WAVE
            frame = normalize_snapshot(capture_screen_text(app))
            # The header names the watched wave + runtime, not the empty banner.
            assert _WAVE in frame
            assert EMPTY_NOTICE not in frame

    asyncio.run(body())


def test_agent_watch_pane_streams_session_events_filtered_to_wave(tmp_path: Path) -> None:
    """A pushed envelope for the watched wave lands in the stream, off-wave does not.

    Seeds an ACTIVE executor session, switches into the Watch mode, then
    pushes one envelope scoped to the watched wave and one scoped to a
    different wave through the App fan-out. Only the watched-wave event renders
    in the zoom (the stream is filtered to the one session), and it replaces
    the live-waiting notice.
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)  # -> agent_watch
            await settle_screen(pilot)
            # One event for the watched wave, one for a sibling wave.
            await app._on_event(
                _event("EV-watched", scope_id=_WAVE, summary="watched dispatch_cost")
            )
            await app._on_event(
                _event("EV-other", scope_id="P01-I01-W02", summary="other wave event")
            )
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            # Exactly one row -- the off-wave event was filtered out.
            assert len(pane.query(f".{WATCH_ROW_CLASS}")) == 1
            assert not pane.query(f"#{WATCH_EMPTY_ID}")
            frame = normalize_snapshot(capture_screen_text(app))
            assert "watched dispatch_cost" in frame
            assert "other wave event" not in frame

    asyncio.run(body())


def test_agent_watch_tail_syncs_new_chunks_from_store_on_poll(tmp_path: Path) -> None:
    """W58: an open watch tail picks up newly-persisted chunks on a poll sync.

    A synchronous spawn blocks the live push, so the tail relies on the
    store-sync running each poll tick. Seed one chunk, mount the zoom (the
    on-mount sync renders it), then persist a second chunk and run the sync --
    the new line appends without re-rendering the first.
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)
    store = state_path.parent / "store"
    store.mkdir(parents=True, exist_ok=True)
    event_path = store / "event.jsonl"
    event_path.write_text(
        _chunk_line(_WAVE, seq=0, lines="first agent word") + "\n", encoding="utf-8"
    )

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            assert "first agent word" in normalize_snapshot(capture_screen_text(app))
            # A new chunk persists; the poll-tick sync brings it in.
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(_chunk_line(_WAVE, seq=1, lines="second agent word") + "\n")
            pane._sync_output_from_store()
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "first agent word" in frame
            assert "second agent word" in frame
            assert frame.count("first agent word") == 1  # no double-render

    asyncio.run(body())


def test_agent_watch_poll_output_tail_picks_up_new_chunks(tmp_path: Path) -> None:
    """W19: the poll-timer handler brings new store chunks in with no state change.

    The freeze regression: the tail's live-push path is gated off once the store
    takes authority and its only other re-sync trigger (_on_app_state) fires on a
    state.json mtime change -- but agent.output.chunk events land in event.jsonl,
    which never bumps state.json, so an open zoom would otherwise freeze mid-turn.
    Seed one chunk, mount the zoom, then persist a second chunk WITHOUT touching
    state.json and run the timer handler directly -- the new line appends.
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)
    store = state_path.parent / "store"
    store.mkdir(parents=True, exist_ok=True)
    event_path = store / "event.jsonl"
    event_path.write_text(
        _chunk_line(_WAVE, seq=0, lines="first agent word") + "\n", encoding="utf-8"
    )

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            assert "first agent word" in normalize_snapshot(capture_screen_text(app))
            # A new chunk persists to event.jsonl only -- state.json is untouched,
            # so _on_app_state never fires; the poll timer's sync is the only path.
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(_chunk_line(_WAVE, seq=1, lines="second agent word") + "\n")
            pane._poll_output_tail()
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "first agent word" in frame
            assert "second agent word" in frame
            assert frame.count("first agent word") == 1  # no double-render

    asyncio.run(body())


def test_agent_watch_poll_output_tail_before_mount_is_noop() -> None:
    """W19 boundary: the poll handler is a quiet no-op on an unmounted screen."""
    pane = AgentWatchModeScreen()
    assert not pane.is_mounted
    pane._poll_output_tail()  # no raise, no side effect


def test_agent_watch_pane_seeds_filtered_from_app_buffer(tmp_path: Path) -> None:
    """A mode switch into Watch seeds the watched wave's buffered events only."""
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            # Events arrive while Home is active (no Watch pane mounted yet).
            await app._on_event(_event("EV-1", scope_id=_WAVE, summary="buffered watched"))
            await app._on_event(_event("EV-2", scope_id="P01-I01-W02", summary="buffered other"))
            await settle_screen(pilot)
            # Now switch into Watch: it seeds from the buffer, filtered to the wave.
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "buffered watched" in frame
            assert "buffered other" not in frame

    asyncio.run(body())


def test_agent_watch_grid_opt_in_routes_events_per_tile(tmp_path: Path) -> None:
    """The ``g``-opt-in parity grid tiles two ACTIVE sessions and routes per-tile.

    W22: the grid is no longer the auto-mounted default at 2+ active sessions
    (the roster is) -- pressing ``g`` opts into the side-by-side grid. With two
    ACTIVE executor sessions on two waves the grid then mounts one tile per
    session, and one pushed event per session routes to its OWN tile and not the
    other's.
    """
    state = _state(
        sessions={
            "S-1": _session("S-1", scope_id="P01-I01-W01"),
            "S-2": _session("S-2", scope_id="P01-I01-W02"),
        }
    )
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(160, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)  # -> agent_watch
            await settle_screen(pilot)
            await pilot.press("g")  # opt into the parity grid (W22)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            grid = pane.query_one(WatchGrid)
            assert len(grid.query(f".{WATCH_TILE_CLASS}")) == 2
            # One event for each session's wave, pushed through the App fan-out.
            await app._on_event(_event("EV-A", scope_id="P01-I01-W01", summary="for S-1"))
            await app._on_event(_event("EV-B", scope_id="P01-I01-W02", summary="for S-2"))
            await settle_screen(pilot)
            tile_a = grid.query_one(f"#{tile_dom_id('S-1')}", WatchTile)
            tile_b = grid.query_one(f"#{tile_dom_id('S-2')}", WatchTile)
            rows_a = [
                str(r.render())
                for r in tile_a.query(f".{WATCH_TILE_ROW_CLASS}").results()  # type: ignore[var-annotated]
            ]
            rows_b = [
                str(r.render())
                for r in tile_b.query(f".{WATCH_TILE_ROW_CLASS}").results()  # type: ignore[var-annotated]
            ]
            assert any("for S-1" in row for row in rows_a)
            assert all("for S-2" not in row for row in rows_a)
            assert any("for S-2" in row for row in rows_b)
            assert all("for S-1" not in row for row in rows_b)

    asyncio.run(body())


def test_agent_watch_session_keys_resolve_to_live_bindings() -> None:
    """Every advertised FA4 session key resolves to a live Binding (parity).

    The affordance-parity contract: each of the advertised session keys --
    ``k`` (kill), ``x`` (the kill alias), ``space`` (pause new dispatches),
    ``l`` (open the browsable session roster), ``v`` (view log), ``Esc`` (back)
    -- maps to a
    concrete :class:`~textual.binding.Binding` whose action method exists on the
    screen, so no advertised key is a dead affordance.
    """
    keys = {
        binding.key: binding.action
        for binding in AgentWatchModeScreen.BINDINGS
        if hasattr(binding, "key")
    }
    assert keys.get("k") == "cancel_session"
    # ``x`` aliases the kill verb to the SAME confirm-gated cancel action.
    assert keys.get("x") == "cancel_session"
    assert keys.get("space") == "pause_session"
    # ``l`` opens the roster; ``v`` (relocated off ``l``) views the log.
    assert keys.get("l") == "open_roster"
    assert keys.get("v") == "view_log"
    assert keys.get("escape") == "leave_zoom"
    # Each advertised key's action method exists on the screen (no dead binding).
    for action in ("cancel_session", "pause_session", "open_roster", "view_log", "leave_zoom"):
        assert callable(getattr(AgentWatchModeScreen, f"action_{action}"))


def test_agent_watch_cancel_key_opens_confirm_modal(tmp_path: Path) -> None:
    """Pressing ``k`` opens a ConfirmModal gating the destructive kill.

    The destructive lane-kill is never one keystroke: ``k`` first opens the
    shared :class:`~eawf.surfaces.tui.screens.overlays.confirm.ConfirmModal`
    naming the SIGTERM stop, so the operator confirms before any RPC fires.
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            await pilot.press("k")  # destructive -> confirm modal
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)

    asyncio.run(body())


def test_agent_watch_x_alias_opens_same_confirm_gated_kill(tmp_path: Path) -> None:
    """Pressing ``x`` opens the SAME confirm-gated kill as ``k`` (alias parity).

    ``x`` is bound as a cancel-verb alias of the ``k`` kill: it routes through
    the identical ``cancel_session`` action, so pressing it opens the shared
    :class:`~eawf.surfaces.tui.screens.overlays.confirm.ConfirmModal` naming the
    SIGTERM stop -- the destructive kill stays confirm-gated, never one
    keystroke, whichever of the two keys the operator reaches for.
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            await pilot.press("x")  # alias of k -> same confirm modal
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)

    asyncio.run(body())


def test_agent_watch_x_alias_confirmed_issues_kill_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed ``x``-cancel issues the SAME ``agent.kill`` RPC as ``k``.

    Drives the ``x`` alias through its confirm-gated kill with a reachable
    daemon stubbed by a fake client. The alias must reach the daemon with the
    identical ``agent.kill`` request (the watched wave + attempt + term signal)
    the ``k`` key fires, proving it routes through the existing kill path rather
    than a duplicate.
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)
    calls: list[tuple[str, dict[str, object]]] = []

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _recording_client(calls))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            await pilot.press("x")  # alias of k -> confirm modal
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("right")  # highlight Yes
            await pilot.press("enter")  # confirm the kill
            await settle_screen(pilot)

    asyncio.run(body())
    # The confirmed alias-cancel reached the daemon with the same kill request.
    assert calls and calls[0][0] == "agent.kill"
    assert calls[0][1]["wave_id"] == _WAVE
    assert calls[0][1]["attempt"] == 1
    assert calls[0][1]["signal"] == "term"


def test_agent_watch_cancel_dismissed_issues_no_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the kill confirm (``Esc``) issues no ``agent.kill`` RPC.

    The destructive gate must not fire the kill when the operator backs out:
    pressing ``Esc`` on the confirm modal dismisses it as ``No`` so the daemon
    is never reached.
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)
    calls: list[tuple[str, dict[str, object]]] = []

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _recording_client(calls))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            await pilot.press("k")
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("escape")  # cancel == No
            await settle_screen(pilot)

    asyncio.run(body())
    assert calls == []  # the cancelled kill never reached the daemon


def test_agent_watch_cancel_action_no_daemon_surfaces_honest_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no reachable daemon the confirmed kill reports the request was not issued.

    The cancel action must never fake a kill: when the daemon socket is
    unavailable the confirmed kill surfaces the honest "daemon unavailable" toast
    rather than a success.
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        # Force the daemon probe to report unavailable so no real RPC is made.
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            await pilot.press("k")  # -> confirm modal
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("right")  # highlight Yes
            await pilot.press("enter")  # confirm
            await settle_screen(pilot)
            toasts = "\n".join(toast_messages(app))
            assert CANCEL_NO_DAEMON in toasts

    asyncio.run(body())


def test_agent_watch_cancel_action_surfaces_placeholder_kill_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The confirmed cancel issues an ``agent.kill`` request and surfaces its result.

    Drives the confirmed cancel path with a reachable daemon stubbed by a fake
    client that returns the placeholder ``killed=false`` result the daemon
    currently sends. The action must surface that the request was issued and the
    daemon's honest verdict (not killed) rather than faking success.
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)
    calls: list[tuple[str, dict[str, object]]] = []

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _recording_client(calls))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            await pilot.press("k")  # -> confirm modal
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("right")  # highlight Yes
            await pilot.press("enter")  # confirm
            await settle_screen(pilot)
            toasts = "\n".join(toast_messages(app))
            assert "not killed" in toasts
            assert CANCEL_IDLE not in toasts

    asyncio.run(body())
    # The confirmed cancel reached the daemon with the watched wave + attempt + term.
    assert calls and calls[0][0] == "agent.kill"
    assert calls[0][1]["wave_id"] == _WAVE
    assert calls[0][1]["attempt"] == 1
    assert calls[0][1]["signal"] == "term"


# --------------------------------------------------------------------------
# Raw-output tail (the FA4 zoom add) mounted in the session zoom
# --------------------------------------------------------------------------


def test_agent_watch_zoom_mounts_output_tail_with_waiting_notice(tmp_path: Path) -> None:
    """The single-session zoom mounts a raw-output tail showing the waiting notice.

    The zoom adds the raw-output tail beneath the typed lifecycle stream; before
    any agent stdout arrives it shows the pinned ``waiting for output...`` notice
    rather than a frozen blank pane.
    """
    state_path = _write_state(tmp_path, _state(sessions={"S-1": _session("S-1")}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            tail = pane.query_one(f"#{WATCH_OUTPUT_ID}", OutputTail)
            assert tail.query(f"#{OUTPUT_TAIL_WAITING_ID}")
            assert not tail.has_output
            frame = normalize_snapshot(capture_screen_text(app))
            assert WAITING_NOTICE in frame

    asyncio.run(body())


def test_agent_watch_append_output_streams_watched_wave_only(tmp_path: Path) -> None:
    """A raw-output line for the watched wave lands in the tail; off-wave does not.

    The raw-output fan-out is filtered to the watched session's wave: a line for
    the watched wave appends to the tail (replacing the waiting notice), while a
    line for a sibling wave is dropped (the tail shows one session's stdout).
    """
    state_path = _write_state(tmp_path, _state(sessions={"S-1": _session("S-1")}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            pane.append_output(_WAVE, "agent says hello")
            pane.append_output("P01-I01-W02", "other lane output")
            await settle_screen(pilot)
            tail = pane.query_one(f"#{WATCH_OUTPUT_ID}", OutputTail)
            rows = [str(r.render()) for r in tail.query(f".{OUTPUT_TAIL_ROW_CLASS}").results()]
            assert any("agent says hello" in row for row in rows)
            assert all("other lane output" not in row for row in rows)
            assert tail.has_output
            assert not tail.query(f"#{OUTPUT_TAIL_WAITING_ID}")  # notice dropped

    asyncio.run(body())


def test_agent_watch_output_navigation_keys_and_mouse_wheel(tmp_path: Path) -> None:
    """Paging, bounds, wheel, and live-tail resume move one mounted output pane."""
    from textual.events import MouseScrollDown, MouseScrollUp

    state_path = _write_state(tmp_path, _state(sessions={"S-1": _session("S-1")}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(80, 18)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            tail = pane.query_one(f"#{WATCH_OUTPUT_ID}", OutputTail)
            for index in range(80):
                pane.append_output(_WAVE, f"line {index}")
            await settle_screen(pilot)
            assert tail.max_scroll_y > 0
            assert tail.scroll_y == tail.max_scroll_y

            await pilot.press("home")
            assert tail.scroll_y == 0
            await pilot.press("end")
            assert tail.scroll_y == tail.max_scroll_y
            await pilot.press("pageup")
            page_up_y = tail.scroll_y
            assert page_up_y < tail.max_scroll_y
            await pilot.press("pagedown")
            assert tail.scroll_y > page_up_y

            tail.post_message(
                MouseScrollUp(
                    tail,
                    x=0,
                    y=0,
                    delta_x=0,
                    delta_y=-1,
                    button=0,
                    shift=False,
                    meta=False,
                    ctrl=False,
                )
            )
            await settle_screen(pilot)
            wheel_up_y = tail.scroll_y
            assert wheel_up_y < tail.max_scroll_y
            tail.post_message(
                MouseScrollDown(
                    tail,
                    x=0,
                    y=0,
                    delta_x=0,
                    delta_y=1,
                    button=0,
                    shift=False,
                    meta=False,
                    ctrl=False,
                )
            )
            await settle_screen(pilot)
            assert tail.scroll_y > wheel_up_y

            pane.append_output(_WAVE, "live tail resumes")
            await settle_screen(pilot)
            assert tail.scroll_y == tail.max_scroll_y

    asyncio.run(body())


# --------------------------------------------------------------------------
# Pause this lane (space) + view log (l)
# --------------------------------------------------------------------------


def _state_with_logged_wave(*, handle: str = "urn:eawf:v1:session-log:claude:abc") -> State:
    """Build a state whose watched wave carries one session-attempt + log handle.

    The ``l`` view-log path resolves the handle off the wave's session table, so
    this builder seeds a wave row carrying one ``SessionAttempt`` with
    *handle* alongside the ACTIVE executor session the picker selects.
    """
    from eawf.kernel.state.enums import WaveStatus
    from eawf.kernel.state.models import SessionAttempt, Wave

    wave = Wave(
        id=_WAVE,
        iter_id="P01-I01",
        title="seeded watched wave",
        status=WaveStatus.IN_PROGRESS,
        opened_at=_T0,
        sessions={
            1: SessionAttempt(
                attempt=1,
                runtime="claude",
                session_id="S-1",
                session_log_handle=handle,
                started_at=_T0,
            )
        },
    )
    state = _state(sessions={"S-1": _session("S-1")})
    state.waves[_WAVE] = wave
    return state


def test_agent_watch_pause_no_target_surfaces_honest_line(tmp_path: Path) -> None:
    """``space`` on an honest-empty scope (no session) says there is no session in view.

    With no dispatched session the pause key has no lane to act from, so it
    surfaces the honest "no session in view" toast without reaching the daemon.
    """
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            assert pane.target is None
            await pilot.press("space")  # pause this lane (none)
            await settle_screen(pilot)
            toasts = "\n".join(toast_messages(app))
            assert PAUSE_NO_TARGET in toasts

    asyncio.run(body())


def test_agent_watch_pause_no_daemon_surfaces_honest_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no reachable daemon ``space`` reports the pause was not issued.

    Pause is non-destructive (no confirm gate), but an unreachable daemon must
    still surface the honest unavailable toast rather than faking a toggle.
    """
    state_path = _write_state(tmp_path, _state(sessions={"S-1": _session("S-1")}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            await pilot.press("space")  # pause this lane
            await settle_screen(pilot)
            toasts = "\n".join(toast_messages(app))
            assert PAUSE_NO_DAEMON in toasts

    asyncio.run(body())


def test_agent_watch_pause_issues_pause_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``space`` issues ``agent.pause`` and surfaces the persisted verdict.

    With a reachable daemon and a not-paused state the pause toggle fires
    ``agent.pause`` (no confirm) and reports the persisted ``paused`` verdict.
    """
    state_path = _write_state(tmp_path, _state(sessions={"S-1": _session("S-1")}))
    calls: list[tuple[str, dict[str, object]]] = []

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _recording_client(calls, response={"paused": True}))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            await pilot.press("space")  # pause this lane
            await settle_screen(pilot)
            toasts = "\n".join(toast_messages(app))
            assert "paused" in toasts

    asyncio.run(body())
    # The pause toggle reached the daemon with agent.pause (not-paused -> pause).
    assert calls and calls[0][0] == "agent.pause"


def test_agent_watch_view_log_no_handle_surfaces_honest_line(tmp_path: Path) -> None:
    """``v`` on a wave with no recorded session-log handle says so honestly.

    The seeded scope has an ACTIVE executor session but no wave session table,
    so no log handle is recorded; the view-log key surfaces the honest
    "no session log recorded yet" toast rather than pointing at a missing log.
    """
    state_path = _write_state(tmp_path, _state(sessions={"S-1": _session("S-1")}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            await pilot.press("v")  # view log
            await settle_screen(pilot)
            toasts = "\n".join(toast_messages(app))
            assert LOG_NO_HANDLE in toasts

    asyncio.run(body())


def test_agent_watch_view_log_surfaces_recorded_handle(tmp_path: Path) -> None:
    """``v`` surfaces the watched attempt's recorded session-log handle.

    A wave carrying a session-attempt row with a recorded log handle resolves
    onto the watch target, so the view-log key surfaces that handle.
    """
    handle = "urn:eawf:v1:session-log:claude:deadbeef"
    state_path = _write_state(tmp_path, _state_with_logged_wave(handle=handle))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            assert pane.target is not None
            assert pane.target.log_handle == handle
            await pilot.press("v")  # view log
            await settle_screen(pilot)
            toasts = "\n".join(toast_messages(app))
            assert handle in toasts

    asyncio.run(body())


def test_agent_watch_pane_keeps_chassis_brand(tmp_path: Path) -> None:
    """Even honest-empty, the Watch pane keeps the shared chassis brand row."""
    from eawf.surfaces.tui.widgets.header import BRAND

    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)  # -> agent_watch
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            header_row = frame.splitlines()[0]
            assert BRAND in header_row
            assert "Watch" in header_row

    asyncio.run(body())


# --------------------------------------------------------------------------
# W08 -- the FA4 live-output producer feeds the App buffer + the agent-watch tail
# --------------------------------------------------------------------------


def _agent_output_envelope(wave_id: str, lines: list[str]) -> Envelope:
    """Build an ``agent.output`` envelope the way the dispatch-runner producer does."""
    from datetime import UTC, datetime

    from eawf.kernel.state.enums import StoreKind
    from eawf.runtime.daemon.dispatch_runner import AGENT_OUTPUT_EVENT_TYPE

    now = datetime.now(UTC)
    return Envelope(
        schema_version="1.0",
        id="EV-agentoutput1",
        kind=StoreKind.EVENT,
        scope_id=wave_id,
        created_at=now,
        updated_at=None,
        summary=f"agent_output wave={wave_id}",
        payload={
            "timestamp": now.isoformat(),
            "event_type": AGENT_OUTPUT_EVENT_TYPE,
            "actor": "daemon",
            "command": "dispatch_runner.emit_agent_output",
            "args_hash": "",
            "status": "ok",
            "message": f"agent_output wave={wave_id}",
            "extras": {"wave_id": wave_id, "lines": "\n".join(lines), "line_count": len(lines)},
        },
        blob_refs=[],
        artifact_ids=[],
    )


def test_app_routes_agent_output_event_to_buffer_and_tail(tmp_path: Path) -> None:
    """W08: an agent.output event feeds the App ring buffer + the agent-watch tail.

    The dispatch-runner producer publishes spawned-child stdout as an
    ``agent.output`` event; the App's _on_event routes it to the raw-output
    buffer (the live tail seed) AND fans each line to the agent-watch zoom's
    tail, so the operator reads the agent's OWN words live -- the producer is no
    longer idle.
    """
    state_path = _write_state(tmp_path, _state(sessions={"S-1": _session("S-1")}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            # Deliver an agent.output event for the watched wave through the App
            # event seam (the real producer->consumer bridge).
            await app._on_event(_agent_output_envelope(_WAVE, ["building...", "done."]))
            await settle_screen(pilot)
            # The App ring buffer recorded the rows (the tail-seed source).
            assert (_WAVE, "building...") in app.live_output_buffer
            assert (_WAVE, "done.") in app.live_output_buffer
            # The agent-watch tail rendered the lines (the consumer fan-out).
            tail = pane.query_one(f"#{WATCH_OUTPUT_ID}", OutputTail)
            rows = [str(r.render()) for r in tail.query(f".{OUTPUT_TAIL_ROW_CLASS}").results()]
            assert any("building..." in row for row in rows)
            assert any("done." in row for row in rows)
            assert tail.has_output

    asyncio.run(body())


def _agent_output_chunk_envelope(wave_id: str, lines: list[str], *, seq: int = 0) -> Envelope:
    """Build an ``agent.output.chunk`` envelope the way the live producer does.

    The typed :class:`~eawf.kernel.store.kinds.events.AgentOutputChunkPayload`
    carries ``wave_id`` + ``lines`` at the TOP level (not under ``extras``), so
    this mirrors that shape -- the W45 live-streaming counterpart of the terminal
    ``agent.output`` event.
    """
    from datetime import UTC, datetime

    from eawf.kernel.state.enums import StoreKind
    from eawf.runtime.daemon.dispatch_runner import AGENT_OUTPUT_CHUNK_EVENT_TYPE

    now = datetime.now(UTC)
    return Envelope(
        schema_version="1.0",
        id=f"EV-chunk{seq}",
        kind=StoreKind.EVENT,
        scope_id=wave_id,
        created_at=now,
        updated_at=None,
        summary=f"agent_output_chunk wave={wave_id} seq={seq}",
        payload={
            "event_type": AGENT_OUTPUT_CHUNK_EVENT_TYPE,
            "timestamp": now.isoformat(),
            "wave_id": wave_id,
            "session_id": "sess-1",
            "seq": seq,
            "lines": "\n".join(lines),
            "trace_request_id": None,
            "trace_wave_id": wave_id,
            "trace_attempt_id": None,
        },
        blob_refs=[],
        artifact_ids=[],
    )


def test_app_routes_agent_output_chunk_event_to_buffer_and_tail(tmp_path: Path) -> None:
    """W45: a live agent.output.chunk event feeds the App buffer + the watch tail.

    The live-spawn path persists a streaming batch of stdout as a typed
    ``agent.output.chunk`` event AS the spawn runs; the App's _on_event routes it
    through the SAME raw-output seam as the terminal ``agent.output`` event, so
    the watch tail renders the agent's words live (and the persisted chunk seeds a
    later-mounted tail from the ring).
    """
    state_path = _write_state(tmp_path, _state(sessions={"S-1": _session("S-1")}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            await app._on_event(
                _agent_output_chunk_envelope(_WAVE, ["chunk line a", "chunk line b"])
            )
            await settle_screen(pilot)
            # The App ring buffer recorded the chunk's lines (the tail-seed source).
            assert (_WAVE, "chunk line a") in app.live_output_buffer
            assert (_WAVE, "chunk line b") in app.live_output_buffer
            # The agent-watch tail rendered them live.
            tail = pane.query_one(f"#{WATCH_OUTPUT_ID}", OutputTail)
            rows = [str(r.render()) for r in tail.query(f".{OUTPUT_TAIL_ROW_CLASS}").results()]
            assert any("chunk line a" in row for row in rows)
            assert any("chunk line b" in row for row in rows)
            assert tail.has_output

    asyncio.run(body())


def test_app_does_not_route_chunk_event_for_other_wave_to_tail(tmp_path: Path) -> None:
    """W45 boundary: a chunk for a DIFFERENT wave is not rendered by the watched pane.

    The watch tail shows one session's stdout. A chunk event keyed on another
    wave's scope_id still rides the App ring (so a later-zoomed pane on that wave
    seeds it), but it must NOT append to the currently-watched lane's tail.
    """
    state_path = _write_state(tmp_path, _state(sessions={"S-1": _session("S-1")}))
    other_wave = "P01-I01-W02"

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            await app._on_event(_agent_output_chunk_envelope(other_wave, ["foreign output"]))
            await settle_screen(pilot)
            # The ring recorded the other-wave row, but the watched tail did not.
            assert (other_wave, "foreign output") in app.live_output_buffer
            tail = pane.query_one(f"#{WATCH_OUTPUT_ID}", OutputTail)
            rows = [str(r.render()) for r in tail.query(f".{OUTPUT_TAIL_ROW_CLASS}").results()]
            assert not any("foreign output" in row for row in rows)

    asyncio.run(body())


def test_app_output_buffer_is_bounded(tmp_path: Path) -> None:
    """W08: the raw-output ring buffer is bounded (the oldest lines scroll off).

    A producer chattier than the ring cap never grows the buffer unbounded: the
    buffer holds at most LIVE_OUTPUT_BUFFER_MAX rows, and the FRESHEST lines win
    (the oldest are evicted left).
    """
    from eawf.surfaces.tui.app import LIVE_OUTPUT_BUFFER_MAX

    state_path = _write_state(tmp_path, _state(sessions={"S-1": _session("S-1")}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            # Feed far more lines than the ring cap straight through append_output.
            for n in range(LIVE_OUTPUT_BUFFER_MAX + 100):
                app.append_output(_WAVE, f"line {n}")
            buffer = app.live_output_buffer
            assert len(buffer) == LIVE_OUTPUT_BUFFER_MAX
            # The freshest line survived; the oldest scrolled off.
            assert buffer[-1] == (_WAVE, f"line {LIVE_OUTPUT_BUFFER_MAX + 99}")
            assert (_WAVE, "line 0") not in buffer

    asyncio.run(body())


# --------------------------------------------------------------------------
# Session picker + status-gated cancel / pause (P30-I22-W07)
# --------------------------------------------------------------------------


def _closed_sessions() -> dict[str, AgentSession]:
    """Two finished executor sessions on different waves; ``S-2`` is newest."""
    return {
        "S-1": _session(
            "S-1",
            scope_id="P01-I01-W01",
            status=AgentSessionStatus.CLOSED,
            started_at=_T0,
        ),
        "S-2": _session(
            "S-2",
            scope_id="P01-I01-W02",
            status=AgentSessionStatus.CLOSED,
            started_at=_T0 + timedelta(minutes=5),
        ),
    }


def test_session_picker_rows_newest_first_any_status() -> None:
    """Picker rows cover every executor session, newest ``started_at`` first."""
    sessions = _closed_sessions()
    sessions["S-3"] = _session(
        "S-3",
        scope_id="P01-I01-W03",
        status=AgentSessionStatus.FAILED,
        started_at=_T0 + timedelta(minutes=2),
    )
    rows = session_picker_rows(_state(sessions=sessions))
    assert [row.session_id for row in rows] == ["S-2", "S-3", "S-1"]
    assert rows[0].wave_id == "P01-I01-W02"
    assert rows[1].status is AgentSessionStatus.FAILED
    assert rows[0].started_label == "12:05"


def test_session_picker_rows_empty_state_and_no_executors() -> None:
    """No state / no executor sessions yield the honest empty tuple."""
    assert session_picker_rows(None) == ()
    assert session_picker_rows(_state()) == ()
    observer = _session("S-9", role=AgentSessionRole.OPERATOR)
    assert session_picker_rows(_state(sessions={"S-9": observer})) == ()


def test_watch_picker_lists_finished_sessions_with_select_hints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With only finished sessions the mode mounts the browsable picker.

    Nothing is live (no lane, no ACTIVE session) but two finished sessions
    exist, so the body is the session picker -- newest first -- and the footer
    advertises the selection cursor.
    """
    from eawf.surfaces.tui.widgets.footer import Footer

    state_path = _write_state(tmp_path, _state(sessions=_closed_sessions()))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            picker = pane.query(SessionPicker)
            assert picker, "expected the session picker body case"
            rows = pane.query(f".{SESSION_PICKER_ROW_CLASS}").results()
            texts = [str(row.render()) for row in rows]
            assert len(texts) == 2
            assert "P01-I01-W02" in texts[0]  # newest first
            assert "P01-I01-W01" in texts[1]
            hints = " ".join(pane.query_one(Footer).hints)
            assert "select" in hints and "open" in hints

    asyncio.run(body())


def test_watch_picker_enter_zooms_and_esc_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enter zooms the highlighted session; Esc steps back out to the picker."""
    from eawf.surfaces.tui.widgets.footer import Footer

    state_path = _write_state(tmp_path, _state(sessions=_closed_sessions()))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            await pilot.press("enter")  # zoom the newest (selected) session
            await settle_screen(pilot)
            header = pane.query(f"#{WATCH_HEADER_ID}")
            assert header, "expected the FA4 zoom after Enter"
            assert "P01-I01-W02" in str(header.first().render())
            zoom_hints = " ".join(pane.query_one(Footer).hints)
            assert "select" not in zoom_hints  # arrows scroll here, not select
            await pilot.press("escape")  # back out to the picker
            await settle_screen(pilot)
            assert pane.query(SessionPicker), "Esc should return to the picker"

    asyncio.run(body())


def _active_plus_closed() -> dict[str, AgentSession]:
    """One ACTIVE executor session (``S-1`` / W01) plus a newer finished one.

    The ACTIVE session auto-targets the single-session zoom; the newer CLOSED
    ``S-2`` (W02) gives the roster a DIFFERENT agent to step to.
    """
    return {
        "S-1": _session(
            "S-1",
            scope_id="P01-I01-W01",
            status=AgentSessionStatus.ACTIVE,
            started_at=_T0,
        ),
        "S-2": _session(
            "S-2",
            scope_id="P01-I01-W02",
            status=AgentSessionStatus.CLOSED,
            started_at=_T0 + timedelta(minutes=5),
        ),
    }


def test_watch_roster_key_opens_picker_over_active_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``l`` mounts the session roster even while an executor session is ACTIVE.

    A single ACTIVE executor session auto-targets the FA4 zoom, which trapped
    the operator on that one agent (no live picker). The dedicated roster key
    mounts the browsable :class:`SessionPicker` (scroll id
    ``watch-session-picker``) OVER the zoom -- decoupled from the
    no-active-sessions auto-mount guard -- so every executor session (the ACTIVE
    W01 and the finished W02) is a selectable, visible row.
    """
    state_path = _write_state(tmp_path, _state(sessions=_active_plus_closed()))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            # The lone ACTIVE session auto-targets the FA4 zoom -- no picker yet.
            assert not pane.query(SessionPicker)
            assert pane.query(f"#{WATCH_HEADER_ID}"), "expected the FA4 zoom pre-roster"
            await pilot.press("l")  # open the browsable roster
            await settle_screen(pilot)
            picker = pane.query(SessionPicker)
            assert picker, "the roster key must mount the picker over an ACTIVE session"
            assert pane.query(f"#{SESSION_PICKER_ID}"), "picker scroll id must be present"
            rows = pane.query(f".{SESSION_PICKER_ROW_CLASS}").results()
            texts = [str(row.render()) for row in rows]
            assert len(texts) == 2
            assert "P01-I01-W02" in texts[0]  # newest first
            assert "P01-I01-W01" in texts[1]

    asyncio.run(body())


def test_watch_roster_pick_zooms_a_different_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting a roster row watches THAT agent, escaping the trapped zoom.

    The ACTIVE session auto-targets W01; opening the roster (``l``) and pressing
    Enter on the preselected newest row (W02) re-targets + zooms that DIFFERENT
    agent into the FA4 single-session view -- the browse-to-another-agent path
    the defect blocked. Confirms :meth:`on_session_picker_pick` wiring.
    """
    state_path = _write_state(tmp_path, _state(sessions=_active_plus_closed()))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            # Auto-targeted onto the ACTIVE W01 agent before any roster step.
            assert pane.target is not None
            assert pane.target.wave_id == "P01-I01-W01"
            await pilot.press("l")  # open the roster
            await settle_screen(pilot)
            assert pane.query(SessionPicker), "expected the roster body case"
            await pilot.press("enter")  # zoom the preselected newest row (W02)
            await settle_screen(pilot)
            header = pane.query(f"#{WATCH_HEADER_ID}")
            assert header, "expected the FA4 zoom after selecting a roster row"
            assert "P01-I01-W02" in str(header.first().render())
            assert pane.target is not None
            assert pane.target.wave_id == "P01-I01-W02"

    asyncio.run(body())


def test_watch_cancel_on_closed_session_is_inert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel on a finished session never opens the kill confirm.

    A single CLOSED session auto-zooms; ``x`` must surface the honest
    "already closed" toast without a ConfirmModal, and the idle line reads the
    replay notice instead of advertising a cancel that cannot happen.
    """
    closed = _session("S-1", status=AgentSessionStatus.CLOSED)
    state_path = _write_state(tmp_path, _state(sessions={"S-1": closed}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            result = pane.query(f"#{WATCH_RESULT_ID}")
            idle = str(result.first().render())
            assert WATCH_REPLAY_TEMPLATE.format(status="closed") in idle
            assert CANCEL_IDLE not in idle
            await pilot.press("x")
            await settle_screen(pilot)
            assert isinstance(app.screen, AgentWatchModeScreen)  # no confirm modal
            toasts = "\n".join(toast_messages(app))
            assert CANCEL_NOT_ACTIVE_TEMPLATE.format(status="closed") in toasts

    asyncio.run(body())


def test_watch_pause_on_closed_session_still_pauses_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``space`` on a finished session still pauses NEW dispatches repo-wide.

    ``agent.pause`` is targetless -- it toggles the repo-wide ``dispatch_paused``
    next-claim flag, not the watched lane's child -- so a terminal watch target
    no longer gates it. Watching a CLOSED session and pressing ``space`` fires
    ``agent.pause`` and surfaces the honest "pause new dispatches: paused" toast
    rather than the old per-session "nothing to pause" line.
    """
    closed = _session("S-1", status=AgentSessionStatus.CLOSED)
    state_path = _write_state(tmp_path, _state(sessions={"S-1": closed}))
    calls: list[tuple[str, dict[str, object]]] = []

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _recording_client(calls, response={"paused": True}))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            await pilot.press("space")
            await settle_screen(pilot)
            toasts = "\n".join(toast_messages(app))
            assert "pause new dispatches: paused" in toasts

    asyncio.run(body())
    # The repo-wide pause fired despite the watched session being terminal.
    assert calls and calls[0][0] == "agent.pause"
