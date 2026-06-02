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

import pytest

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
    DISPATCH_IDLE,
    DISPATCH_NO_DAEMON,
    DISPATCH_RESULT_ID,
    EMPTY_NOTICE,
    FRONTIER_ROW_CLASS,
    HALT_NO_DAEMON,
    HALT_NO_TARGET,
    KILL_NO_DAEMON,
    KILL_NO_TARGET,
    AutopilotModeScreen,
    ReadyWaveRow,
    build_frontier_items,
    ready_rows,
    render_frontier_header,
    render_ready_row,
)
from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)

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
    pane mounts exactly those rows, in claim order, surfacing each ready wave id
    while the held / blocked waves (W03 / W04) stay off the list.
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
            # The two ready waves are listed; the held + blocked ones are not.
            assert "P01-I01-W02" in frame
            assert "P01-I02-W05" in frame
            assert "P01-I01-W04" not in frame  # dep still open
            assert EMPTY_NOTICE not in frame
            # The listed order matches compute_ready_frontier's claim order.
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


@pytest.mark.parametrize(
    ("key", "verb", "method"),
    [
        ("S", "skip", "agent.skip"),
        ("space", "pause", "agent.pause"),
        ("a", "arm", "agent.arm"),
    ],
)
def test_autopilot_unwired_keys_surface_not_yet_wired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    verb: str,
    method: str,
) -> None:
    """skip / pause / arm surface the honest "not yet wired" line.

    Their daemon RPCs do not exist, so with a reachable daemon stubbed to answer
    method-not-found the action surfaces that the method is not wired and never
    fakes the intervention.
    """
    state_path = _write_state(tmp_path, _frontier_state())

    class _MethodNotFoundClient:
        def __enter__(self) -> _MethodNotFoundClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, _method: str, _params: dict[str, object]) -> dict[str, object]:
            from eawf.surfaces.cli._daemon_client import DaemonRpcError

            raise DaemonRpcError(code=-32601, message="method not found")

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", lambda *a, **k: _MethodNotFoundClient())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press(key)  # non-destructive -> no confirm modal
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)  # no modal opened
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "not yet wired" in rendered
            assert method in rendered
            assert verb in rendered

    asyncio.run(body())


@pytest.mark.parametrize("key", ["S", "space", "a"])
def test_autopilot_unwired_keys_no_daemon_surface_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    """With no daemon, skip / pause / arm surface the honest unavailable line.

    No fake success: when the daemon socket is unavailable each non-destructive
    intervention reports the request was not issued.
    """
    state_path = _write_state(tmp_path, _frontier_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press(key)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert "unavailable" in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
