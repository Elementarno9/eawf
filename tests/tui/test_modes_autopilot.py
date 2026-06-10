"""Tests for the autopilot ready-wave frontier + dispatch pane (P29-I04-W12).

The Autopilot mode (digit ``2``) renders the **dependency frontier** of the
active scope's wave graph -- the PENDING waves that are claim-ready right now
(every dep CLOSED + no lower-numbered ready sibling under the same iter), in
claim order -- and offers a **dispatch** control that asks the daemon to
live-spawn the selected ready wave via the ``agent.dispatch`` RPC
(``spawn=True``). These tests pin the two halves:

* the pure helpers --
  :func:`~eawf.surfaces.tui.modes.autopilot.build_frontier_items` (the state ->
  frontier-view projection),
  :func:`~eawf.surfaces.tui.modes.autopilot.ready_rows` (title enrichment of
  the computed frontier), and the render helpers -- tested against
  directly-built rows / states so the logic is verified without mounting
  Textual, including that the listed order matches
  :func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier`; and
* the mounted pane under a Pilot: digit ``2`` switches to the mode and the
  breadcrumb trails with the ``Autopilot`` segment; an honest-empty scope (no
  claim-ready wave) renders the "no ready waves" banner; a seeded scope whose
  waves form a ready frontier lists the ready waves in claim order; and the
  dispatch key binding exists + the dispatch action issues an ``agent.dispatch``
  request and surfaces the result honestly.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` (``pilot.pause()``
is CPU-idle-based, not worker-aware) before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from textual.pilot import Pilot

from eawf.kernel.spec.auq_bridge import compute_ready_frontier
from eawf.kernel.state.enums import (
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    CurrentPointers,
    Project,
    State,
    Wave,
)
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.autopilot import (
    ARM_DEFERRED,
    BATCH_NO_DAEMON,
    DISPATCH_IDLE,
    DISPATCH_NO_DAEMON,
    DISPATCH_RESULT_ID,
    EMPTY_NOTICE,
    FRONTIER_ROW_CLASS,
    HALT_NO_DAEMON,
    HALT_NO_TARGET,
    KILL_NO_DAEMON,
    KILL_NO_TARGET,
    MULTI_SELECT_ID,
    MULTI_SELECT_NO_TARGET,
    PAUSE_NO_DAEMON,
    SKIP_NO_NEXT,
    SKIP_NO_TARGET,
    AutopilotModeScreen,
    ReadyWaveRow,
    build_frontier_items,
    ready_rows,
    render_frontier_header,
    render_ready_row,
)
from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal
from eawf.surfaces.tui.screens.overlays.multichoice_checklist import MultichoiceChecklist
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.sigils import chrome

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: The digit key that switches to the Autopilot mode.
_AUTOPILOT_DIGIT = "2"


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


def _ready_row(wave_id: str = "P01-I01-W02", *, iter_id: str = "P01-I01") -> ReadyWaveRow:
    """Build a directly-constructed ready row for the render helpers."""
    return ReadyWaveRow(wave_id=wave_id, iter_id=iter_id, title=f"Wave {wave_id}")


def _state(*, waves: dict[str, Wave] | None = None) -> State:
    """Build a minimal repo state, optionally with a wave graph."""
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
            "waves": (
                {wid: w.model_dump(mode="json") for wid, w in waves.items()}
                if waves is not None
                else {}
            ),
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _frontier_state() -> State:
    """Build a state whose waves form a two-wave ready frontier.

    W01 is CLOSED; W02 + W03 are PENDING with W01 CLOSED as their only dep, so
    both are dep-ready -- but the lower-numbered-sibling gate holds W03 off the
    frontier while W02 (its lower-numbered sibling) is ready. W04 depends on the
    still-PENDING W02, so it is not dep-ready. The ready frontier is therefore
    exactly ``(W02,)`` until W02 closes; a second iter's W05 (deps closed) joins
    it, so the ready frontier is ``(W02, W05)`` in claim order.
    """
    waves = {
        "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED),
        "P01-I01-W02": _wave("P01-I01-W02", status=WaveStatus.PENDING, deps=["P01-I01-W01"]),
        "P01-I01-W03": _wave("P01-I01-W03", status=WaveStatus.PENDING, deps=["P01-I01-W01"]),
        "P01-I01-W04": _wave("P01-I01-W04", status=WaveStatus.PENDING, deps=["P01-I01-W02"]),
        "P01-I02-W05": _wave(
            "P01-I02-W05",
            status=WaveStatus.PENDING,
            deps=["P01-I01-W01"],
            iter_id="P01-I02",
        ),
    }
    return _state(waves=waves)


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


# --------------------------------------------------------------------------
# build_frontier_items -- state -> frontier-view projection (boundary cases)
# --------------------------------------------------------------------------


def test_build_frontier_items_none_state_returns_empty() -> None:
    """An unbound state yields no frontier-view rows (honest-empty path)."""
    assert build_frontier_items(None) == ()


def test_build_frontier_items_no_waves_returns_empty() -> None:
    """A scope with no waves yields no frontier-view rows."""
    assert build_frontier_items(_state()) == ()


def test_build_frontier_items_projects_every_wave_with_deps() -> None:
    """Every wave projects onto a view row carrying its id, iter, status, deps."""
    items = build_frontier_items(_frontier_state())
    assert len(items) == 5
    by_id = {item.wave_id: item for item in items}
    assert by_id["P01-I01-W02"].status is WaveStatus.PENDING
    assert by_id["P01-I01-W02"].deps == ("P01-I01-W01",)
    assert by_id["P01-I02-W05"].iter_id == "P01-I02"


# --------------------------------------------------------------------------
# ready_rows -- title enrichment of the computed frontier (claim order)
# --------------------------------------------------------------------------


def test_ready_rows_empty_frontier_returns_empty() -> None:
    """A frontier with no ready wave yields no display rows."""
    state = _state(waves={"P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED)})
    frontier = compute_ready_frontier(build_frontier_items(state))
    assert ready_rows(frontier, state) == ()


def test_ready_rows_matches_compute_ready_frontier_claim_order() -> None:
    """The display rows mirror ``compute_ready_frontier``'s ready ids in order."""
    state = _frontier_state()
    frontier = compute_ready_frontier(build_frontier_items(state))
    rows = ready_rows(frontier, state)
    # The lower-numbered-sibling gate holds W03; W04's dep is open. Ready = W02,
    # plus the second iter's W05 (its dep closed), in claim (natural-id) order.
    assert tuple(row.wave_id for row in rows) == frontier.ready_ids
    assert tuple(row.wave_id for row in rows) == ("P01-I01-W02", "P01-I02-W05")


def test_ready_rows_enriches_with_wave_title() -> None:
    """Each ready row carries the wave's title read off state."""
    waves = {
        "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED),
        "P01-I01-W02": _wave(
            "P01-I01-W02",
            status=WaveStatus.PENDING,
            deps=["P01-I01-W01"],
            title="Add autopilot frontier",
        ),
    }
    state = _state(waves=waves)
    frontier = compute_ready_frontier(build_frontier_items(state))
    rows = ready_rows(frontier, state)
    assert len(rows) == 1
    assert rows[0].title == "Add autopilot frontier"


# --------------------------------------------------------------------------
# render helpers -- empty banner vs populated rows
# --------------------------------------------------------------------------


def test_render_frontier_header_empty_shows_honest_empty_banner() -> None:
    """The empty header leads with the no-ready-waves banner."""
    body = render_frontier_header(())
    assert EMPTY_NOTICE in body


def test_render_frontier_header_populated_reports_ready_count() -> None:
    """A populated header reports the ready count and omits the empty banner."""
    body = render_frontier_header((_ready_row("P01-I01-W02"), _ready_row("P01-I02-W05")))
    assert "2" in body
    assert EMPTY_NOTICE not in body


def test_render_ready_row_surfaces_wave_id_iter_and_title() -> None:
    """A ready row renders the wave id, its iter, and its title."""
    body = render_ready_row(_ready_row("P01-I01-W02"))
    assert "P01-I01-W02" in body
    assert "P01-I01" in body
    assert "Wave P01-I01-W02" in body


# --------------------------------------------------------------------------
# Mounted pane -- registration, honest-empty, populated frontier, dispatch
# --------------------------------------------------------------------------


def test_autopilot_mode_registers_on_digit_two(tmp_path: Path) -> None:
    """Digit ``2`` switches to the Autopilot mode and trails the breadcrumb.

    Pins the registry wiring: the ModeSpec row registers the mode under
    digit ``2`` (its brief-assigned slot), so the digit key switches to an
    :class:`AutopilotModeScreen` and the header breadcrumb trails with the
    ``Autopilot`` segment derived from the registry title (the breadcrumb is
    ``scope > code > phase > iter > mode``, so the mode trails).
    """
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)  # -> autopilot
            await settle_screen(pilot)
            assert isinstance(app.screen, AutopilotModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            header_row = frame.splitlines()[0]
            assert "Autopilot" in header_row

    asyncio.run(body())


def test_autopilot_pane_renders_honest_empty(tmp_path: Path) -> None:
    """A scope with no claim-ready wave renders the honest-empty banner.

    The load-bearing honesty assertion: a scope whose frontier is empty must
    show "no ready waves" rather than an empty list that reads as primed.
    """
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)  # -> autopilot
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            assert not pane.query(f".{FRONTIER_ROW_CLASS}")
            frame = normalize_snapshot(capture_screen_text(app))
            assert EMPTY_NOTICE in frame

    asyncio.run(body())


def test_autopilot_pane_lists_ready_frontier_in_claim_order(tmp_path: Path) -> None:
    """The mounted pane lists the ready waves in claim order, matching the compute.

    Seeds a wave graph whose ready frontier is ``(W02, W05)`` and asserts the
    pane mounts exactly those ready rows (CSS class :data:`FRONTIER_ROW_CLASS`),
    in claim order. The held / blocked waves (W03 / W04) are NOT ready rows --
    the reskin surfaces them in the separate blocked band (see
    :func:`test_autopilot_pane_renders_ready_blocked_split`), so they never
    appear as a dispatch-target row.
    """
    state = _frontier_state()
    state_path = _write_state(tmp_path, state)
    expected = compute_ready_frontier(build_frontier_items(state)).ready_ids

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)  # -> autopilot
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            rows = pane.query(f".{FRONTIER_ROW_CLASS}")
            assert len(rows) == len(expected) == 2
            frame = normalize_snapshot(capture_screen_text(app))
            # The two ready waves are listed in the ready band.
            assert "P01-I01-W02" in frame
            assert "P01-I02-W05" in frame
            assert EMPTY_NOTICE not in frame
            # W04 is held (dep open), so it is NOT a ready (dispatch-target) row;
            # the reskin shows it in the blocked band instead.
            row_text = " ".join(str(row.render()) for row in rows)  # type: ignore[attr-defined]
            assert "P01-I01-W04" not in row_text
            # The listed ready order matches compute_ready_frontier's claim order.
            row_order = [str(row.render()) for row in rows]  # type: ignore[attr-defined]
            assert "P01-I01-W02" in row_order[0]
            assert "P01-I02-W05" in row_order[1]

    asyncio.run(body())


def test_autopilot_dispatch_binding_exists() -> None:
    """The Autopilot pane binds ``d`` to the dispatch action and arrows to select."""
    keys = {
        binding.key: binding.action
        for binding in AutopilotModeScreen.BINDINGS
        if hasattr(binding, "key")
    }
    assert keys.get("d") == "dispatch_selected"
    assert keys.get("up") == "select_prev"
    assert keys.get("down") == "select_next"


def test_autopilot_dispatch_action_no_daemon_surfaces_honest_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no reachable daemon the dispatch action reports the request was not issued.

    The dispatch action must never fake a spawn: when the daemon socket is
    unavailable it surfaces the honest "daemon unavailable" line rather than a
    success.
    """
    state = _frontier_state()
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        # Force the daemon probe to report unavailable so no real RPC is made.
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            await pilot.press("d")  # dispatch
            await settle_screen(pilot)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert DISPATCH_NO_DAEMON in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_autopilot_dispatch_action_issues_request_and_surfaces_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch action issues an ``agent.dispatch`` (spawn) request + surfaces it.

    Drives the dispatch path with a reachable daemon stubbed by a fake client
    that returns a live-spawn plan. The action must reach the daemon with the
    selected ready wave id + ``spawn=True`` and surface the captured pid +
    runtime honestly (no faked dispatch).
    """
    state = _frontier_state()
    state_path = _write_state(tmp_path, state)
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            # Mirror the daemon's live-spawn DispatchPlan shape (subset).
            return {"runtime": "claude-code", "pid": 4321, "session_id": "S-1", "attempt": 1}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _FakeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            await pilot.press("d")  # dispatch the selected (first) ready wave
            await settle_screen(pilot)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "spawned" in rendered
            assert "4321" in rendered  # captured pid surfaced
            assert DISPATCH_IDLE not in rendered

    asyncio.run(body())
    # The dispatch action reached the daemon with the first ready wave + spawn.
    assert calls and calls[0][0] == "agent.dispatch"
    assert calls[0][1]["wave_id"] == "P01-I01-W02"
    assert calls[0][1]["spawn"] is True


def test_autopilot_dispatch_selects_with_arrows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arrow-down moves the dispatch target to the next ready wave.

    Selecting the second ready wave with ``down`` then dispatching must reach
    the daemon with that wave's id, proving the arrows drive the dispatch
    target and not just a cosmetic highlight.
    """
    state = _frontier_state()
    state_path = _write_state(tmp_path, state)
    calls: list[dict[str, object]] = []

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, _method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append(params)
            return {"runtime": "claude-code", "pid": 9, "session_id": "S-2", "attempt": 1}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _FakeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("down")  # select the second ready wave (W05)
            await settle_screen(pilot)
            await pilot.press("d")  # dispatch it
            await settle_screen(pilot)

    asyncio.run(body())
    assert calls and calls[0]["wave_id"] == "P01-I02-W05"


def test_autopilot_pane_keeps_chassis_brand(tmp_path: Path) -> None:
    """Even honest-empty, the Autopilot pane keeps the shared chassis brand row."""
    from eawf.surfaces.tui.widgets.header import BRAND

    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)  # -> autopilot
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            header_row = frame.splitlines()[0]
            assert BRAND in header_row
            assert "Autopilot" in header_row

    asyncio.run(body())


# --------------------------------------------------------------------------
# Intervention keys -- bindings, confirm gating, honest-unavailable lines
# --------------------------------------------------------------------------


class _RecordingClient:
    """Fake :class:`DaemonClient` that records its calls + returns a canned dict.

    Mirrors the daemon's placeholder ``agent.kill`` response (``killed=false``)
    so the kill / halt path surfaces the honest not-killed verdict, and records
    every ``(method, params)`` pair so a test can assert the wire shape.
    """

    #: Shared call log -- one row per ``call`` across all instances of a test's
    #: client (the seam re-instantiates the client per RPC).
    calls: list[tuple[str, dict[str, object]]]

    def __init__(self, *_a: object, **_k: object) -> None:
        return None

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        type(self).calls.append((method, params))
        return {"killed": False, "signal": params.get("signal", "term")}


def _make_recording_client(sink: list[tuple[str, dict[str, object]]]) -> type[_RecordingClient]:
    """Build a recording-client class whose ``calls`` log is *sink*."""
    return type("_BoundRecordingClient", (_RecordingClient,), {"calls": sink})


def test_autopilot_intervention_bindings_exist() -> None:
    """The Autopilot pane binds H / S / K / space / a to their actions."""
    keys = {
        binding.key: binding.action
        for binding in AutopilotModeScreen.BINDINGS
        if hasattr(binding, "key")
    }
    assert keys.get("H") == "halt_selected"
    assert keys.get("S") == "skip_selected"
    assert keys.get("K") == "kill_selected"
    assert keys.get("space") == "toggle_pause"
    assert keys.get("a") == "arm_flow"
    # The dispatch + selection bindings stay unchanged.
    assert keys.get("d") == "dispatch_selected"
    assert keys.get("up") == "select_prev"
    assert keys.get("down") == "select_next"


def test_autopilot_intervention_keys_in_footer_hints() -> None:
    """The intervention keys are advertised in the footer hints (discoverable)."""
    hints = " ".join(AutopilotModeScreen.FOOTER_HINTS)
    assert "H halt" in hints
    assert "S skip" in hints
    assert "K kill" in hints
    assert "space pause" in hints
    assert "a arm" in hints


def test_autopilot_footer_hints_drop_mode_digit_hint() -> None:
    """The redundant ``1-9 mode`` hint is gone (the footer mode row replaces it).

    The always-visible footer mode row (row 2) lists every mode with its
    digit, so the Autopilot pane no longer advertises the mode-switch digits
    in its own hint strip.
    """
    hints = " ".join(AutopilotModeScreen.FOOTER_HINTS)
    assert "1-9 mode" not in hints
    assert "mode" not in hints


def test_autopilot_kill_pushes_confirm_modal(tmp_path: Path) -> None:
    """Pressing ``K`` opens a ConfirmModal naming the SIGKILL stop (gated)."""
    state_path = _write_state(tmp_path, _frontier_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("K")  # destructive -> confirm modal
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)

    asyncio.run(body())


def test_autopilot_halt_pushes_confirm_modal(tmp_path: Path) -> None:
    """Pressing ``H`` opens a ConfirmModal naming the graceful stop (gated)."""
    state_path = _write_state(tmp_path, _frontier_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("H")  # destructive -> confirm modal
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)

    asyncio.run(body())


def test_autopilot_kill_dismissed_issues_no_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the kill confirm (``Esc``) issues no ``agent.kill`` RPC.

    The destructive gate must not fire the kill when the operator backs out:
    pressing ``Esc`` on the confirm modal dismisses it as ``No``, so the
    daemon is never reached.
    """
    state_path = _write_state(tmp_path, _frontier_state())
    calls: list[tuple[str, dict[str, object]]] = []

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _make_recording_client(calls))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("K")
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("escape")  # cancel == No
            await settle_screen(pilot)

    asyncio.run(body())
    assert calls == []  # the cancelled kill never reached the daemon


def test_autopilot_kill_confirmed_issues_kill_rpc_with_kill_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirming the kill issues ``agent.kill`` with a SIGKILL-class signal.

    The action reaches the daemon with the selected ready wave's id + attempt
    and the SIGKILL-class signal, and surfaces the daemon's honest (placeholder)
    not-killed verdict rather than faking success.
    """
    state_path = _write_state(tmp_path, _frontier_state())
    calls: list[tuple[str, dict[str, object]]] = []

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _make_recording_client(calls))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("K")
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("right")  # highlight Yes
            await pilot.press("enter")  # confirm
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "not killed" in rendered  # honest placeholder verdict
            assert "kill" in rendered

    asyncio.run(body())
    # The confirmed kill reached the daemon with the first ready wave + SIGKILL.
    assert calls and calls[0][0] == "agent.kill"
    assert calls[0][1]["wave_id"] == "P01-I01-W02"
    assert calls[0][1]["attempt"] == 1
    assert calls[0][1]["signal"] == "kill"


def test_autopilot_halt_confirmed_issues_kill_rpc_with_term_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirming the halt issues ``agent.kill`` with the graceful SIGTERM signal.

    Halt is the soft entry to the same daemon-owned ladder, so it routes through
    ``agent.kill`` with the ``term`` signal (not a separate RPC).
    """
    state_path = _write_state(tmp_path, _frontier_state())
    calls: list[tuple[str, dict[str, object]]] = []

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _make_recording_client(calls))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("H")
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("right")  # highlight Yes
            await pilot.press("enter")  # confirm
            await settle_screen(pilot)

    asyncio.run(body())
    assert calls and calls[0][0] == "agent.kill"
    assert calls[0][1]["wave_id"] == "P01-I01-W02"
    assert calls[0][1]["signal"] == "term"


def test_autopilot_kill_no_daemon_surfaces_honest_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no daemon the confirmed kill surfaces the honest unavailable line.

    The kill must never fake a stop: with the daemon socket unavailable the
    confirmed kill reports the request was not issued.
    """
    state_path = _write_state(tmp_path, _frontier_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("K")
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("right")
            await pilot.press("enter")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert KILL_NO_DAEMON in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_autopilot_halt_no_daemon_surfaces_honest_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no daemon the confirmed halt surfaces the honest unavailable line."""
    state_path = _write_state(tmp_path, _frontier_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("H")
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("right")
            await pilot.press("enter")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert HALT_NO_DAEMON in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_autopilot_kill_no_target_surfaces_honest_line(tmp_path: Path) -> None:
    """With no ready wave, ``K`` surfaces the honest nothing-to-kill line.

    An honest-empty frontier has no selected wave, so the kill must report there
    is nothing to kill and never open the confirm modal.
    """
    state_path = _write_state(tmp_path, _state())  # empty frontier

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("K")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)  # no modal opened
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert KILL_NO_TARGET in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_autopilot_halt_no_target_surfaces_honest_line(tmp_path: Path) -> None:
    """With no ready wave, ``H`` surfaces the honest nothing-to-halt line."""
    state_path = _write_state(tmp_path, _state())  # empty frontier

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("H")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)  # no modal opened
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert HALT_NO_TARGET in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_autopilot_skip_helpers_removed_no_stub_routing() -> None:
    """No autopilot control routes through a not-yet-wired stub helper.

    The load-bearing honesty assertion for this wave: the ``_issue_unwired`` /
    ``_call_unwired`` stub helpers (which fired a doomed RPC and dressed up the
    method-not-found as "not yet wired") are gone, so neither skip nor arm can
    route through them. Pins the deletion so a future re-introduction of a
    faked-stub control fails this test.
    """
    assert not hasattr(AutopilotModeScreen, "_issue_unwired")
    assert not hasattr(AutopilotModeScreen, "_call_unwired")


def test_autopilot_skip_advances_selection_to_next_ready_wave(tmp_path: Path) -> None:
    """``S`` (skip) advances the selection past the current ready wave (local).

    Skip is a real, cheap local frontier operation -- no daemon round-trip. From
    the first ready wave (W02) it steps the selection to the next one (W05) in
    claim order and surfaces where it landed, never implying a faked skip.
    """
    state_path = _write_state(tmp_path, _frontier_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            assert pane.selected == 0  # starts on the first ready wave (W02)
            await pilot.press("S")  # skip -> advance to the next ready wave
            await settle_screen(pilot)
            assert pane.selected == 1  # the selection genuinely moved (real effect)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "skip: now on" in rendered
            assert "P01-I02-W05" in rendered  # the wave it stepped to

    asyncio.run(body())


def test_autopilot_skip_no_next_surfaces_honest_line(tmp_path: Path) -> None:
    """``S`` on the last ready wave reports nothing further to skip to (no fake).

    Stepping past the final ready wave has nothing to land on, so the cursor
    stays put and the result honestly says there is no further ready wave rather
    than implying a skip happened.
    """
    state_path = _write_state(tmp_path, _frontier_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            await pilot.press("down")  # select the last ready wave (W05)
            await settle_screen(pilot)
            assert pane.selected == 1
            await pilot.press("S")  # nothing further to skip to
            await settle_screen(pilot)
            assert pane.selected == 1  # cursor unmoved (honest no-op)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert SKIP_NO_NEXT in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_autopilot_skip_no_target_surfaces_honest_line(tmp_path: Path) -> None:
    """``S`` on an empty frontier reports there is no ready wave to skip.

    An honest-empty frontier has no selected wave, so skip must report nothing
    to skip rather than implying a stepped cursor.
    """
    state_path = _write_state(tmp_path, _state())  # empty frontier

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("S")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert SKIP_NO_TARGET in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_autopilot_arm_surfaces_deferred_not_faked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``a`` (arm) surfaces the honest deferred line and fires no RPC.

    Arm has no cheap real semantics, so it must report the capability is
    deferred -- NOT a "not yet wired daemon RPC" line and NOT a faked arm. The
    daemon-client seam is stubbed to record any call; arm must never reach it.
    """
    state_path = _write_state(tmp_path, _frontier_state())
    calls: list[tuple[str, dict[str, object]]] = []

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _make_recording_client(calls))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("a")  # arm -> deferred (no RPC)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)  # no modal opened
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert ARM_DEFERRED in rendered
            assert "deferred" in rendered
            assert "not yet wired" not in rendered  # not the old faked-stub line

    asyncio.run(body())
    assert calls == []  # arm never reached the daemon (no doomed RPC)


# --------------------------------------------------------------------------
# space (pause / resume) -- the real agent.pause / agent.resume wiring (W05)
# --------------------------------------------------------------------------


def _paused_frontier_state() -> State:
    """Build the two-wave frontier state with ``dispatch_paused`` already set."""
    state = _frontier_state()
    state.dispatch_paused = True
    return state


def test_autopilot_pause_issues_pause_rpc_when_not_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``space`` on a running scope issues ``agent.pause`` and surfaces ``paused``.

    The bound state reads ``dispatch_paused=False``, so the toggle issues the
    real ``agent.pause`` RPC and surfaces the persisted paused verdict (never a
    "not yet wired" line).
    """
    state_path = _write_state(tmp_path, _frontier_state())  # dispatch_paused defaults False
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            return {"paused": True}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _FakeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("space")  # pause
            await settle_screen(pilot)
            await app.workers.wait_for_complete()
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "pause: paused" in rendered
            assert "not yet wired" not in rendered

    asyncio.run(body())
    # The toggle reached the daemon with the real pause RPC + empty params.
    assert calls and calls[0] == ("agent.pause", {})


def test_autopilot_pause_issues_resume_rpc_when_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``space`` on a paused scope issues ``agent.resume`` and surfaces ``resumed``.

    The bound state reads ``dispatch_paused=True``, so the toggle issues the
    real ``agent.resume`` RPC and surfaces the persisted resumed verdict.
    """
    state_path = _write_state(tmp_path, _paused_frontier_state())
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            return {"paused": False}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _FakeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("space")  # resume (already paused)
            await settle_screen(pilot)
            await app.workers.wait_for_complete()
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "pause: resumed" in rendered

    asyncio.run(body())
    assert calls and calls[0] == ("agent.resume", {})


def test_autopilot_pause_no_daemon_surfaces_honest_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no daemon the pause toggle surfaces the honest unavailable line.

    The toggle must never fake a pause: with the daemon socket unavailable it
    reports the request was not issued.
    """
    state_path = _write_state(tmp_path, _frontier_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("space")
            await settle_screen(pilot)
            await app.workers.wait_for_complete()
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert PAUSE_NO_DAEMON in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


# --------------------------------------------------------------------------
# m (multi-select wave-claim shell) -- the [X] toggle over the ready frontier
# --------------------------------------------------------------------------


def _three_ready_state() -> State:
    """Build a state whose frontier has THREE simultaneously-ready waves.

    W01 is CLOSED; W02 / W03 / W04 are each PENDING in their OWN iter with W01
    CLOSED as their only dep, so no lower-numbered-sibling gate holds any of
    them -- all three are ready at once. W05 depends on the missing W99 (an
    unresolved dep), so it is NOT dep-ready and stays off the frontier. The
    ready frontier is therefore ``(W02, W03, W04)`` in claim order, and the
    blocked band carries W05.
    """
    waves = {
        "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED),
        "P01-I01-W02": _wave("P01-I01-W02", status=WaveStatus.PENDING, deps=["P01-I01-W01"]),
        "P01-I02-W03": _wave(
            "P01-I02-W03",
            status=WaveStatus.PENDING,
            deps=["P01-I01-W01"],
            iter_id="P01-I02",
        ),
        "P01-I03-W04": _wave(
            "P01-I03-W04",
            status=WaveStatus.PENDING,
            deps=["P01-I01-W01"],
            iter_id="P01-I03",
        ),
        "P01-I04-W05": _wave(
            "P01-I04-W05",
            status=WaveStatus.PENDING,
            deps=["P01-I04-W99"],  # unresolved dep -> blocked, never selectable
            iter_id="P01-I04",
        ),
    }
    return _state(waves=waves)


def test_autopilot_multi_select_binding_exists() -> None:
    """The Autopilot pane binds ``m`` to the multi-select shell action."""
    keys = {
        binding.key: binding.action
        for binding in AutopilotModeScreen.BINDINGS
        if hasattr(binding, "key")
    }
    assert keys.get("m") == "open_multi_select"
    # The live pause binding (space) is untouched -- multi-select rides on m.
    assert keys.get("space") == "toggle_pause"


def test_autopilot_multi_select_space_twice_checks_exactly_two_rows(tmp_path: Path) -> None:
    """``m`` then Space x2 selects exactly two ready frontier waves shown ``[X]``.

    The load-bearing success criterion: with three ready frontier waves the
    multi-select shell lists all three as selectable choices; pressing Space on
    two distinct rows (moving the checklist cursor between them) leaves exactly
    two checked, rendered with the filled ``check_on`` (``[X]``) mark and the
    third still hollow. The selection model (``selected_items``) and the
    rendered marks agree.
    """
    state = _three_ready_state()
    state_path = _write_state(tmp_path, state)
    expected = compute_ready_frontier(build_frontier_items(state)).ready_ids
    assert expected == ("P01-I01-W02", "P01-I02-W03", "P01-I03-W04")

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)  # -> autopilot
            await settle_screen(pilot)
            await pilot.press("m")  # open the multi-select shell
            await settle_screen(pilot)
            await app.workers.wait_for_complete()
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            checklist = pane.query_one(f"#{MULTI_SELECT_ID}", MultichoiceChecklist)
            # All three ready frontier waves are selectable choices.
            assert checklist._choices == expected
            checklist.focus()
            await pilot.press("space")  # check the first ready wave (W02)
            await pilot.press("down")  # move cursor to W03
            await pilot.press("space")  # check the second ready wave (W03)
            await settle_screen(pilot)
            await app.workers.wait_for_complete()
            # Exactly two waves are selected, in declaration (claim) order.
            assert checklist.selected_items() == ["P01-I01-W02", "P01-I02-W03"]
            # The rendered checklist shows exactly two filled [X] marks.
            mode = getattr(app, "render_mode", "unicode")
            check_on = chrome("check_on", mode=mode)
            rendered = str(checklist.render())
            assert rendered.count(check_on) == 2

    asyncio.run(body())


def test_autopilot_multi_select_excludes_non_ready_waves(tmp_path: Path) -> None:
    """A non-ready (deps-not-CLOSED) wave is never a selectable choice.

    The shell single-sources its choices from ``compute_ready_frontier``, so
    the blocked W05 (its dep unresolved) is absent from the checklist choices --
    it can never be toggled into the claim batch.
    """
    state = _three_ready_state()
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("m")
            await settle_screen(pilot)
            await app.workers.wait_for_complete()
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            checklist = pane.query_one(f"#{MULTI_SELECT_ID}", MultichoiceChecklist)
            # The blocked wave is not a selectable choice (single-sourced frontier).
            assert "P01-I04-W05" not in checklist._choices
            assert set(checklist._choices) == {
                "P01-I01-W02",
                "P01-I02-W03",
                "P01-I03-W04",
            }

    asyncio.run(body())


def test_autopilot_multi_select_commit_stages_batch_and_tears_down(tmp_path: Path) -> None:
    """Committing the checklist (``Enter``) stages the batch and tears it down.

    After checking two waves and pressing ``Enter``, the shell records the
    staged claim batch (single-sourced from the ready frontier choices) and
    removes the checklist. The bare test harness has no reachable daemon, so the
    commit surfaces the honest :data:`BATCH_NO_DAEMON` line (the W07 dispatch
    wiring); the positive-dispatch path is covered by the fake-client tests
    below.
    """
    state = _three_ready_state()
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("m")
            await settle_screen(pilot)
            await app.workers.wait_for_complete()
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            checklist = pane.query_one(f"#{MULTI_SELECT_ID}", MultichoiceChecklist)
            checklist.focus()
            await pilot.press("space")  # W02
            await pilot.press("down", "down")  # cursor -> W04
            await pilot.press("space")  # W04
            await pilot.press("enter")  # commit the batch
            await settle_screen(pilot)
            await app.workers.wait_for_complete()
            # The checklist tore down on commit + the batch was staged.
            assert not pane.query(f"#{MULTI_SELECT_ID}")
            assert pane._claim_batch == ("P01-I01-W02", "P01-I03-W04")
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert BATCH_NO_DAEMON in rendered

    asyncio.run(body())


def test_autopilot_multi_select_empty_frontier_surfaces_no_target(tmp_path: Path) -> None:
    """``m`` on an empty frontier surfaces the honest no-target line (no overlay).

    An honest-empty frontier has no ready wave to select, so the shell must
    report there is nothing to select rather than mounting an empty checklist
    that reads as a primed batch.
    """
    state_path = _write_state(tmp_path, _state())  # empty frontier

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("m")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            assert not pane.query(f"#{MULTI_SELECT_ID}")  # no checklist mounted
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert MULTI_SELECT_NO_TARGET in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_autopilot_multi_select_cancel_tears_down_without_staging(tmp_path: Path) -> None:
    """``Esc`` in the checklist aborts the batch without staging (no commit).

    Cancelling the multi-select shell tears the checklist down and leaves the
    staged claim batch empty -- a toggled-but-cancelled selection never stages.
    """
    state_path = _write_state(tmp_path, _three_ready_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("m")
            await settle_screen(pilot)
            await app.workers.wait_for_complete()
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            checklist = pane.query_one(f"#{MULTI_SELECT_ID}", MultichoiceChecklist)
            checklist.focus()
            await pilot.press("space")  # check W02
            await pilot.press("escape")  # abort
            await settle_screen(pilot)
            await app.workers.wait_for_complete()
            assert not pane.query(f"#{MULTI_SELECT_ID}")  # torn down
            assert pane._claim_batch == ()

    asyncio.run(body())


# --------------------------------------------------------------------------
# W07 -- multi-select batch dispatch to agent.dispatch (spawn=True) on a worker
# --------------------------------------------------------------------------


async def _commit_two_wave_batch(app: EaApp, pilot: Pilot[None]) -> AutopilotModeScreen:
    """Open the shell, check W02 + W04, commit the batch, and return the pane.

    Drives the ``m`` -> Space (W02) -> down,down -> Space (W04) -> Enter path
    over a :func:`_three_ready_state` frontier so the staged claim batch is
    ``(P01-I01-W02, P01-I03-W04)`` -- the shared setup the W07 dispatch tests
    reuse before draining the worker and asserting the RPC calls.
    """
    settle = cast("Pilot[object]", pilot)
    await settle_screen(settle)
    await pilot.press(_AUTOPILOT_DIGIT)
    await settle_screen(settle)
    await pilot.press("m")
    await settle_screen(settle)
    await app.workers.wait_for_complete()
    pane = app.screen
    assert isinstance(pane, AutopilotModeScreen)
    checklist = pane.query_one(f"#{MULTI_SELECT_ID}", MultichoiceChecklist)
    checklist.focus()
    await pilot.press("space")  # W02
    await pilot.press("down", "down")  # cursor -> W04
    await pilot.press("space")  # W04
    await pilot.press("enter")  # commit the batch
    await settle_screen(settle)
    assert pane._claim_batch == ("P01-I01-W02", "P01-I03-W04")
    return pane


def test_autopilot_batch_dispatch_calls_agent_dispatch_once_per_wave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committing a 2-wave batch issues ``agent.dispatch`` (spawn) once per wave.

    The load-bearing W07 success criterion: with a reachable daemon stubbed by a
    fake client, committing two checked waves reaches the daemon with exactly two
    ``agent.dispatch`` calls (one per selected wave), each carrying that wave's
    id + ``spawn=True``, and the calls run on a Textual worker (so the test
    drains ``app.workers`` before asserting).
    """
    state_path = _write_state(tmp_path, _three_ready_state())
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            return {"runtime": "claude-code", "pid": 1234, "session_id": "S", "attempt": 1}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _FakeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            pane = await _commit_two_wave_batch(app, pilot)
            # The dispatch RPCs run on a worker -- drain before asserting.
            await app.workers.wait_for_complete()
            await settle_screen(pilot)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "P01-I01-W02" in rendered
            assert "P01-I03-W04" in rendered

    asyncio.run(body())
    # Exactly one agent.dispatch (spawn) call per selected wave, in claim order.
    assert [method for method, _ in calls] == ["agent.dispatch", "agent.dispatch"]
    assert [params["wave_id"] for _, params in calls] == ["P01-I01-W02", "P01-I03-W04"]
    assert all(params["spawn"] is True for _, params in calls)


def test_autopilot_batch_dispatch_no_daemon_issues_zero_rpcs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed batch with no reachable daemon issues ZERO RPCs, honestly.

    With the daemon probe forced unavailable the fleet must NOT open a client or
    call ``agent.dispatch`` at all (reachability is checked once up front); the
    result line surfaces the exact :data:`BATCH_NO_DAEMON` "not issued" phrasing.
    """
    state_path = _write_state(tmp_path, _three_ready_state())
    calls: list[tuple[str, dict[str, object]]] = []

    class _ExplodingClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            raise AssertionError("DaemonClient must not be constructed when daemon is unavailable")

        def __enter__(self) -> _ExplodingClient:  # pragma: no cover - never reached
            return self

        def __exit__(self, *_args: object) -> None:  # pragma: no cover - never reached
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))  # pragma: no cover - never reached
            return {}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        monkeypatch.setattr(dc, "DaemonClient", _ExplodingClient)
        async with app.run_test(size=(120, 40)) as pilot:
            pane = await _commit_two_wave_batch(app, pilot)
            await app.workers.wait_for_complete()
            await settle_screen(pilot)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert BATCH_NO_DAEMON in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
    # Zero RPCs were issued (no client was even constructed).
    assert calls == []


def test_autopilot_batch_dispatch_one_rejected_others_proceed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-batch rejected wave reads ``rejected`` while the others dispatch.

    The daemon rejects the first staged wave (``-32602 invalid_params``) and
    accepts the second. The fleet must NOT abort on the rejection: both waves are
    issued an ``agent.dispatch`` call, the rejected wave's outcome reads
    ``rejected`` on the result line, and the accepted wave reads ``spawned``.
    """
    state_path = _write_state(tmp_path, _three_ready_state())
    calls: list[dict[str, object]] = []

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        class _RejectingClient:
            def __init__(self, *_a: object, **_k: object) -> None:
                return None

            def __enter__(self) -> _RejectingClient:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def call(self, _method: str, params: dict[str, object]) -> dict[str, object]:
                calls.append(params)
                if params["wave_id"] == "P01-I01-W02":
                    raise dc.DaemonRpcError(-32602, "invalid_params")
                return {"runtime": "claude-code", "pid": 9, "session_id": "S", "attempt": 1}

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _RejectingClient)
        async with app.run_test(size=(120, 40)) as pilot:
            pane = await _commit_two_wave_batch(app, pilot)
            await app.workers.wait_for_complete()
            await settle_screen(pilot)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            # The rejected wave reads rejected; the accepted wave reads spawned.
            assert "P01-I01-W02 rejected" in rendered
            assert "P01-I03-W04 spawned" in rendered

    asyncio.run(body())
    # Both waves were still dispatched -- one rejection never aborts the fleet.
    assert [params["wave_id"] for params in calls] == ["P01-I01-W02", "P01-I03-W04"]
