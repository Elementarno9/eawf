"""Movement-liveness gate: an advertised navigation key must MOVE the selection.

The W28 interaction-liveness gate (``test_interaction_liveness_w28``) presses
each footer-advertised key and asserts a visible response over four OR-ed
channels -- a text-frame delta, a toast, a screen change, or a focused-cursor
move. That gate has a blind spot for MOVEMENT keys: a navigation key (up / down
/ left / right) that only SCROLLS its pane repaints new pixels while the
selection cursor stays put, so the frame-delta channel counts the scroll as a
response and passes a DEAD selection key. This is exactly the pre-W01 arrow-trap
bug: before W01 the roadmap-tree arrows scrolled the pane without advancing its
cursor, yet the frame changed.

This module gates that class. It pins
:func:`~eawf.surfaces.tui.snapshot.pilot_harness.assert_footer_movement_key_moves_selection`,
which captures the active surface's SELECTION identity
(:func:`~eawf.surfaces.tui.snapshot.pilot_harness._selection_signature`) around
the press and asserts it changed -- not merely that some pixels repainted. Where
a surface exposes no selection identity the helper falls back to the frame-change
assertion so the gate stays total.

Three halves, matching the wave's success criteria:

* **The helper (criterion 1).** Unit tests mount bare stub surfaces: a key that
  advances a ``selected`` index passes, a key that moves a focused DataTable row
  cursor passes, and a selection-less surface defers to the frame-change gate
  (a mutating key passes, a silent no-op raises).

* **The regression pin (criterion 2).** ``_TrapPane`` reproduces the pre-W01
  arrow-trap: its Down scrolls a windowed list (a frame delta) while the
  selection index never moves. The old frame-delta gate is shown to PASS the
  trap; the movement gate RaisesAssertionError -- the exact defect it exists to
  catch.

* **The real modes (criterion 3).** The gate runs green across the three modes
  that expose a selection identity: the evidence pane (a focused report-row
  cursor), the research board (its flat ``selected`` tree cursor), and the watch
  lane grid (the ``LaneGrid.selected`` Enter-zoom target).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast

import orjson
import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.pilot import Pilot
from textual.reactive import reactive
from textual.widgets import DataTable, Static

from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    stage_campaign,
)
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    OpenQuestionStatus,
    ProjectStatus,
    ScopeKind,
    StoreKind,
)
from eawf.kernel.state.models import (
    CurrentPointers,
    OpenQuestion,
    Project,
    State,
)
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    ExecutorReportBody,
    store_kind_for_role,
)
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.agent_watch import LaneGrid, LaneGridRow, LaneState
from eawf.surfaces.tui.modes.evidence import EvidenceModeScreen
from eawf.surfaces.tui.modes.research_board import ResearchBoardModeScreen
from eawf.surfaces.tui.snapshot.pilot_harness import (
    _selection_signature,
    assert_footer_key_responds,
    assert_footer_movement_key_moves_selection,
    settle_screen,
)
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.surfaces.tui.widgets.git_pane import GitFields

_T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
#: A rich repo fixture: an ACTIVE phase / iter / in-progress wave, so the
#: evidence report join has a wave to bind two seeded report rows to.
_REPO_FIXTURE = _FIXTURES / "03-phase-iter-wave-active.json"
_SEEDED_WAVE = "P01-I01-W01"
_SIZE = (200, 60)
#: The committed theme stylesheet the bare watch host loads so the lane rows'
#: ``$accent`` / selection-tint markup resolves under a Pilot.
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"

assert _THEME.is_file(), f"missing theme: {_THEME}"


# --------------------------------------------------------------------------
# Autouse isolation (registry + git probe), mirroring the sibling mode suites
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect registry + Doctor instrument-probe writes into ``tmp_path``."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(tmp_path / "instrument-probe.json"))


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the workspace git probe to a deterministic clean tree."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


# --------------------------------------------------------------------------
# Bare stub surfaces -- pin the helper directly (criteria 1 + 2)
# --------------------------------------------------------------------------


class _SelectPane(Static):
    """A focusable pane whose Down advances a real ``selected`` index.

    The positive control: the movement key both repaints AND moves the selection
    index, so the movement gate passes and the selection signature changes.
    """

    can_focus = True
    BINDINGS: ClassVar[list[BindingType]] = [Binding("down", "select_next", "down")]
    #: The selection cursor a real movement key advances.
    selected: int = 0

    def on_mount(self) -> None:
        """Seed three items and focus the pane so Down reaches its binding."""
        self._items = ("alpha", "beta", "gamma")
        self.update(self._render_items())
        self.focus()

    def _render_items(self) -> str:
        return "\n".join(
            f"{'>' if index == self.selected else ' '} {item}"
            for index, item in enumerate(self._items)
        )

    def action_select_next(self) -> None:
        """Advance the selection to the next item (clamped at the bottom)."""
        self.selected = min(len(self._items) - 1, self.selected + 1)
        self.update(self._render_items())


class _SelectApp(App[None]):
    """A bare host carrying one selectable pane."""

    def compose(self) -> ComposeResult:
        yield _SelectPane()


class _TrapPane(Static):
    """A focusable pane whose Down SCROLLS the window but never moves selection.

    Reproduces the pre-W01 arrow-trap: the Down key repaints a windowed list (a
    visible frame delta) while the ``selected`` cursor index -- the value a real
    selection key would advance -- stays pinned at 0. The frame-delta gate counts
    the repaint as a response; the movement gate must RED because the selection
    did not move.
    """

    can_focus = True
    BINDINGS: ClassVar[list[BindingType]] = [Binding("down", "scroll_only", "scroll")]
    #: The tree cursor a real movement key would advance; the scroll leaves it pinned.
    selected: int = 0

    def on_mount(self) -> None:
        """Seed the window at the top and focus the pane so Down reaches it."""
        self._offset = 0
        self.update(self._window())
        self.focus()

    def _window(self) -> str:
        return "\n".join(f"line-{self._offset + row}" for row in range(6))

    def action_scroll_only(self) -> None:
        """Repaint a new window of lines (a frame delta) WITHOUT moving selection."""
        self._offset += 1
        self.update(self._window())


class _TrapApp(App[None]):
    """A bare host carrying the scroll-trap pane."""

    def compose(self) -> ComposeResult:
        yield _TrapPane()


class _NoSelectionApp(App[None]):
    """A surface with NO selection identity: the gate must fall back to frame-change.

    Nothing focusable carries a cursor and no widget exposes a ``selected`` index,
    so :func:`_selection_signature` returns ``None`` and
    :func:`assert_footer_movement_key_moves_selection` defers to the frame-change
    liveness assertion -- a mutating key passes, a silent no-op raises.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("down", "mutate", "down"),
        Binding("up", "noop", "up"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("original", id="body")

    def action_mutate(self) -> None:
        """Rewrite the body line -- a visible frame delta, no selection identity."""
        self.query_one("#body", Static).update("changed")

    def action_noop(self) -> None:
        """Resolve to a handler that does nothing -- a silent no-op."""
        return None


class _CursorTableApp(App[None]):
    """A bare host whose focused DataTable moves a row cursor without text change."""

    def compose(self) -> ComposeResult:
        yield DataTable(id="probe-table")

    def on_mount(self) -> None:
        """Seed three rows and focus the table so Down moves its row cursor."""
        table = self.query_one("#probe-table", DataTable)
        table.add_column("cell")
        table.add_rows([("row-0",), ("row-1",), ("row-2",)])
        table.focus()


def test_movement_gate_passes_when_selection_index_advances() -> None:
    """A Down that advances a ``selected`` index passes and moves the signature."""

    async def body() -> None:
        app = _SelectApp()
        async with app.run_test(size=(60, 12)) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            before = _selection_signature(app)
            assert before is not None
            await assert_footer_movement_key_moves_selection(pilot, "down", hint="select down")
            after = _selection_signature(app)
            assert after != before

    asyncio.run(body())


def test_movement_gate_passes_on_focused_datatable_row_cursor() -> None:
    """A Down that moves a focused DataTable row cursor passes (the cursor identity).

    The row-cursor move repaints only a style highlight, so the plain-text frame
    is byte-identical; the movement gate reads the focused widget's cursor as the
    selection identity, so the move still registers.
    """

    async def body() -> None:
        app = _CursorTableApp()
        async with app.run_test(size=(60, 12)) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            before = _selection_signature(app)
            assert before is not None
            await assert_footer_movement_key_moves_selection(pilot, "down", hint="table down")
            after = _selection_signature(app)
            assert after != before

    asyncio.run(body())


def test_movement_gate_reds_when_key_only_scrolls_pre_w01_trap() -> None:
    """A Down that only SCROLLS (no selection move) reds the gate (the pre-W01 trap).

    The headline regression pin: before W01 the roadmap-tree arrows scrolled the
    pane without advancing its cursor, and the frame-delta liveness channel
    counted that scroll as a live response. This proves the movement gate is not
    fooled: the OLD frame-change gate PASSES the trap (the scroll repaints), but
    the movement gate raises because the ``selected`` index never moved.
    """

    async def body() -> None:
        app = _TrapApp()
        async with app.run_test(size=(60, 8)) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            # The OLD frame-delta gate is FOOLED: the scroll repaints, so it
            # counts the dead selection key as a live response.
            fooled = await assert_footer_key_responds(pilot, "down", hint="trap down")
            assert fooled.frame_changed is True
            # The movement gate is NOT fooled: the selection index never moved.
            with pytest.raises(AssertionError, match="did not move the selection"):
                await assert_footer_movement_key_moves_selection(pilot, "down", hint="trap down")

    asyncio.run(body())


def test_movement_gate_falls_back_to_frame_change_without_selection() -> None:
    """With no selection identity the gate defers to the frame-change assertion.

    A mutating key passes through the fallback; a silent no-op raises the
    frame-change gate's "no visible response" error -- so the gate stays total
    over surfaces that expose no selection.
    """

    async def body() -> None:
        app = _NoSelectionApp()
        async with app.run_test(size=(60, 8)) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            assert _selection_signature(app) is None
            # Fallback PASSES on a mutating key (frame delta).
            passed = await assert_footer_movement_key_moves_selection(pilot, "down", hint="mutate")
            assert passed.frame_changed is True
            # Fallback RAISES on a silent no-op key.
            with pytest.raises(AssertionError, match="no visible response"):
                await assert_footer_movement_key_moves_selection(pilot, "up", hint="noop")

    asyncio.run(body())


# --------------------------------------------------------------------------
# State + report-store seeding for the real modes (criterion 3)
# --------------------------------------------------------------------------


def _append_executor_report(state_path: Path, base_id: str, attempt: int) -> None:
    """Append one executor-report envelope keyed by ``base_id`` to the store."""
    report_body = ExecutorReportBody(
        role="executor",
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary=f"attempt {attempt} complete",
        wave_id=base_id,
        outcome="done",
    )
    report_id = f"AR-executor-{base_id}-{attempt:02d}"
    header = AgentReportHeader(
        report_id=report_id,
        role=AgentSessionRole.EXECUTOR,
        session_id=f"SES-{attempt}",
        scope_id=base_id,
        base_id=base_id,
        attempt=attempt,
        runtime="codex",
        generated_at=_T0,
        summary=f"attempt {attempt} complete",
    )
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(AgentSessionRole.EXECUTOR),
        scope_id=base_id,
        created_at=_T0 + timedelta(minutes=attempt),
        updated_at=None,
        summary=report_body.summary,
        payload=AgentReportPayload(header=header, body=report_body).model_dump(mode="json"),
    )
    path = store_path(state_path, store_kind_for_role(AgentSessionRole.EXECUTOR))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(envelope.model_dump_json() + "\n")


def _seed_evidence_state(tmp_path: Path) -> Path:
    """Write the rich fixture to a writable path + seed two evidence report rows."""
    state = State.model_validate(orjson.loads(_REPO_FIXTURE.read_bytes()))
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))
    for attempt in (1, 2):
        _append_executor_report(state_path, _SEEDED_WAVE, attempt)
    return state_path


def _research_state() -> State:
    """Build a minimal repo state carrying one open question (a walkable tree)."""
    question = OpenQuestion(
        id="OQ-0001",
        scope_id="QR",
        title="Which curve model fits the short tenor",
        status=OpenQuestionStatus.OPEN,
        blocking=False,
        created_at=_T0,
    )
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
            "claims": None,
            "open_questions": {"OQ-0001": question.model_dump(mode="json")},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _campaign_payload(campaign_id: str = "RC-0001") -> ResearchCampaignPayload:
    """Stage a two-domain campaign and wrap it in a persistable payload."""
    block = ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(focus="venues + flow"),
            "pricing-models": ResearchDomainConfig(depth=ResearchDepth.DEEP),
        },
    )
    campaign = stage_campaign("Survey the options-pricing landscape", block)
    return ResearchCampaignPayload(campaign_id=campaign_id, config=block, campaign=campaign)


def _seed_research_state(tmp_path: Path) -> Path:
    """Write the research state + append a campaign so the tree has walkable nodes."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(_research_state().model_dump_json(), encoding="utf-8")
    payload = _campaign_payload()
    envelope = Envelope(
        id=payload.campaign_id,
        kind=StoreKind.RESEARCH_CAMPAIGN,
        scope_id="QR",
        created_at=_T0,
        updated_at=_T0,
        summary=f"campaign {payload.campaign_id}",
        payload=payload.model_dump(mode="json"),
    )
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(envelope.model_dump_json().encode("utf-8") + b"\n")
    return state_path


# --------------------------------------------------------------------------
# The watch lane grid -- a bare themed host over the FA3 selection surface
# --------------------------------------------------------------------------


class _WatchHostApp(App[None]):
    """A bare themed host carrying the watch mode's FA3 lane grid.

    Mirrors the sibling lane-grid suite's host: it registers the Ea themes so the
    lane rows' ``$accent`` / selection-tint markup resolves, and exposes the
    ``render_mode`` reactive the grid reads. The lane grid IS the watch mode's
    selectable Enter-zoom surface, so driving it under a Pilot exercises the
    watch-mode selection identity without standing up a live fleet run.
    """

    CSS_PATH: ClassVar[str] = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self, *, grid: LaneGrid) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self._grid = grid

    def compose(self) -> ComposeResult:
        yield self._grid

    def on_lane_grid_zoom(self, message: LaneGrid.Zoom) -> None:
        """Swallow the zoom message so a stray Enter never bubbles to the app."""
        message.stop()


def _lane_rows() -> tuple[LaneGridRow, ...]:
    """Build three selectable lane rows spanning three lifecycle states."""
    return (
        LaneGridRow(
            wave_id="P01-I01-W01",
            vendor="claude",
            elapsed_label="42m",
            spend_label="1.2k tok $0.30",
            tier_badge="MECH",
            sandbox_label="open",
            state=LaneState.RUNNING,
        ),
        LaneGridRow(
            wave_id="P01-I01-W02",
            vendor="codex",
            elapsed_label="30m",
            spend_label="900 tok $0.20",
            tier_badge="HIGH",
            sandbox_label="1 denied",
            state=LaneState.CLOSED,
        ),
        LaneGridRow(
            wave_id="P01-I01-W03",
            vendor="claude",
            elapsed_label="12m",
            spend_label="500 tok $0.10",
            tier_badge="MECH",
            sandbox_label="open",
            state=LaneState.FAILED,
        ),
    )


# --------------------------------------------------------------------------
# The real modes -- the extended gate runs green across research / evidence / watch
# --------------------------------------------------------------------------


def test_movement_gate_evidence_row_cursor_moves(tmp_path: Path) -> None:
    """Evidence Down moves the report-row cursor, so the movement gate passes.

    The evidence pane auto-focuses its report DataTable; with two seeded report
    rows a Down advances the row cursor -- a selection identity the movement gate
    captures and asserts changed.
    """
    state_path = _seed_evidence_state(tmp_path)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            await pilot.press("6")  # -> evidence
            await settle_screen(pilot)
            assert isinstance(app.screen, EvidenceModeScreen)
            before = _selection_signature(app)
            assert before is not None
            await assert_footer_movement_key_moves_selection(pilot, "down", hint="evidence up-down")
            assert _selection_signature(app) != before

    asyncio.run(body())


def test_movement_gate_research_selection_moves(tmp_path: Path) -> None:
    """Research Down advances the flat tree ``selected`` cursor, not the scroll.

    With a campaign + open question seeded the flat node tree has several rows;
    the movement gate captures the screen-level ``selected`` index (the research
    board exposes no focused-widget cursor) and asserts a Down moved it -- the W01
    selection-not-scroll contract lifted into the general gate.
    """
    state_path = _seed_research_state(tmp_path)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            await pilot.press("3")  # -> research_board
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            assert len(pane._tree) >= 2
            assert pane.selected == 0
            before = _selection_signature(app)
            assert before is not None
            await assert_footer_movement_key_moves_selection(pilot, "down", hint="research up-down")
            assert pane.selected == 1
            assert _selection_signature(app) != before

    asyncio.run(body())


def test_movement_gate_watch_lane_selection_moves() -> None:
    """Watch lane-grid Down advances the ``LaneGrid.selected`` Enter-zoom target.

    The lane grid is the watch mode's selectable FA3 surface; the movement gate
    captures its ``selected`` index and asserts a Down moved it (a repaint of the
    selection tint that the plain-text frame alone would under-count).
    """
    grid = LaneGrid(_lane_rows(), mode="unicode")

    async def body() -> None:
        app = _WatchHostApp(grid=grid)
        async with app.run_test(size=_SIZE) as raw:
            pilot = cast(Pilot[object], raw)
            await settle_screen(pilot)
            grid.focus()
            await settle_screen(pilot)
            assert grid.selected == 0
            before = _selection_signature(app)
            assert before is not None
            await assert_footer_movement_key_moves_selection(pilot, "down", hint="watch up-down")
            assert grid.selected == 1
            assert _selection_signature(app) != before

    asyncio.run(body())
