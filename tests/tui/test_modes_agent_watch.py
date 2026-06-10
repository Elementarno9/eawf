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
    EMPTY_NOTICE,
    WATCH_EMPTY_ID,
    WATCH_RESULT_ID,
    WATCH_ROW_CLASS,
    WATCH_TILE_CLASS,
    WATCH_TILE_ROW_CLASS,
    AgentWatchModeScreen,
    WatchGrid,
    WatchTarget,
    WatchTile,
    is_watched_event,
    pick_watch_target,
    render_watch_header,
    tile_dom_id,
)
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
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
) -> WatchTarget:
    """Build a directly-constructed watch target for the render helpers."""
    return WatchTarget(
        session_id=session_id,
        wave_id=wave_id,
        runtime=runtime,
        status=status,
        attempt=attempt,
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


def test_agent_watch_mode_mounts_grid_for_two_active_executors(tmp_path: Path) -> None:
    """Two ACTIVE executor sessions switch the mode body to the parallel grid.

    With two ACTIVE executor sessions on two waves, the agent-watch mode mounts
    the :class:`WatchGrid` (one tile per session) in place of the single-session
    zoom; one pushed event per session routes to its OWN tile and not the
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


def test_agent_watch_cancel_binding_exists() -> None:
    """The Watch pane binds ``k`` to the cancel action."""
    keys = {
        binding.key: binding.action
        for binding in AgentWatchModeScreen.BINDINGS
        if hasattr(binding, "key")
    }
    assert keys.get("k") == "cancel_session"


def test_agent_watch_cancel_action_no_daemon_surfaces_honest_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no reachable daemon the cancel action reports the request was not issued.

    The cancel action must never fake a kill: when the daemon socket is
    unavailable it surfaces the honest "daemon unavailable" line rather than a
    success.
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
            await pilot.press("k")  # cancel
            await settle_screen(pilot)
            result = pane.query_one(f"#{WATCH_RESULT_ID}")
            assert CANCEL_NO_DAEMON in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_agent_watch_cancel_action_surfaces_placeholder_kill_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancel action issues an ``agent.kill`` request and surfaces its result.

    Drives the cancel path with a reachable daemon stubbed by a fake client
    that returns the placeholder ``killed=false`` result the daemon currently
    sends. The action must surface that the request was issued and the daemon's
    honest verdict (not killed) rather than faking success.
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            # Mirror the daemon's placeholder agent.kill response.
            return {"killed": False, "signal": "term"}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", lambda *a, **k: _FakeClient())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            await pilot.press("k")  # cancel
            await settle_screen(pilot)
            result = pane.query_one(f"#{WATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "not killed" in rendered
            assert CANCEL_IDLE not in rendered

    asyncio.run(body())
    # The cancel action reached the daemon with the watched wave + attempt.
    assert calls and calls[0][0] == "agent.kill"
    assert calls[0][1]["wave_id"] == _WAVE
    assert calls[0][1]["attempt"] == 1


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
