"""Golden snapshot + affordance parity for the Autopilot ready/blocked split (P30-I02-W11).

The Autopilot mode (digit ``2``) renders the cosmetic-terminal reskin of the
dependency frontier as a two-band split:

* the **ready** band lists the claim-ready waves, each leading with the
  multi-select checkbox affordance + the dispatch chrome arrow (the affordance
  LOOK only -- the actual multi-select wiring is deferred to a later wave); and
* the **blocked** band lists the PENDING waves held off the frontier, each
  naming the dep blocking it (e.g. ``<- P01-I01-W02``).

These tests pin two things:

* a golden snapshot of the mounted split (ready band with the dispatch arrow +
  checkbox affordance, blocked band naming each row's dep); and
* an ``affordance_parity`` assertion that the dispatch action key (``d``)
  resolves to a LIVE :class:`~textual.binding.Binding` (``dispatch_selected``)
  and FIRES -- the action method runs and moves the result line off its idle
  surface.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 EAWF_DAEMONLESS=1 \
        uv run pytest tests/snapshots/tui/test_autopilot_split.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.spec.auq_bridge import compute_ready_frontier
from eawf.kernel.state.enums import ProjectStatus, ScopeKind, WaveStatus
from eawf.kernel.state.models import CurrentPointers, Project, State, Wave
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.autopilot import (
    BLOCKED_BY_MARKER,
    BLOCKED_CAPTION,
    BLOCKED_ROW_CLASS,
    COCKPIT_IDLE,
    DISPATCH_FLAVOUR,
    DISPATCH_IDLE,
    DISPATCH_RESULT_ID,
    EMPTY_NOTICE,
    FRONTIER_ROW_CLASS,
    READY_CAPTION,
    AutopilotModeScreen,
    BlockedWaveRow,
    ReadyWaveRow,
    blocked_rows,
    build_frontier_items,
    render_blocked_row,
    render_frontier_header,
    render_ready_row,
)
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
    toast_messages,
)
from eawf.surfaces.tui.widgets import sigils

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
_SIZE = (120, 40)
_AUTOPILOT_DIGIT = "2"
_DISPATCH_KEY = "d"
_GOLDEN = Path(__file__).resolve().parent / "golden"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _wave(
    wave_id: str,
    *,
    status: WaveStatus,
    deps: list[str] | None = None,
    iter_id: str = "P01-I01",
    title: str | None = None,
) -> Wave:
    """Build a wave row for the frontier projection."""
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=title or f"Wave {wave_id}",
        status=status,
        deps=deps or [],
        opened_at=_T0,
    )


def _split_state() -> State:
    """Build a state whose frontier splits into one ready + two blocked waves.

    W01 is CLOSED; W02 is PENDING with W01 closed (the lone ready wave); W03 is
    PENDING with W01 closed but held off the frontier by its lower-numbered
    ready sibling W02 (the monotonic claim-order gate); W04 is PENDING with the
    still-open W02 as its dep. So the ready band is ``(W02,)`` and the blocked
    band is ``(W03 <- W02, W04 <- W02)`` in claim order.
    """
    waves = {
        "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED, title="Closed groundwork"),
        "P01-I01-W02": _wave(
            "P01-I01-W02",
            status=WaveStatus.PENDING,
            deps=["P01-I01-W01"],
            title="Add ready frontier",
        ),
        "P01-I01-W03": _wave(
            "P01-I01-W03",
            status=WaveStatus.PENDING,
            deps=["P01-I01-W01"],
            title="Held by sibling",
        ),
        "P01-I01-W04": _wave(
            "P01-I01-W04",
            status=WaveStatus.PENDING,
            deps=["P01-I01-W02"],
            title="Waiting on open dep",
        ),
    }
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
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _dry_frontier_state() -> State:
    """Build a state whose ready/blocked frontier is empty (all waves CLOSED).

    Every wave is CLOSED, so :func:`compute_ready_frontier` yields no
    claim-ready wave and no held PENDING wave -- the dry-frontier honest-empty
    path the Autopilot list renders as the centered honest-empty hero.
    """
    waves = {
        "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED, title="Closed groundwork"),
        "P01-I01-W02": _wave("P01-I01-W02", status=WaveStatus.CLOSED, title="Closed follow-up"),
    }
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
            "agent_sessions": {},
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
# Pure helpers -- ready/blocked split + reskin glyphs (no Textual mount)
# --------------------------------------------------------------------------


def test_blocked_rows_names_open_dep_and_holding_sibling() -> None:
    """Each blocked row names the wave holding it off the frontier.

    W03 is held by its lower-numbered ready sibling W02; W04 is held by its
    still-open dep W02. Both blocked rows therefore name ``P01-I01-W02`` as
    their blocker, in claim order, while the ready wave W02 stays off the
    blocked band.
    """
    state = _split_state()
    frontier = compute_ready_frontier(build_frontier_items(state))
    rows = blocked_rows(frontier, state)
    assert tuple(row.wave_id for row in rows) == ("P01-I01-W03", "P01-I01-W04")
    assert all(row.blocked_by == "P01-I01-W02" for row in rows)
    # The ready wave is never a blocked row.
    assert "P01-I01-W02" not in {row.wave_id for row in rows}


def test_blocked_rows_empty_when_no_pending_held() -> None:
    """A frontier with no held PENDING wave yields no blocked rows."""
    waves = {
        "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED),
        "P01-I01-W02": _wave("P01-I01-W02", status=WaveStatus.PENDING, deps=["P01-I01-W01"]),
    }
    state = State.model_validate(
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
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )
    frontier = compute_ready_frontier(build_frontier_items(state))
    assert blocked_rows(frontier, state) == ()


def test_render_blocked_row_names_blocking_dep() -> None:
    """A blocked row renders the wave id, iter, title, and the ``<- dep`` marker."""
    row = BlockedWaveRow(
        wave_id="P01-I01-W04",
        iter_id="P01-I01",
        title="Waiting on open dep",
        blocked_by="P01-I01-W02",
    )
    body = render_blocked_row(row)
    assert "P01-I01-W04" in body
    assert "Waiting on open dep" in body
    assert f"{BLOCKED_BY_MARKER} P01-I01-W02" in body


def test_render_ready_row_selected_shows_filled_checkbox_and_arrow() -> None:
    """A selected ready row shows the filled checkbox + dispatch arrow look."""
    row = ReadyWaveRow(wave_id="P01-I01-W02", iter_id="P01-I01", title="Add ready frontier")
    body = render_ready_row(row, selected=True, mode="unicode")
    assert sigils.chrome("check_on", mode="unicode") in body  # filled box
    assert sigils.chrome("dispatch", mode="unicode") in body  # dispatch arrow
    assert "P01-I01-W02" in body


def test_render_ready_row_unselected_shows_hollow_checkbox() -> None:
    """An unselected ready row shows the hollow checkbox affordance."""
    row = ReadyWaveRow(wave_id="P01-I01-W02", iter_id="P01-I01", title="Add ready frontier")
    body = render_ready_row(row, selected=False, mode="unicode")
    assert sigils.chrome("check_off", mode="unicode") in body  # hollow box


def test_render_ready_row_ascii_mode_uses_ascii_affordance() -> None:
    """ASCII render mode resolves the affordance glyphs in the ASCII column."""
    row = ReadyWaveRow(wave_id="P01-I01-W02", iter_id="P01-I01", title="Add ready frontier")
    body = render_ready_row(row, selected=True, mode="ascii")
    assert sigils.chrome("dispatch", mode="ascii") in body  # ascii ">"
    assert sigils.chrome("check_on", mode="ascii") in body  # ascii "[x]"


def test_render_frontier_header_leads_with_dispatch_arrow() -> None:
    """The populated header leads with the dispatch chrome arrow + blocked tally."""
    ready = (ReadyWaveRow(wave_id="P01-I01-W02", iter_id="P01-I01", title="r"),)
    blocked = (
        BlockedWaveRow(
            wave_id="P01-I01-W03", iter_id="P01-I01", title="b", blocked_by="P01-I01-W02"
        ),
    )
    body = render_frontier_header(ready, blocked, mode="unicode")
    assert sigils.chrome("dispatch", mode="unicode") in body
    assert "1 blocked" in body


# --------------------------------------------------------------------------
# Golden snapshot: the mounted ready/blocked split + reskin glyphs
# --------------------------------------------------------------------------


def test_autopilot_split_snapshot(tmp_path: Path) -> None:
    """The mounted Autopilot pane renders the ready/blocked split golden.

    Seeds a frontier whose ready band is ``(W02,)`` and blocked band is
    ``(W03 <- W02, W04 <- W02)``, mounts the mode, and snapshots the frame so a
    layout regression on the split (the ready band's dispatch arrow + checkbox
    affordance, the blocked band naming each row's dep) is caught.
    """
    state_path = _write_state(tmp_path, _split_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)  # -> autopilot
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AutopilotModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            # The split state arms no fleet run, so the cockpit vitals header
            # renders the honest-empty idle hero (the pinned spec literal).
            assert COCKPIT_IDLE in frame
            # The ready band lists the lone ready wave with the dispatch arrow.
            assert READY_CAPTION in frame
            assert "P01-I01-W02" in frame
            assert sigils.chrome("dispatch", mode="unicode") in frame
            assert sigils.chrome("check_on", mode="unicode") in frame  # selected affordance
            # The blocked band names each held wave's blocking dep.
            assert BLOCKED_CAPTION in frame
            assert f"{BLOCKED_BY_MARKER} P01-I01-W02" in frame
            assert "P01-I01-W03" in frame
            assert "P01-I01-W04" in frame
            # The dispatch result line carries the cockpit flavour hover text.
            assert DISPATCH_FLAVOUR in frame
            # Exactly one ready (dispatch-target) row; two blocked rows.
            assert len(screen.query(f".{FRONTIER_ROW_CLASS}")) == 1
            assert len(screen.query(f".{BLOCKED_ROW_CLASS}")) == 2
            assert_screen_snapshot(app, _GOLDEN / "autopilot_split.txt")

    asyncio.run(body())


def test_autopilot_empty_frontier_snapshot(tmp_path: Path) -> None:
    """A dry frontier renders the centered honest-empty hero in the list.

    Seeds a state whose every wave is CLOSED, so the ready + blocked bands are
    both empty. The Autopilot list then renders the shared honest-empty hero
    (a muted brand sigil over the ``EMPTY_NOTICE`` headline + the framing
    subline + the ``[ a arm fleet ]`` action chip) -- the same centered calm
    hero the research board + sandbox timeline render, not a top-left
    one-liner.
    """
    state_path = _write_state(tmp_path, _dry_frontier_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)  # -> autopilot
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AutopilotModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            # The honest-empty hero: brand sigil over the EMPTY_NOTICE headline.
            assert EMPTY_NOTICE in frame
            assert sigils.chrome("brand", mode="unicode") in frame
            assert "[ a arm fleet ]" in frame
            # No ready or blocked rows on a dry frontier.
            assert len(screen.query(f".{FRONTIER_ROW_CLASS}")) == 0
            assert len(screen.query(f".{BLOCKED_ROW_CLASS}")) == 0
            assert_screen_snapshot(app, _GOLDEN / "autopilot_empty_frontier.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# affordance_parity: the dispatch key resolves to a live Binding and FIRES
# --------------------------------------------------------------------------


def test_autopilot_dispatch_key_resolves_to_live_binding(tmp_path: Path) -> None:
    """The dispatch key ``d`` resolves to a live ``dispatch_selected`` Binding.

    The affordance-parity half: the advertised dispatch key must resolve to a
    real binding whose action method exists -- not a dead key. Probes the
    mounted screen's active binding map (the real key->Binding path) and
    confirms the action method is present + callable on the screen.
    """
    state_path = _write_state(tmp_path, _split_state())

    async def body() -> str | None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AutopilotModeScreen)
            entry = screen.active_bindings.get(_DISPATCH_KEY)
            action = entry.binding.action if entry is not None else None
            # The bound action method exists and is callable (a live affordance).
            assert callable(getattr(screen, "action_dispatch_selected", None))
            return action

    assert asyncio.run(body()) == "dispatch_selected"


def test_autopilot_dispatch_key_press_fires_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing ``d`` fires the dispatch action -- the verdict surfaces as a toast.

    The affordance-parity FIRES half: driving the REAL key-press path (not the
    action string) must run ``action_dispatch_selected``, observable as the
    dispatch verdict landing on the toast rack while the result line keeps its
    idle hint. The daemon probe is forced unavailable so no real RPC is issued,
    yet the action still fires and surfaces the honest unavailable verdict --
    proving the key resolves AND the bound method ran.
    """
    state_path = _write_state(tmp_path, _split_state())

    async def body() -> tuple[str, str]:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AutopilotModeScreen)
            from textual.widgets import Static

            before = str(screen.query_one(f"#{DISPATCH_RESULT_ID}", Static).render())
            assert DISPATCH_IDLE in before  # idle before the press
            await pilot.press(_DISPATCH_KEY)  # drive the real key->Binding path
            await settle_screen(pilot)
            after = str(screen.query_one(f"#{DISPATCH_RESULT_ID}", Static).render())
            return after, "\n".join(toast_messages(app))

    after, toasts = asyncio.run(body())
    # The action fired: the honest dispatch verdict (here, daemon-unavailable
    # -- never faked) surfaced as a toast; the result line stays on its idle
    # hint rather than pinning a stale outcome.
    assert DISPATCH_IDLE in after
    assert "dispatch:" in toasts
