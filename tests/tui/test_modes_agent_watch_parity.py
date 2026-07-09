"""Tests for the fleet parity lens of the Watch mode (P30-I07-W17).

The Watch mode's parity lens generalises the watch grid into a side-by-side
surface over every dispatched session: with two or more ACTIVE executor
:class:`~eawf.kernel.state.models.AgentSession` rows the body is the parallel
:class:`~eawf.surfaces.tui.modes.agent_watch.WatchGrid` (one tile per session),
fed by BOTH the App's single ``event.subscribe`` push fan-out AND an always-on
poll backstop; a scope with zero sessions renders the honest-empty
:data:`~eawf.surfaces.tui.modes.agent_watch.EMPTY_NOTICE` rather than a
fabricated grid.

These tests pin the W17 generalisation on top of the W08 grid:

* the parity-set key helper
  (:func:`~eawf.surfaces.tui.modes.agent_watch.parity_session_ids`) -- the
  id-sorted ACTIVE-executor set the poll backstop compares to decide whether
  the dispatched fleet changed;
* the **push** path under a full :class:`~eawf.surfaces.tui.app.EaApp`: two
  ACTIVE executor sessions render side-by-side in the parity grid and one
  pushed event per session lands in its OWN tile (the shared fan-out, not a
  second subscription);
* the **poll backstop**: a state-only revision (no event pushed) that adds a
  second ACTIVE executor recomposes the body from the single-session zoom into
  the side-by-side parity grid -- the always-on poll beside the push the
  project's TUI-staleness lesson pins -- and the freshly-composed tiles re-seed
  from the App's live buffer; and
* the **honest-empty** path: a zero-session scope renders the honest-empty
  notice, and a poll tick that drains the fleet to zero swaps the grid back to
  that notice rather than leaving a stale grid.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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
    EMPTY_NOTICE,
    WATCH_GRID_EMPTY_ID,
    WATCH_TILE_CLASS,
    WATCH_TILE_ROW_CLASS,
    AgentWatchModeScreen,
    WatchGrid,
    WatchTile,
    parity_session_ids,
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

#: A wide terminal so two side-by-side parity tiles lay out unwrapped.
_SIZE = (160, 40)

#: The two waves the two seeded executor sessions scope to (their ``scope_id``),
#: which the dispatch events also stamp as their own ``scope_id``.
_WAVE_A = "P01-I01-W01"
_WAVE_B = "P01-I01-W02"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home.

    Keeps a stray ``u`` scope switch (and any registry read) deterministic and
    off the operator's real registry.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never resolve a real daemon socket under the test App."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")


def _session(
    sid: str,
    *,
    scope_id: str,
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
    role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    runtime: str = "claude",
) -> AgentSession:
    """Build an executor agent-session row for the parity enumerator."""
    return AgentSession(
        id=sid,
        role=role,
        runtime=runtime,
        scope_id=scope_id,
        status=status,
        started_at=_T0,
    )


def _event(event_id: str, *, scope_id: str, summary: str) -> Envelope:
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
# parity_session_ids -- the poll-backstop parity-set key (boundary cases)
# --------------------------------------------------------------------------


def test_parity_session_ids_none_state_is_empty() -> None:
    """An unbound state yields an empty parity set (honest-empty path)."""
    assert parity_session_ids(None) == ()


def test_parity_session_ids_filters_to_active_executors() -> None:
    """Only ACTIVE executor sessions are in the parity set."""
    state = _state(
        sessions={
            "S-active": _session("S-active", scope_id=_WAVE_A),
            "S-closed": _session("S-closed", scope_id=_WAVE_B, status=AgentSessionStatus.CLOSED),
            "S-rev": _session("S-rev", scope_id=_WAVE_B, role=AgentSessionRole.REVIEWER),
        }
    )
    assert parity_session_ids(state) == ("S-active",)


def test_parity_session_ids_is_id_sorted() -> None:
    """The parity set is id-sorted so the poll-backstop key is order-stable."""
    state = _state(
        sessions={
            "S-2": _session("S-2", scope_id=_WAVE_B),
            "S-1": _session("S-1", scope_id=_WAVE_A),
        }
    )
    assert parity_session_ids(state) == ("S-1", "S-2")


# --------------------------------------------------------------------------
# Push path -- side-by-side parity grid fed by the shared event fan-out
# --------------------------------------------------------------------------


def test_parity_grid_renders_sessions_side_by_side_fed_by_push(tmp_path: Path) -> None:
    """Two ACTIVE executors render side-by-side, each fed by the shared push.

    The load-bearing W17 criterion: with two ACTIVE executor sessions the body
    is the side-by-side parity grid (two tiles), and one event pushed per
    session through the App's single ``event.subscribe`` fan-out routes to its
    OWN tile and not its sibling's -- the shared push, never a second
    subscription.
    """
    state = _state(
        sessions={
            "S-1": _session("S-1", scope_id=_WAVE_A),
            "S-2": _session("S-2", scope_id=_WAVE_B),
        }
    )
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)  # -> agent_watch
            await settle_screen(pilot)
            await pilot.press("g")  # opt into the parity grid (W22 default is the roster)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            grid = pane.query_one(WatchGrid)
            # Two tiles laid out side-by-side, one per ACTIVE executor session.
            tiles = grid.query(f".{WATCH_TILE_CLASS}")
            assert len(tiles) == 2
            # Side-by-side: both tile regions share a row, distinct columns.
            tile_a = grid.query_one(f"#{tile_dom_id('S-1')}", WatchTile)
            tile_b = grid.query_one(f"#{tile_dom_id('S-2')}", WatchTile)
            assert tile_a.region.y == tile_b.region.y
            assert tile_a.region.x != tile_b.region.x
            # One event per session, pushed through the SHARED App fan-out.
            await app._on_event(_event("EV-A", scope_id=_WAVE_A, summary="for S-1"))
            await app._on_event(_event("EV-B", scope_id=_WAVE_B, summary="for S-2"))
            await settle_screen(pilot)
            rows_a = _tile_rows(tile_a)
            rows_b = _tile_rows(tile_b)
            assert any("for S-1" in row for row in rows_a)
            assert all("for S-2" not in row for row in rows_a)
            assert any("for S-2" in row for row in rows_b)
            assert all("for S-1" not in row for row in rows_b)

    asyncio.run(body())


# --------------------------------------------------------------------------
# Poll backstop -- a state-only revision recomposes the parity grid
# --------------------------------------------------------------------------


def test_poll_backstop_grows_zoom_into_parity_grid(tmp_path: Path) -> None:
    """A state-only poll tick that adds a 2nd executor swaps zoom -> parity grid.

    The poll-backstop half of the W17 criterion: starting from ONE ACTIVE
    executor (the single-session zoom, no grid), a fresh state revision (NO
    event pushed) that adds a second ACTIVE executor recomposes the body into
    the side-by-side parity grid with both tiles. This is the always-on poll
    beside the push -- a newly dispatched session becomes visible without an
    event push and without an app restart.
    """
    one = _state(sessions={"S-1": _session("S-1", scope_id=_WAVE_A)})
    state_path = _write_state(tmp_path, one)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)  # -> agent_watch
            await settle_screen(pilot)
            await pilot.press("g")  # opt into the grid; still zoom at 1 session (W22)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            # One ACTIVE executor -> the single-session zoom, no parity grid yet.
            assert len(pane.query(WatchGrid)) == 0
            # A poll tick reveals a second ACTIVE executor (no event pushed).
            two = _state(
                sessions={
                    "S-1": _session("S-1", scope_id=_WAVE_A),
                    "S-2": _session("S-2", scope_id=_WAVE_B),
                }
            )
            await app._on_state(two)
            await settle_screen(pilot)
            # The body recomposed into the side-by-side parity grid: two tiles.
            grid = pane.query_one(WatchGrid)
            assert len(grid.query(f".{WATCH_TILE_CLASS}")) == 2
            tile_a = grid.query_one(f"#{tile_dom_id('S-1')}", WatchTile)
            tile_b = grid.query_one(f"#{tile_dom_id('S-2')}", WatchTile)
            assert tile_a.region.y == tile_b.region.y
            assert tile_a.region.x != tile_b.region.x

    asyncio.run(body())


def test_poll_backstop_reseeds_new_grid_from_buffer(tmp_path: Path) -> None:
    """A poll-backstop recompose re-seeds the new parity tiles from the buffer.

    Events buffered while the single-session zoom was mounted must replay into
    the freshly-composed parity tiles after a poll tick grows the fleet, so the
    new grid shows the events that arrived before it existed (not a blank grid).
    """
    one = _state(sessions={"S-1": _session("S-1", scope_id=_WAVE_A)})
    state_path = _write_state(tmp_path, one)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)  # -> agent_watch
            await settle_screen(pilot)
            await pilot.press("g")  # opt into the grid; still zoom at 1 session (W22)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            # Events for both waves arrive (into the buffer) while only S-1 is
            # ACTIVE -- so they predate the parity grid's tiles.
            await app._on_event(_event("EV-A", scope_id=_WAVE_A, summary="buffered for S-1"))
            await app._on_event(_event("EV-B", scope_id=_WAVE_B, summary="buffered for S-2"))
            await settle_screen(pilot)
            # A poll tick reveals the second ACTIVE executor.
            two = _state(
                sessions={
                    "S-1": _session("S-1", scope_id=_WAVE_A),
                    "S-2": _session("S-2", scope_id=_WAVE_B),
                }
            )
            await app._on_state(two)
            await settle_screen(pilot)
            grid = pane.query_one(WatchGrid)
            tile_a = grid.query_one(f"#{tile_dom_id('S-1')}", WatchTile)
            tile_b = grid.query_one(f"#{tile_dom_id('S-2')}", WatchTile)
            # The freshly-composed tiles re-seeded from the App buffer.
            assert any("buffered for S-1" in row for row in _tile_rows(tile_a))
            assert any("buffered for S-2" in row for row in _tile_rows(tile_b))

    asyncio.run(body())


def test_poll_backstop_unchanged_fleet_is_a_noop(tmp_path: Path) -> None:
    """A poll tick that leaves the fleet unchanged does NOT clobber tile rows.

    The no-op guard: a fresh state revision whose ACTIVE-executor set is the
    same must leave the already-streamed event rows in place rather than
    churning the DOM (which would drop the live-pushed rows). Distinguishes the
    poll backstop from a blind recompose-on-every-tick.
    """
    two = _state(
        sessions={
            "S-1": _session("S-1", scope_id=_WAVE_A),
            "S-2": _session("S-2", scope_id=_WAVE_B),
        }
    )
    state_path = _write_state(tmp_path, two)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            await pilot.press("g")  # opt into the parity grid (W22 default is the roster)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            grid = pane.query_one(WatchGrid)
            await app._on_event(_event("EV-A", scope_id=_WAVE_A, summary="streamed for S-1"))
            await settle_screen(pilot)
            tile_a = grid.query_one(f"#{tile_dom_id('S-1')}", WatchTile)
            assert any("streamed for S-1" in row for row in _tile_rows(tile_a))
            # A poll tick with the SAME ACTIVE-executor fleet (a re-read).
            same = _state(
                sessions={
                    "S-1": _session("S-1", scope_id=_WAVE_A),
                    "S-2": _session("S-2", scope_id=_WAVE_B),
                }
            )
            await app._on_state(same)
            await settle_screen(pilot)
            # No recompose: the same grid instance survives and keeps its row.
            assert pane.query_one(WatchGrid) is grid
            tile_a_after = grid.query_one(f"#{tile_dom_id('S-1')}", WatchTile)
            assert any("streamed for S-1" in row for row in _tile_rows(tile_a_after))

    asyncio.run(body())


# --------------------------------------------------------------------------
# Honest-empty -- zero sessions, and a poll tick draining the fleet to zero
# --------------------------------------------------------------------------


def test_parity_zero_sessions_renders_honest_empty(tmp_path: Path) -> None:
    """A scope with zero sessions renders the honest-empty notice, not a grid."""
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            # No parity grid is mounted for a zero-session scope.
            assert len(pane.query(WatchGrid)) == 0
            frame = normalize_snapshot(capture_screen_text(app))
            assert EMPTY_NOTICE in frame

    asyncio.run(body())


def test_poll_backstop_drains_grid_to_honest_empty(tmp_path: Path) -> None:
    """A poll tick that drains the fleet to zero swaps the grid for honest-empty.

    The honest-empty backstop: when the dispatched executors are gone, a fresh
    state revision recomposes the body away from the (now stale) parity grid
    back to the honest-empty notice rather than leaving a fabricated grid of
    dead tiles. Drained to a session-free scope (the real honest-empty path,
    where the zoom does NOT fall back to a CLOSED-session header).
    """
    two = _state(
        sessions={
            "S-1": _session("S-1", scope_id=_WAVE_A),
            "S-2": _session("S-2", scope_id=_WAVE_B),
        }
    )
    state_path = _write_state(tmp_path, two)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            await pilot.press("g")  # opt into the parity grid (W22 default is the roster)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            assert len(pane.query(WatchGrid)) == 1
            # A poll tick with no executor session at all -> honest-empty.
            await app._on_state(_state())
            await settle_screen(pilot)
            # The parity grid is gone; the honest-empty banner is shown.
            assert len(pane.query(WatchGrid)) == 0
            assert pane.target is None
            frame = normalize_snapshot(capture_screen_text(app))
            assert EMPTY_NOTICE in frame
            # No stale grid-empty tile container lingers either.
            assert len(pane.query(f"#{WATCH_GRID_EMPTY_ID}")) == 0

    asyncio.run(body())


def _tile_rows(tile: WatchTile) -> list[str]:
    """Return the rendered event-row strings of *tile*'s scroll column."""
    return [str(row.render()) for row in tile.query(f".{WATCH_TILE_ROW_CLASS}").results()]
