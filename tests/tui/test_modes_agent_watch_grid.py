"""Tests for the parallel multi-session watch grid pane.

The Watch mode's grid surface lays out one tile per ACTIVE executor
:class:`~eawf.kernel.state.models.AgentSession`; a pushed event for a session
routes to that session's tile ONLY and not a sibling's. A scope with zero
dispatched sessions renders the honest-empty
:data:`~eawf.surfaces.tui.modes.agent_watch.EMPTY_NOTICE`; a daemon-unreachable
App renders :data:`~eawf.surfaces.tui.modes.agent_watch.WATCH_DEGRADED`.

These tests pin the two halves:

* the pure helpers --
  :func:`~eawf.surfaces.tui.modes.agent_watch.active_watchable_sessions` (tile
  enumeration), :func:`~eawf.surfaces.tui.modes.agent_watch.tile_dom_id`
  (per-session DOM id), and
  :func:`~eawf.surfaces.tui.modes.agent_watch.session_routes_event` (the
  per-tile routing predicate) -- tested against directly-built rows so the
  routing logic is verified without mounting Textual; and
* the mounted :class:`~eawf.surfaces.tui.modes.agent_watch.WatchGrid` under a
  Pilot on a bare themed host: two ACTIVE executor sessions get two tiles, one
  pushed event per session lands in its OWN tile (not the other's); zero
  dispatched sessions renders the honest-empty notice; and a degraded App
  renders the daemon-unreachable notice.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widgets import Static

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
from eawf.surfaces.tui.modes.agent_watch import (
    EMPTY_NOTICE,
    WATCH_DEGRADED,
    WATCH_GRID_EMPTY_ID,
    WATCH_TILE_CLASS,
    WATCH_TILE_ROW_CLASS,
    WatchGrid,
    WatchTile,
    active_watchable_sessions,
    session_routes_event,
    tile_dom_id,
)
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: A wide terminal so two side-by-side tiles lay out unwrapped.
_SIZE = (160, 40)

#: The two waves the two seeded executor sessions scope to (their ``scope_id``),
#: which the dispatch events also stamp as their own ``scope_id``.
_WAVE_A = "P01-I01-W01"
_WAVE_B = "P01-I01-W02"

assert _THEME.is_file(), f"missing theme: {_THEME}"


class _HostApp(App[None]):
    """Bare themed host carrying the read-only surface the grid reads.

    The grid reads ``render_mode`` (the tile sigil column) off ``self.app``.
    The ``degraded`` flag drives the empty notice's honest-degraded wording.
    No daemon socket is exposed, so the grid never reaches off-disk.
    """

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")
    degraded: reactive[bool] = reactive(False)

    def __init__(self, *, grid: WatchGrid, degraded: bool = False) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self.degraded = degraded
        self._grid = grid

    def compose(self) -> ComposeResult:
        yield self._grid


def _session(
    sid: str,
    *,
    scope_id: str,
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
    role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    runtime: str = "claude",
) -> AgentSession:
    """Build an executor agent-session row for the grid enumerator."""
    return AgentSession(
        id=sid,
        role=role,
        runtime=runtime,
        scope_id=scope_id,
        status=status,
        started_at=_T0,
    )


def _event(event_id: str, *, scope_id: str | None, summary: str = "live event") -> Envelope:
    """Build a minimal event-kind envelope keyed to *scope_id*."""
    return Envelope(
        id=event_id,
        kind="event",  # type: ignore[arg-type]
        scope_id=scope_id,
        created_at=datetime(2026, 5, 27, 9, 30, 15, tzinfo=UTC),
        updated_at=None,
        summary=summary,
        payload={"event_type": "test", "status": "ok"},
    )


def _state(sessions: dict[str, AgentSession]) -> State:
    """Build a minimal repo state carrying *sessions*."""
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
            "agent_sessions": {sid: s.model_dump(mode="json") for sid, s in sessions.items()},
            "plugins": {},
            "indexes": {},
        }
    )


# --------------------------------------------------------------------------
# active_watchable_sessions -- tile enumeration (boundary cases)
# --------------------------------------------------------------------------


def test_active_watchable_sessions_none_state_is_empty() -> None:
    """An unbound state yields no tiles (honest-empty grid path)."""
    assert active_watchable_sessions(None) == []


def test_active_watchable_sessions_includes_all_watchable_roles() -> None:
    """ACTIVE executor + researcher sessions tile; non-watchable + closed drop.

    A researcher is now watchable alongside the executor -- the grid tiles any
    spawned agent role, not only wave executors -- while a REVIEWER (not a
    spawned, output-streaming role) and a CLOSED session are filtered out.
    """
    state = _state(
        {
            "S-active": _session("S-active", scope_id=_WAVE_A),
            "S-research": _session(
                "S-research", scope_id="CAMP-1-research-ab", role=AgentSessionRole.RESEARCHER
            ),
            "S-closed": _session("S-closed", scope_id=_WAVE_B, status=AgentSessionStatus.CLOSED),
            "S-reviewer": _session("S-reviewer", scope_id=_WAVE_B, role=AgentSessionRole.REVIEWER),
        }
    )
    picked = active_watchable_sessions(state)
    assert [s.id for s in picked] == ["S-active", "S-research"]


def test_active_watchable_sessions_is_id_sorted() -> None:
    """Two ACTIVE watchable sessions are returned id-sorted for a stable layout."""
    state = _state(
        {
            "S-2": _session("S-2", scope_id=_WAVE_B),
            "S-1": _session("S-1", scope_id=_WAVE_A),
        }
    )
    assert [s.id for s in active_watchable_sessions(state)] == ["S-1", "S-2"]


# --------------------------------------------------------------------------
# tile_dom_id + session_routes_event -- per-tile routing (boundary cases)
# --------------------------------------------------------------------------


def test_tile_dom_id_namespaces_the_session_id() -> None:
    """The tile DOM id namespaces the session id so two tiles never collide."""
    assert tile_dom_id("S-1") == "watch-tile--S-1"
    assert tile_dom_id("S-1") != tile_dom_id("S-2")


def test_session_routes_event_true_when_scope_matches() -> None:
    """An envelope scoped to the session's wave routes to its tile."""
    sess = _session("S-1", scope_id=_WAVE_A)
    assert session_routes_event(sess, _event("EV-1", scope_id=_WAVE_A)) is True


def test_session_routes_event_false_when_scope_differs() -> None:
    """An envelope scoped to a sibling wave does not route to this tile."""
    sess = _session("S-1", scope_id=_WAVE_A)
    assert session_routes_event(sess, _event("EV-1", scope_id=_WAVE_B)) is False


def test_session_routes_event_false_when_scope_is_none() -> None:
    """An envelope with no scope id routes to no tile."""
    sess = _session("S-1", scope_id=_WAVE_A)
    assert session_routes_event(sess, _event("EV-1", scope_id=None)) is False


# --------------------------------------------------------------------------
# Mounted grid -- per-session routing, honest-empty, degraded
# --------------------------------------------------------------------------


def test_grid_routes_each_event_to_its_own_tile_only() -> None:
    """Two ACTIVE sessions, one event each -> each lands in its OWN tile.

    The load-bearing routing assertion: a pushed event for session A updates
    tile A only and not tile B, and vice versa. The off-tile column stays empty.
    """
    sessions = [_session("S-1", scope_id=_WAVE_A), _session("S-2", scope_id=_WAVE_B)]
    grid = WatchGrid(sessions, degraded=False, mode="unicode")

    async def body() -> None:
        app = _HostApp(grid=grid)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            # Two tiles laid out, one per ACTIVE executor session.
            assert len(grid.query(f".{WATCH_TILE_CLASS}")) == 2
            # One event for each session's wave.
            routed_a = grid.append_event(_event("EV-A", scope_id=_WAVE_A, summary="for A"))
            routed_b = grid.append_event(_event("EV-B", scope_id=_WAVE_B, summary="for B"))
            await settle_screen(pilot)
            # Each event routed to its OWN session's tile.
            assert routed_a == "S-1"
            assert routed_b == "S-2"
            tile_a = grid.query_one(f"#{tile_dom_id('S-1')}", WatchTile)
            tile_b = grid.query_one(f"#{tile_dom_id('S-2')}", WatchTile)
            rows_a = [
                str(r.render()) for r in tile_a.query(f".{WATCH_TILE_ROW_CLASS}").results(Static)
            ]
            rows_b = [
                str(r.render()) for r in tile_b.query(f".{WATCH_TILE_ROW_CLASS}").results(Static)
            ]
            # Tile A carries A's event and NOT B's; tile B the mirror.
            assert any("for A" in row for row in rows_a)
            assert all("for B" not in row for row in rows_a)
            assert any("for B" in row for row in rows_b)
            assert all("for A" not in row for row in rows_b)

    asyncio.run(body())


def test_grid_off_grid_event_routes_to_no_tile() -> None:
    """A pushed event for a wave with no tile lands in no tile at all."""
    sessions = [_session("S-1", scope_id=_WAVE_A), _session("S-2", scope_id=_WAVE_B)]
    grid = WatchGrid(sessions, degraded=False, mode="unicode")

    async def body() -> None:
        app = _HostApp(grid=grid)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            routed = grid.append_event(_event("EV-X", scope_id="P09-I09-W09", summary="off-grid"))
            await settle_screen(pilot)
            assert routed is None
            for sid in ("S-1", "S-2"):
                tile = grid.query_one(f"#{tile_dom_id(sid)}", WatchTile)
                assert len(tile.query(f".{WATCH_TILE_ROW_CLASS}")) == 0

    asyncio.run(body())


def test_grid_zero_sessions_renders_honest_empty() -> None:
    """A scope with zero dispatched sessions renders the honest-empty notice."""
    grid = WatchGrid([], degraded=False, mode="unicode")

    async def body() -> None:
        app = _HostApp(grid=grid)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert len(grid.query(f".{WATCH_TILE_CLASS}")) == 0
            notice = grid.query_one(f"#{WATCH_GRID_EMPTY_ID}", Static)
            rendered = str(notice.render())
            assert EMPTY_NOTICE in rendered
            assert WATCH_DEGRADED not in rendered

    asyncio.run(body())


def test_grid_daemon_unreachable_renders_degraded() -> None:
    """A daemon-unreachable (degraded) App renders the WATCH_DEGRADED notice."""
    grid = WatchGrid([], degraded=True, mode="unicode")

    async def body() -> None:
        app = _HostApp(grid=grid, degraded=True)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert len(grid.query(f".{WATCH_TILE_CLASS}")) == 0
            notice = grid.query_one(f"#{WATCH_GRID_EMPTY_ID}", Static)
            rendered = str(notice.render())
            assert WATCH_DEGRADED in rendered

    asyncio.run(body())


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never resolve a real daemon socket under the bare host."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
