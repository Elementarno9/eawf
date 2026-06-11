"""Tests for the FA3 parallel-session lane grid (P30-I13-W03).

The Watch mode's FA3 lane grid generalizes the I07-W08 watch grid into a
one-row-per-lane fleet lens: each row reads
``<sigil> <wave> <vendor> <elapsed> <tok/$> <tier> <sandbox> <detail>`` (the
``<sandbox>`` column is the U5 cross-vendor parity lens) with a distinct
lifecycle :class:`~eawf.surfaces.tui.widgets.sigils.Sigil` per running / closed /
failed / fork state. Arrows move the selection; Enter posts a
:class:`~eawf.surfaces.tui.modes.agent_watch.LaneGrid.Zoom` message naming the
selected lane's wave so the host zooms it to the FA4 single-session view (C1). A
fleet with no in-flight lane renders the honest-empty literal
:data:`~eawf.surfaces.tui.modes.agent_watch.LANE_GRID_EMPTY` -- never a
fabricated row (C2).

These tests pin both halves:

* the pure builder + renderer --
  :func:`~eawf.surfaces.tui.modes.agent_watch.lane_grid_rows` (lane projection +
  state classification + honest-empty),
  :func:`~eawf.surfaces.tui.modes.agent_watch.render_lane_row` (the
  eight-column row including the U5 sandbox-parity column), and
  :func:`~eawf.surfaces.tui.modes.agent_watch.lane_state_sigil_markup` (the
  per-state lifecycle sigil) -- tested against directly-built rows so the logic
  is verified without mounting Textual; and
* the mounted :class:`~eawf.surfaces.tui.modes.agent_watch.LaneGrid` under a
  Pilot on a bare themed host: a four-lane run lays out four rows with the four
  states, up/down moves the selection, Enter posts the Zoom message for the
  selected lane, and a lane-less run renders the honest-empty literal.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting,
and the running-lane elapsed window is pinned via the ``now`` reference so the
projection is a pure function of the fixture state.
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
from eawf.runtime.sandbox.policy import SandboxPolicy
from eawf.surfaces.tui.modes.agent_watch import (
    LANE_GRID_EMPTY,
    LANE_GRID_EMPTY_ID,
    LANE_GRID_ROW_CLASS,
    LANE_SELECTED_CLASS,
    LaneGrid,
    LaneGridRow,
    LaneState,
    lane_grid_rows,
    lane_parity_key,
    lane_state_sigil_markup,
    render_lane_row,
)
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph
from eawf.surfaces.tui.widgets.status_tint import SELECTION_TINT

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
#: A fixed reference time the running-lane elapsed window measures to, so the
#: lane projection is a pure function of the fixture state (no wall clock).
_NOW = _T0 + timedelta(minutes=42)

#: A wide terminal so the seven-column lane rows lay out unwrapped.
_SIZE = (160, 40)

assert _THEME.is_file(), f"missing theme: {_THEME}"


class _HostApp(App[None]):
    """Bare themed host carrying the read-only surface the grid reads.

    The grid reads ``render_mode`` (the row sigil column) off ``self.app``. No
    daemon socket is exposed, so the grid never reaches off-disk.
    """

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self, *, grid: LaneGrid) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self._grid = grid
        self.zoomed: list[str] = []

    def compose(self) -> ComposeResult:
        yield self._grid

    def on_lane_grid_zoom(self, message: LaneGrid.Zoom) -> None:
        """Record the zoomed wave so the test asserts Enter fired."""
        message.stop()
        self.zoomed.append(message.wave_id)


def _session(sid: str, *, scope_id: str, runtime: str = "claude") -> AgentSession:
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


def _lane(wave_id: str, *, session_id: str | None = None) -> FleetLane:
    """Build an in-flight fleet lane for *wave_id*."""
    return FleetLane(wave_id=wave_id, attempt=1, session_id=session_id, dispatched_at=_T0)


def _fork(wave_id: str, *, tier: RiskTier = RiskTier.HIGH) -> FleetFork:
    """Build a queued blocking fork for *wave_id* at *tier*."""
    return FleetFork(
        wave_id=wave_id,
        attempt=1,
        risk_tier=tier,
        reason=FleetForkReason.UNCALIBRATED_JURY,
        forked_at=_T0 + timedelta(minutes=10),
    )


def _policy(
    policy_id: str,
    *,
    scope_kind: str,
    scope_id: str,
    denied_tools: list[str],
) -> SandboxPolicy:
    """Build a sandbox policy denying *denied_tools* for *scope_id*."""
    return SandboxPolicy(
        id=policy_id,
        scope_kind=scope_kind,  # type: ignore[arg-type]
        scope_id=scope_id,
        denied_tools=denied_tools,
        granted_at=_T0,
    )


def _state(
    *,
    run: FleetRun | None,
    waves: dict[str, Wave] | None = None,
    sessions: dict[str, AgentSession] | None = None,
    sandbox_policies: dict[str, SandboxPolicy] | None = None,
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
            "waves": {wid: w.model_dump(mode="json") for wid, w in (waves or {}).items()},
            "artifacts": {},
            "agent_sessions": {
                sid: s.model_dump(mode="json") for sid, s in (sessions or {}).items()
            },
            "sandbox_policies": (
                {pid: p.model_dump(mode="json") for pid, p in sandbox_policies.items()}
                if sandbox_policies is not None
                else None
            ),
            "plugins": {},
            "indexes": {},
            "fleet_run": run.model_dump(mode="json") if run is not None else None,
        }
    )


def _four_lane_state() -> State:
    """Build a four-lane run exercising all four lane states.

    A running lane (in-flight, wave IN_PROGRESS), a closed lane (in-flight slot,
    wave CLOSED), a failed lane (in-flight slot, wave FAILED), and a forked lane
    (queued in ``forks``) -- so the grid lays out one row per state.
    """
    run = FleetRun(
        run_state="draining",  # type: ignore[arg-type]
        concurrency=4,
        lanes={
            "P01-I01-W01": _lane("P01-I01-W01", session_id="S-run"),
            "P01-I01-W02": _lane("P01-I01-W02", session_id="S-closed"),
            "P01-I01-W03": _lane("P01-I01-W03", session_id="S-failed"),
        },
        forks=[_fork("P01-I01-W04", tier=RiskTier.UI)],
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


# --------------------------------------------------------------------------
# lane_grid_rows -- lane projection + state classification (boundary cases)
# --------------------------------------------------------------------------


def test_lane_grid_rows_unbound_state_is_empty() -> None:
    """An unbound state yields no lane rows (honest-empty grid path, C2)."""
    assert lane_grid_rows(None, now=_NOW) == ()


def test_lane_grid_rows_unarmed_run_is_empty() -> None:
    """A state with no fleet run yields no lane rows (honest-empty path, C2)."""
    state = _state(run=None)
    assert lane_grid_rows(state, now=_NOW) == ()


def test_lane_grid_rows_no_lanes_is_empty() -> None:
    """An armed run with zero in-flight lanes + zero forks yields no rows (C2)."""
    run = FleetRun(run_state="draining", armed_at=_T0)  # type: ignore[arg-type]
    state = _state(run=run)
    assert lane_grid_rows(state, now=_NOW) == ()


def test_lane_grid_rows_classifies_the_four_states() -> None:
    """A four-lane run projects one row per running / closed / failed / fork state."""
    rows = lane_grid_rows(_four_lane_state(), now=_NOW)
    by_wave = {row.wave_id: row.state for row in rows}
    assert by_wave == {
        "P01-I01-W01": LaneState.RUNNING,
        "P01-I01-W02": LaneState.CLOSED,
        "P01-I01-W03": LaneState.FAILED,
        "P01-I01-W04": LaneState.FORK,
    }


def test_lane_grid_rows_are_claim_ordered() -> None:
    """Lane rows are returned in natural claim order regardless of dict order."""
    rows = lane_grid_rows(_four_lane_state(), now=_NOW)
    assert [row.wave_id for row in rows] == [
        "P01-I01-W01",
        "P01-I01-W02",
        "P01-I01-W03",
        "P01-I01-W04",
    ]


def test_lane_grid_rows_resolve_vendor_from_session() -> None:
    """A lane's vendor is the runtime of its registered executor session."""
    rows = {row.wave_id: row for row in lane_grid_rows(_four_lane_state(), now=_NOW)}
    assert rows["P01-I01-W01"].vendor == "claude"
    assert rows["P01-I01-W02"].vendor == "codex"
    assert rows["P01-I01-W03"].vendor == "opencode"


def test_lane_grid_running_lane_elapsed_measures_to_now() -> None:
    """A running lane's elapsed window measures dispatch -> the now reference."""
    rows = {row.wave_id: row for row in lane_grid_rows(_four_lane_state(), now=_NOW)}
    # 42 minutes between _T0 and _NOW.
    assert rows["P01-I01-W01"].elapsed_label == "42m"


def test_lane_grid_closed_lane_elapsed_measures_to_close() -> None:
    """A closed lane's elapsed window measures dispatch -> the wave close, not now."""
    rows = {row.wave_id: row for row in lane_grid_rows(_four_lane_state(), now=_NOW)}
    # 20 minutes between _T0 dispatch and the +20m close.
    assert rows["P01-I01-W02"].elapsed_label == "20m"


def test_lane_grid_running_lane_spend_reads_runtime_counters() -> None:
    """A running lane's tok/$ reads the wave's latest runtime counters."""
    rows = {row.wave_id: row for row in lane_grid_rows(_four_lane_state(), now=_NOW)}
    assert rows["P01-I01-W01"].spend_label == "4000 tok $1.25"


def test_lane_grid_lane_without_counters_reads_unknown_spend() -> None:
    """A lane whose wave has no runtime counters reads the honest dash, not a zero."""
    rows = {row.wave_id: row for row in lane_grid_rows(_four_lane_state(), now=_NOW)}
    assert rows["P01-I01-W03"].spend_label == "--"


def test_lane_grid_sandbox_column_reads_open_when_no_policy() -> None:
    """A lane with no sandbox policy reads the honest ``open`` U5 parity column."""
    rows = {row.wave_id: row for row in lane_grid_rows(_four_lane_state(), now=_NOW)}
    assert rows["P01-I01-W01"].sandbox_label == "open"


def test_lane_grid_sandbox_column_counts_wave_scoped_denials() -> None:
    """A wave-scoped sandbox policy surfaces its deny count in the U5 parity column."""
    run = FleetRun(
        run_state="draining",  # type: ignore[arg-type]
        concurrency=1,
        lanes={"P01-I01-W01": _lane("P01-I01-W01")},
        forks=[],
        armed_at=_T0,
    )
    waves = {"P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.IN_PROGRESS)}
    policies = {
        "POL-1": _policy(
            "POL-1",
            scope_kind="wave",
            scope_id="P01-I01-W01",
            denied_tools=["Bash", "WebFetch"],
        )
    }
    state = _state(run=run, waves=waves, sandbox_policies=policies)
    rows = {row.wave_id: row for row in lane_grid_rows(state, now=_NOW)}
    assert rows["P01-I01-W01"].sandbox_label == "2 denied"


def test_lane_grid_sandbox_column_folds_global_denials() -> None:
    """A global sandbox policy covers every lane's U5 parity column."""
    run = FleetRun(
        run_state="draining",  # type: ignore[arg-type]
        concurrency=1,
        lanes={"P01-I01-W01": _lane("P01-I01-W01")},
        forks=[],
        armed_at=_T0,
    )
    waves = {"P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.IN_PROGRESS)}
    policies = {
        "POL-G": _policy(
            "POL-G",
            scope_kind="global",
            scope_id="global",
            denied_tools=["Bash"],
        )
    }
    state = _state(run=run, waves=waves, sandbox_policies=policies)
    rows = {row.wave_id: row for row in lane_grid_rows(state, now=_NOW)}
    assert rows["P01-I01-W01"].sandbox_label == "1 denied"


def test_lane_grid_fork_row_reads_recorded_risk_tier() -> None:
    """A forked lane's tier badge reads the band recorded on the fork (UI)."""
    rows = {row.wave_id: row for row in lane_grid_rows(_four_lane_state(), now=_NOW)}
    assert rows["P01-I01-W04"].tier_badge == "UI"
    assert rows["P01-I01-W04"].state is LaneState.FORK


def test_lane_grid_fork_supersedes_in_flight_row_for_same_wave() -> None:
    """A wave both in-flight and forked renders ONCE as the fork row, not twice."""
    run = FleetRun(
        run_state="draining",  # type: ignore[arg-type]
        concurrency=2,
        lanes={"P01-I01-W01": _lane("P01-I01-W01")},
        forks=[_fork("P01-I01-W01", tier=RiskTier.HIGH)],
        armed_at=_T0,
    )
    state = _state(
        run=run,
        waves={"P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.IN_PROGRESS)},
    )
    rows = lane_grid_rows(state, now=_NOW)
    assert len(rows) == 1
    assert rows[0].state is LaneState.FORK


# --------------------------------------------------------------------------
# render_lane_row + lane_state_sigil_markup -- the seven-column row
# --------------------------------------------------------------------------


def test_lane_state_sigil_maps_each_state_to_its_lifecycle_shape() -> None:
    """Each lane state renders its own distinct lifecycle sigil glyph."""
    assert glyph(Sigil.RUNNING, mode="unicode") in lane_state_sigil_markup(
        LaneState.RUNNING, mode="unicode"
    )
    assert glyph(Sigil.CLOSED, mode="unicode") in lane_state_sigil_markup(
        LaneState.CLOSED, mode="unicode"
    )
    assert glyph(Sigil.FAILED, mode="unicode") in lane_state_sigil_markup(
        LaneState.FAILED, mode="unicode"
    )
    assert glyph(Sigil.ABANDONED, mode="unicode") in lane_state_sigil_markup(
        LaneState.FORK, mode="unicode"
    )


def test_render_lane_row_carries_all_columns() -> None:
    """A rendered row names wave, vendor, elapsed, tok/$, tier, sandbox, detail."""
    row = LaneGridRow(
        wave_id="P01-I01-W01",
        vendor="claude",
        elapsed_label="42m",
        spend_label="4000 tok $1.25",
        tier_badge="HIGH",
        sandbox_label="2 denied",
        state=LaneState.RUNNING,
    )
    rendered = render_lane_row(row, selected=False, mode="unicode")
    assert glyph(Sigil.RUNNING, mode="unicode") in rendered  # the sigil
    assert "P01-I01-W01" in rendered  # the wave id
    assert "claude" in rendered  # the vendor
    assert "42m" in rendered  # the elapsed
    assert "4000 tok $1.25" in rendered  # the tok/$
    assert "HIGH" in rendered  # the tier badge
    assert "2 denied" in rendered  # the U5 sandbox-parity column
    assert "draining" in rendered  # the state detail


def test_lane_grid_selected_row_wears_the_brand_selection_tint() -> None:
    """The selected lane row's CSS carries the brand accent-dim selection tint.

    The focused-row highlight is the brand-book accent-dim
    (:data:`~eawf.surfaces.tui.widgets.status_tint.SELECTION_TINT`), not the
    leftover teal ``$accent 20%`` default -- so the Enter-zoom target reads in
    the green accent family.
    """
    css = LaneGrid.DEFAULT_CSS
    assert SELECTION_TINT in css
    assert ".watch-lane-row.-selected" in css


def test_render_lane_row_fork_detail_names_the_fork() -> None:
    """A forked row's detail names the fork (awaiting operator), not a clean state."""
    row = LaneGridRow(
        wave_id="P01-I01-W04",
        vendor="claude",
        elapsed_label="10m",
        spend_label="--",
        tier_badge="UI",
        sandbox_label="open",
        state=LaneState.FORK,
    )
    rendered = render_lane_row(row, selected=False, mode="unicode")
    assert glyph(Sigil.ABANDONED, mode="unicode") in rendered
    assert "forked" in rendered


# --------------------------------------------------------------------------
# lane_parity_key -- the poll-backstop key
# --------------------------------------------------------------------------


def test_lane_parity_key_flips_on_a_lane_state_transition() -> None:
    """A lane transition (running -> closed) flips the parity key so the body recomposes."""
    running = _state(
        run=FleetRun(
            run_state="draining",  # type: ignore[arg-type]
            lanes={"P01-I01-W01": _lane("P01-I01-W01")},
            armed_at=_T0,
        ),
        waves={"P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.IN_PROGRESS)},
    )
    closed = _state(
        run=FleetRun(
            run_state="draining",  # type: ignore[arg-type]
            lanes={"P01-I01-W01": _lane("P01-I01-W01")},
            armed_at=_T0,
        ),
        waves={
            "P01-I01-W01": _wave(
                "P01-I01-W01", status=WaveStatus.CLOSED, closed_at=_T0 + timedelta(minutes=5)
            )
        },
    )
    assert lane_parity_key(running) != lane_parity_key(closed)


# --------------------------------------------------------------------------
# Mounted LaneGrid -- rows, selection, Enter-zoom, honest-empty
# --------------------------------------------------------------------------


def test_lane_grid_mounts_one_row_per_lane() -> None:
    """A four-lane run lays out exactly four selectable lane rows under a Pilot."""
    rows = lane_grid_rows(_four_lane_state(), now=_NOW)
    grid = LaneGrid(rows, mode="unicode")

    async def body() -> None:
        app = _HostApp(grid=grid)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            mounted = grid.query(f".{LANE_GRID_ROW_CLASS}")
            assert len(mounted) == 4
            texts = [str(r.render()) for r in mounted.results(Static)]
            # Every wave + its state detail surfaces in the rendered rows.
            blob = "\n".join(texts)
            for wave_id in ("P01-I01-W01", "P01-I01-W02", "P01-I01-W03", "P01-I01-W04"):
                assert wave_id in blob
            assert "draining" in blob
            assert "closed clean" in blob
            assert "failed" in blob
            assert "forked" in blob

    asyncio.run(body())


def test_lane_grid_arrows_move_the_selection() -> None:
    """up/down move the selection through the lane rows (clamped at the ends)."""
    rows = lane_grid_rows(_four_lane_state(), now=_NOW)
    grid = LaneGrid(rows, mode="unicode")

    async def body() -> int:
        app = _HostApp(grid=grid)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert grid.selected == 0
            grid.focus()
            await pilot.press("down")
            await pilot.press("down")
            await settle_screen(pilot)
            return grid.selected

    assert asyncio.run(body()) == 2


def test_lane_grid_enter_zooms_the_selected_lane() -> None:
    """Enter posts a Zoom message naming the SELECTED lane's wave (C1).

    The load-bearing FA3 -> FA4 drill: moving the selection then pressing Enter
    zooms exactly the selected lane, not the first one.
    """
    rows = lane_grid_rows(_four_lane_state(), now=_NOW)
    grid = LaneGrid(rows, mode="unicode")

    async def body() -> list[str]:
        app = _HostApp(grid=grid)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            grid.focus()
            await pilot.press("down")  # select the second lane
            await settle_screen(pilot)
            await pilot.press("enter")  # the real key->Binding path
            await settle_screen(pilot)
            return app.zoomed

    zoomed = asyncio.run(body())
    assert zoomed == ["P01-I01-W02"]


def test_lane_grid_selected_row_carries_the_selected_class() -> None:
    """The selected lane row wears the ``-selected`` class so it reads highlighted."""
    rows = lane_grid_rows(_four_lane_state(), now=_NOW)
    grid = LaneGrid(rows, mode="unicode")

    async def body() -> bool:
        app = _HostApp(grid=grid)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            first = grid.query(f".{LANE_GRID_ROW_CLASS}").first(Static)
            return first.has_class(LANE_SELECTED_CLASS)

    assert asyncio.run(body()) is True


def test_lane_grid_zero_lanes_renders_honest_empty_literal() -> None:
    """A lane-less run renders the honest-empty literal + zero data rows (C2)."""
    grid = LaneGrid((), mode="unicode")

    async def body() -> None:
        app = _HostApp(grid=grid)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert len(grid.query(f".{LANE_GRID_ROW_CLASS}")) == 0
            notice = grid.query_one(f"#{LANE_GRID_EMPTY_ID}", Static)
            assert LANE_GRID_EMPTY in str(notice.render())

    asyncio.run(body())


def test_lane_grid_empty_enter_is_a_noop() -> None:
    """Enter on the honest-empty grid posts no Zoom (nothing to zoom)."""
    grid = LaneGrid((), mode="unicode")

    async def body() -> list[str]:
        app = _HostApp(grid=grid)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            grid.focus()
            await pilot.press("enter")
            await settle_screen(pilot)
            return app.zoomed

    assert asyncio.run(body()) == []


# --------------------------------------------------------------------------
# Screen-level FA3 -> FA4 drill: Enter on the real screen pins the zoom target
# --------------------------------------------------------------------------


class _ScreenHost(App[None]):
    """Bare themed host that mounts the real :class:`AgentWatchModeScreen`.

    The screen reads ``state`` (the fleet run + waves + sessions) and
    ``render_mode`` off ``self.app`` and renders the FA3 lane grid; no daemon
    socket is exposed.
    """

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")
    state: reactive[State | None] = reactive(None)

    def __init__(self, *, state: State | None) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self.state = state
        self._state_path: Path | None = None

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self) -> None:
        from eawf.surfaces.tui.modes.agent_watch import AgentWatchModeScreen

        self.push_screen(AgentWatchModeScreen())

    def _daemon_socket_available(self) -> bool:
        return False


def test_screen_enter_zooms_lane_into_the_fa4_single_session_view() -> None:
    """Enter on the screen's lane grid pins the FA4 target + drops the grid (C1).

    The end-to-end FA3 -> FA4 drill on the REAL screen: with a fleet run mounting
    the lane grid, selecting a lane and pressing Enter pins that lane's wave as
    the watched FA4 target and recomposes the body into the single-session zoom
    (the lane grid is gone).
    """
    from eawf.surfaces.tui.modes.agent_watch import AgentWatchModeScreen

    state = _four_lane_state()

    async def body() -> tuple[str | None, int, int]:
        app = _ScreenHost(state=state)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            # The lane grid is the live surface.
            grid = screen.query_one(LaneGrid)
            assert len(grid.query(f".{LANE_GRID_ROW_CLASS}")) == 4
            grid.focus()
            await pilot.press("down")  # select the second lane (W02)
            await settle_screen(pilot)
            await pilot.press("enter")  # FA3 -> FA4 drill
            await settle_screen(pilot)
            target_wave = screen.target.wave_id if screen.target is not None else None
            grids_left = len(screen.query(LaneGrid))
            zoom_lists = len(screen.query("#watch-list"))
            return target_wave, grids_left, zoom_lists

    target_wave, grids_left, zoom_lists = asyncio.run(body())
    assert target_wave == "P01-I01-W02"  # the selected lane is the FA4 target
    assert grids_left == 0  # the lane grid was dropped for the zoom
    assert zoom_lists == 1  # the single-session zoom is now mounted


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never resolve a real daemon socket under the bare host."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
