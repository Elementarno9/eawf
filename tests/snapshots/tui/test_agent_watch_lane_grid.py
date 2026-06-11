"""Golden snapshots for the FA3 parallel-session lane grid (P30-I13-W03).

Pins the FA3 lane grid the wave delivers, captured from the
:class:`~eawf.surfaces.tui.modes.agent_watch.AgentWatchModeScreen` mounted IN
ISOLATION on a bare themed host (mirroring the agent-watch reskin suite) so the
frame is a pure function of the bound fixture state with no off-disk daemon read:

* the populated four-lane look (C1) -- a fleet run with four lanes lays out one
  row per lane reading ``<sigil> <wave> <vendor> <elapsed> <tok/$> <tier> <detail>``
  with a distinct lifecycle sigil per running / closed / failed / fork state; and
* the honest-empty literal (C2) -- a fleet run with zero in-flight lanes renders
  the literal :data:`~eawf.surfaces.tui.modes.agent_watch.LANE_GRID_EMPTY` ("no
  sessions in flight") and zero data rows, never a fabricated lane row.

Both frames pin the unicode render mode so the sigil column is deterministic.
The host carries only the read-only ``state`` / ``_state_path`` / ``render_mode``
the screen reads; there is no daemon socket. The running-lane elapsed window is
volatile (it measures dispatch -> now), so the populated frame asserts the four
states + the honest-empty frame structurally rather than byte-pinning the
elapsed cell; the empty golden is byte-stable.

Regenerate the goldens after an intentional layout change with::

    EAWF_DAEMONLESS=1 EAWF_SNAPSHOT_REGEN=1 uv run pytest \
        tests/snapshots/tui/test_agent_watch_lane_grid.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widgets import Static

from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    ProjectStatus,
    RiskTier,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    AgentSession,
    CurrentPointers,
    FleetFork,
    FleetForkReason,
    FleetLane,
    FleetRun,
    Project,
    RuntimeLatest,
    State,
    Wave,
)
from eawf.surfaces.tui.modes.agent_watch import (
    LANE_GRID_EMPTY,
    LANE_GRID_EMPTY_ID,
    LANE_GRID_ROW_CLASS,
    AgentWatchModeScreen,
)
from eawf.surfaces.tui.snapshot import assert_screen_snapshot, settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph

_THEME = Path(__file__).resolve().parents[3] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_GOLDEN = Path(__file__).resolve().parent / "golden"
_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: A wide terminal so the seven-column lane rows lay out unwrapped.
_SIZE = (160, 40)

assert _THEME.is_file(), f"missing theme: {_THEME}"


class _HostApp(App[None]):
    """Bare themed host carrying the read-only surface the screen reads.

    The screen reads ``state`` (the fleet run + waves + sessions) and
    ``render_mode`` (the sigil column) off ``self.app``. The host exposes exactly
    those and no daemon socket, so the lane grid renders off the fixture alone.
    """

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")
    state: reactive[State | None] = reactive(None)

    def __init__(self, *, state: State | None, state_path: Path | None) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self.state = state
        self._state_path = state_path

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self) -> None:
        self.push_screen(AgentWatchModeScreen())

    def _daemon_socket_available(self) -> bool:
        """No daemon under the bare host."""
        return False


def _session(sid: str, *, scope_id: str, runtime: str) -> AgentSession:
    """Build an ACTIVE executor session scoped to *scope_id* (the lane's wave)."""
    return AgentSession(
        id=sid,
        role=AgentSessionRole.EXECUTOR,
        runtime=runtime,
        scope_id=scope_id,
        status=AgentSessionStatus.ACTIVE,
        started_at=_T0,
    )


def _wave(
    wave_id: str,
    *,
    status: WaveStatus,
    closed_at: datetime | None = None,
    cost_usd: float | None = None,
    tokens: int | None = None,
) -> Wave:
    """Build a wave row, optionally carrying close time + runtime counters."""
    latest = (
        RuntimeLatest(
            cost_usd=cost_usd,
            input_tokens=tokens,
            output_tokens=0,
            captured_at=_T0,
        )
        if (cost_usd is not None or tokens is not None)
        else None
    )
    return Wave(
        id=wave_id,
        iter_id="P01-I01",
        title=f"wave {wave_id}",
        status=status,
        opened_at=_T0,
        closed_at=closed_at,
        runtime_latest=latest,
    )


def _lane(wave_id: str, *, session_id: str) -> FleetLane:
    """Build an in-flight fleet lane for *wave_id*."""
    return FleetLane(wave_id=wave_id, attempt=1, session_id=session_id, dispatched_at=_T0)


def _state(
    *,
    run: FleetRun | None,
    waves: dict[str, Wave],
    sessions: dict[str, AgentSession],
) -> State:
    """Build a minimal repo state carrying *run* + its waves + sessions."""
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
            "waves": {wid: w.model_dump(mode="json") for wid, w in waves.items()},
            "artifacts": {},
            "agent_sessions": {sid: s.model_dump(mode="json") for sid, s in sessions.items()},
            "plugins": {},
            "indexes": {},
            "fleet_run": run.model_dump(mode="json") if run is not None else None,
        }
    )


def _four_lane_state() -> State:
    """Build a four-lane run exercising all four lane states."""
    run = FleetRun(
        run_state="draining",  # type: ignore[arg-type]
        concurrency=4,
        lanes={
            "P01-I01-W01": _lane("P01-I01-W01", session_id="S-run"),
            "P01-I01-W02": _lane("P01-I01-W02", session_id="S-closed"),
            "P01-I01-W03": _lane("P01-I01-W03", session_id="S-failed"),
        },
        forks=[
            FleetFork(
                wave_id="P01-I01-W04",
                attempt=1,
                risk_tier=RiskTier.UI,
                reason=FleetForkReason.UNCALIBRATED_JURY,
                forked_at=_T0 + timedelta(minutes=10),
            )
        ],
        armed_at=_T0,
    )
    waves = {
        "P01-I01-W01": _wave(
            "P01-I01-W01", status=WaveStatus.IN_PROGRESS, cost_usd=1.25, tokens=4000
        ),
        "P01-I01-W02": _wave(
            "P01-I01-W02", status=WaveStatus.CLOSED, closed_at=_T0 + timedelta(minutes=20)
        ),
        "P01-I01-W03": _wave("P01-I01-W03", status=WaveStatus.FAILED),
        "P01-I01-W04": _wave("P01-I01-W04", status=WaveStatus.IN_PROGRESS),
    }
    sessions = {
        "S-run": _session("S-run", scope_id="P01-I01-W01", runtime="claude"),
        "S-closed": _session("S-closed", scope_id="P01-I01-W02", runtime="codex"),
        "S-failed": _session("S-failed", scope_id="P01-I01-W03", runtime="opencode"),
    }
    return _state(run=run, waves=waves, sessions=sessions)


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


# --------------------------------------------------------------------------
# Snapshot: the populated four-lane grid (C1)
# --------------------------------------------------------------------------


def test_agent_watch_lane_grid_four_states_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mounted pane lays out one row per lane with the four lifecycle states (C1).

    The running-lane elapsed window measures dispatch -> now, the one volatile
    cell the screen-level render produces (it calls ``lane_grid_rows`` without a
    pinned ``now``). The clock is frozen to a fixed reference so the populated
    golden stays byte-stable; every other cell is a pure function of the fixture.
    """
    import eawf.surfaces.tui.modes.agent_watch as agent_watch_mod

    frozen = _T0 + timedelta(minutes=42)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return frozen

    monkeypatch.setattr(agent_watch_mod, "datetime", _FrozenDatetime)

    state = _four_lane_state()
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = _HostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            rows = [
                str(r.render())
                for r in screen.query(f".{LANE_GRID_ROW_CLASS}").results(Static)
            ]
            assert len(rows) == 4
            blob = "\n".join(rows)
            # Each lifecycle sigil renders for its state.
            assert glyph(Sigil.RUNNING, mode=app.render_mode) in blob
            assert glyph(Sigil.CLOSED, mode=app.render_mode) in blob
            assert glyph(Sigil.FAILED, mode=app.render_mode) in blob
            assert glyph(Sigil.ABANDONED, mode=app.render_mode) in blob
            # The state-detail words name the four states.
            assert "draining" in blob
            assert "closed clean" in blob
            assert "failed" in blob
            assert "forked" in blob
            assert_screen_snapshot(app, _GOLDEN / "agent_watch_lane_grid_four_states.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Snapshot: the honest-empty literal (C2)
# --------------------------------------------------------------------------


class _GridHost(App[None]):
    """Bare themed host that mounts the FA3 lane grid as its sole surface.

    Used to capture the lane grid's OWN honest-empty literal in isolation (C2),
    distinct from the screen-level fall-through to the single-session zoom.
    """

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self, *, grid: object) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self._grid = grid

    def compose(self) -> ComposeResult:
        yield self._grid  # type: ignore[misc]


def test_agent_watch_lane_grid_empty_snapshot() -> None:
    """A lane grid with zero lanes renders the honest-empty literal + zero rows (C2).

    The grid IS the surface here (mounted in isolation), so the golden pins the
    lane grid's own ``no sessions in flight`` literal -- never a fabricated lane
    row from an empty fleet.
    """
    from eawf.surfaces.tui.modes.agent_watch import LaneGrid

    grid = LaneGrid((), mode="unicode")

    async def body() -> None:
        app = _GridHost(grid=grid)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert len(grid.query(f".{LANE_GRID_ROW_CLASS}")) == 0
            notice = grid.query_one(f"#{LANE_GRID_EMPTY_ID}", Static)
            assert str(notice.render()) == LANE_GRID_EMPTY
            assert_screen_snapshot(app, _GOLDEN / "agent_watch_lane_grid_empty.txt")

    asyncio.run(body())


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-braces: never resolve a real daemon socket under the host."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
