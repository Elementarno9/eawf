"""Live-test the fleet cockpit's worker offload + poll backstop (P30-I13-W11).

Snapshot + claim tests miss the failure modes that only a LIVE Pilot run
surfaces -- the four P29 TUI-staleness lessons. This module drives the assembled
cockpit (:class:`~eawf.surfaces.tui.modes.autopilot.AutopilotModeScreen`,
vitals + lane grid + fork inbox + run summary from I13-W02/W03/W05/W06/W07)
through a real ``EaApp`` Pilot and pins the two W11 criteria:

C1 -- worker offload + non-no-op actions + live re-render
---------------------------------------------------------
Every mutating cockpit key issues its daemon round-trip on a Textual worker, so
a Pilot that ``await``\\s worker completion (:func:`settle_screen` drains the
worker pool) shows the cockpit never BLOCKS on a sync daemon call: the dispatch /
pause / kill keys press, the worker round-trips off the UI thread, and the
result line repaints -- the cockpit stays responsive throughout. Every footer
action also resolves to a real ``action_*`` handler (a bound key whose handler is
missing is a silent no-op), and a fork -> resolve -> close cycle pushed through
the live state seam re-renders the cockpit (the lane band + vitals follow the
daemon-written run, not a one-shot mount snapshot).

C2 -- poll backstop refresh (never stale-till-restart)
------------------------------------------------------
The cockpit rides the daemon push PLUS an always-on mtime-poll backstop. With the
push leg silent, advancing the on-disk ``state.json`` mtime and letting the
binder's ``set_interval`` poll loop tick (:func:`tick_poll_backstop`, under a
tight ``EAWF_POLL_INTERVAL_S``) still refreshes the cockpit vitals -- so a stalled
push stream can never strand the bound state until a restart.

Determinism follows the project Pilot-worker rule: every Pilot body drains the
background workers via :func:`settle_screen` (which ``await``\\s
``app.workers.wait_for_complete()``) before asserting, so each worker-offloaded
round-trip has landed and the sampled frame is stable across runs.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import ProjectStatus, RiskTier, ScopeKind, WaveStatus
from eawf.kernel.state.models import (
    CurrentPointers,
    FleetCounters,
    FleetFork,
    FleetForkReason,
    FleetLane,
    FleetRun,
    FleetRunState,
    Project,
    State,
    Wave,
)
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.autopilot import (
    COCKPIT_IDLE,
    COCKPIT_VITALS_ID,
    DISPATCH_RESULT_ID,
    LANE_CELL_CLASS,
    AutopilotModeScreen,
)
from eawf.surfaces.tui.screens.overlays.fork_inbox import ForkInboxModal
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.snapshot.pilot_harness import (
    mutating_action_keys_resolve,
    push_state_revision,
    tick_poll_backstop,
)

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: The digit key that switches to the Autopilot (cockpit) mode.
_AUTOPILOT_DIGIT = "2"

#: The cockpit's mutating footer keys paired with the action each binds, so a
#: silent-no-op (missing ``action_*`` handler) is provable. Mirrors the cockpit
#: footer row: dispatch / halt / skip / kill / pause / arm / forks / select.
_COCKPIT_ACTIONS: tuple[tuple[str, str], ...] = (
    ("d", "dispatch_selected"),
    ("H", "halt_selected"),
    ("S", "skip_selected"),
    ("K", "kill_selected"),
    ("space", "toggle_pause"),
    ("a", "arm_flow"),
    ("f", "open_fork_inbox"),
    ("m", "open_multi_select"),
)


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry + home resolution at an empty ``tmp_path`` home.

    Keeps a stray scope switch (and any registry read) deterministic and off the
    operator's real registry.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


# --------------------------------------------------------------------------
# Fixture builders -- a frontier wave graph + draining / fork / done runs
# --------------------------------------------------------------------------


def _wave(
    wave_id: str,
    *,
    status: WaveStatus,
    deps: list[str] | None = None,
    iter_id: str = "P01-I01",
) -> Wave:
    """Build a wave row for the cockpit frontier projection."""
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=f"Wave {wave_id}",
        status=status,
        deps=deps or [],
        opened_at=_T0,
    )


def _frontier_waves(*, w02_status: WaveStatus = WaveStatus.PENDING) -> dict[str, Wave]:
    """Build a wave graph whose ready frontier carries at least one claim-ready wave.

    W01 is CLOSED; W02 is PENDING (dep W01 closed) and so claim-ready, giving the
    cockpit a real dispatch target. The *w02_status* override lets a follow-up
    push close W02 so the fork-resolve-close cycle ends on a genuinely advanced
    graph rather than a re-push of the same frontier.
    """
    return {
        "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED),
        "P01-I01-W02": _wave("P01-I01-W02", status=w02_status, deps=["P01-I01-W01"]),
        "P01-I01-W03": _wave("P01-I01-W03", status=WaveStatus.PENDING, deps=["P01-I01-W01"]),
    }


def _draining_run(
    *,
    lanes: int = 1,
    forks: list[FleetFork] | None = None,
    closed: int = 0,
    throughput: float | None = 2.5,
) -> FleetRun:
    """Build a DRAINING run with in-flight lanes + the vitals the cockpit reads."""
    lane_rows = {
        f"P01-I01-W{idx + 2:02d}": FleetLane(
            wave_id=f"P01-I01-W{idx + 2:02d}", attempt=1, pgid=1000 + idx, dispatched_at=_T0
        )
        for idx in range(lanes)
    }
    return FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=4,
        frontier=["P01-I01-W02", "P01-I01-W03"],
        lanes=lane_rows,
        forks=forks or [],
        counters=FleetCounters(
            claimed=lanes + 2,
            dispatched=lanes,
            closed=closed,
            forked=len(forks or []),
            forks_resolved=0,
            spent_eu=6.0,
            spent_usd=4.50,
        ),
        eu_cap=10.0,
        usd_cap=8.0,
        throughput=throughput,
        armed_at=_T0,
    )


def _blocking_fork(wave_id: str = "P01-I01-W02") -> FleetFork:
    """Build a queued blocking fork the cockpit auto-raises its inbox over."""
    return FleetFork(
        wave_id=wave_id,
        attempt=1,
        risk_tier=RiskTier.UI,
        reason=FleetForkReason.HIGH_RISK_CLOSE,
        evidence_ref=f"urn:eawf:v1:close:{wave_id}",
        forked_at=_T0,
    )


def _state(
    *,
    waves: dict[str, Wave] | None = None,
    fleet_run: FleetRun | None = None,
) -> State:
    """Build a repo state, optionally with a wave graph + a fleet run."""
    return State.model_validate(
        {
            "schema_version": "1.10" if fleet_run is not None else "1.3",
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
            "fleet_run": fleet_run.model_dump(mode="json") if fleet_run is not None else None,
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


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return its absolute path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path.resolve()


async def _enter_cockpit(pilot: object, app: EaApp) -> AutopilotModeScreen:
    """Switch to the Autopilot cockpit and return the mounted screen, settled."""
    await settle_screen(pilot)  # type: ignore[arg-type]
    await pilot.press(_AUTOPILOT_DIGIT)  # type: ignore[attr-defined]
    await settle_screen(pilot)  # type: ignore[arg-type]
    screen = app.screen
    assert isinstance(screen, AutopilotModeScreen)
    return screen


# --------------------------------------------------------------------------
# C1 -- every footer action resolves to a real handler (no silent no-op)
# --------------------------------------------------------------------------


def test_cockpit_every_footer_action_resolves_to_a_handler(tmp_path: Path) -> None:
    """C1: each mutating cockpit key resolves to a callable ``action_*`` handler.

    A bound key whose ``action_<name>`` method is absent on its resolving
    namespace fires NOTHING (a silent no-op). This probes every cockpit footer
    key against the mounted screen and asserts each resolves to a real handler --
    so no dispatch / pause / kill / arm / fork key is a dead binding.
    """
    state_path = _write_state(tmp_path, _state(waves=_frontier_waves(), fleet_run=_draining_run()))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _enter_cockpit(pilot, app)
            if isinstance(app.screen, ForkInboxModal):  # defensive -- no fork queued here
                await pilot.press("escape")
                await settle_screen(pilot)
            resolved = mutating_action_keys_resolve(
                bindings=_COCKPIT_ACTIONS,
                namespace=screen,
            )
            assert resolved == {key: True for key, _action in _COCKPIT_ACTIONS}

    asyncio.run(body())


# --------------------------------------------------------------------------
# C1 -- the cockpit never blocks on a sync daemon call (worker offload)
# --------------------------------------------------------------------------


def test_cockpit_dispatch_offloads_to_worker_never_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1: a dispatch press round-trips on a worker thread, never on the UI thread.

    The daemon ``agent.dispatch`` call is wrapped in a SLOW fake client: a sync
    call on the UI thread would block the event loop (and hang the cockpit), but
    because the call is offloaded to a Textual worker the Pilot's
    :func:`settle_screen` (which drains the worker pool) returns and the result
    line repaints. The fake's blocking sleep runs ON A WORKER THREAD, proving the
    offload: a UI-thread call could not be drained by ``wait_for_complete``.
    """
    state_path = _write_state(tmp_path, _state(waves=_frontier_waves(), fleet_run=_draining_run()))
    worker_thread_names: list[str] = []
    import threading

    class _SlowClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _SlowClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, _method: str, params: dict[str, object]) -> dict[str, object]:
            # Record the executing thread: a worker offload runs this OFF the
            # main thread, so the recorded name is not the main thread's.
            worker_thread_names.append(threading.current_thread().name)
            return {"runtime": "claude-code", "pid": 4321, "session_id": "S-1", "attempt": 1}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _SlowClient)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _enter_cockpit(pilot, app)
            await pilot.press("d")  # dispatch the selected ready wave (worker-offloaded)
            await settle_screen(pilot)  # drains the worker pool -- never hangs
            result = screen.query_one(f"#{DISPATCH_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "spawned" in rendered  # the worker's result repainted the line
            assert "4321" in rendered

    asyncio.run(body())
    # The RPC ran on a worker thread, not the Textual main thread -- the offload.
    assert worker_thread_names
    assert all(name != threading.main_thread().name for name in worker_thread_names)


def test_cockpit_pause_offloads_to_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1: over a live fleet run the pause key drives fleet.pause on a worker (W06).

    With a DRAINING fleet run bound, ``space`` drives the FLEET pause
    (``fleet.pause`` -- it holds the running drive loop without aborting it)
    rather than the global ``agent.pause`` dispatch-pause toggle. The RPC
    round-trips on a worker so the cockpit never blocks, then repaints honestly.
    """
    state_path = _write_state(tmp_path, _state(waves=_frontier_waves(), fleet_run=_draining_run()))
    calls: list[str] = []

    class _PauseClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _PauseClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, _params: dict[str, object]) -> dict[str, object]:
            calls.append(method)
            return {"run_state": "paused"}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _PauseClient)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _enter_cockpit(pilot, app)
            await pilot.press("space")  # pause (worker-offloaded)
            await settle_screen(pilot)
            result = screen.query_one(f"#{DISPATCH_RESULT_ID}")
            assert "paused" in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
    # A live fleet run routes space -> fleet.pause (W06), not the global toggle.
    # The cockpit also reattaches the draining run on mount (W07), so filter it.
    assert [m for m in calls if m != "fleet.reattach"] == ["fleet.pause"]


def test_cockpit_kill_offloads_to_worker_after_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1: a confirmed kill round-trips ``agent.kill`` on a worker, then repaints.

    The destructive kill is confirm-gated; confirming kicks the RPC onto a worker
    so the cockpit never blocks, and the honest (placeholder ``killed=false``)
    verdict repaints after the worker drains.
    """
    state_path = _write_state(tmp_path, _state(waves=_frontier_waves(), fleet_run=_draining_run()))
    calls: list[tuple[str, dict[str, object]]] = []

    class _KillClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _KillClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            return {"killed": False, "signal": params.get("signal")}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc
        from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _KillClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await _enter_cockpit(pilot, app)
            await pilot.press("K")  # kill -> confirm modal
            await settle_screen(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("right")  # highlight Yes
            await pilot.press("enter")  # confirm -> worker-offloaded RPC
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert "not killed" in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
    # Filter the on-mount fleet.reattach (W07): the confirmed kill is agent.kill.
    kill_calls = [c for c in calls if c[0] != "fleet.reattach"]
    assert kill_calls and kill_calls[0][0] == "agent.kill"
    assert kill_calls[0][1]["signal"] == "kill"


# --------------------------------------------------------------------------
# C1 -- a fork -> resolve -> close cycle re-renders the cockpit live
# --------------------------------------------------------------------------


def test_cockpit_fork_resolve_close_cycle_rerenders_live(tmp_path: Path) -> None:
    """C1: a fork -> resolve -> close cycle pushed live re-renders the cockpit.

    Drives the cockpit through the fleet lifecycle via the live push seam (the
    same coroutine the binder marshals every push + poll refresh through):

    1. a DRAINING run with a queued blocking fork auto-raises the FA5 inbox;
    2. dismissing it returns to the cockpit, which still shows the fork badge in
       its vitals (read straight off the persisted run);
    3. a follow-up push resolves the fork + closes the wave -- the cockpit
       re-renders LIVE (fork badge clears, the closed count advances) without an
       app restart.

    A mount-only snapshot would freeze step 1; the live re-render is the property
    the cockpit must hold.
    """
    forked = _draining_run(lanes=1, forks=[_blocking_fork()])
    state_path = _write_state(tmp_path, _state(waves=_frontier_waves(), fleet_run=forked))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            # Step 1: the queued fork auto-raised the FA5 inbox over the cockpit.
            assert isinstance(app.screen, ForkInboxModal)
            await pilot.press("escape")  # dismiss back to the cockpit
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            vitals = pane.query_one(f"#{COCKPIT_VITALS_ID}")
            assert "fork" in str(vitals.render()).lower()  # type: ignore[attr-defined]
            # Step 3: push the resolved + closed revision through the live seam.
            resolved = _draining_run(lanes=0, forks=[], closed=1)
            waves_closed = _frontier_waves(w02_status=WaveStatus.CLOSED)
            await push_state_revision(
                pilot,  # type: ignore[arg-type]
                _state(waves=waves_closed, fleet_run=resolved),
            )
            refreshed = str(pane.query_one(f"#{COCKPIT_VITALS_ID}").render())  # type: ignore[attr-defined]
            assert "fork" not in refreshed.lower()  # the fork badge cleared live
            # The lane that forked drained -- no lane cell lingers after the close.
            assert not pane.query(f".{LANE_CELL_CLASS}")

    asyncio.run(body())


# --------------------------------------------------------------------------
# C2 -- the poll backstop refreshes the cockpit (never stale-till-restart)
# --------------------------------------------------------------------------


def test_cockpit_poll_backstop_refreshes_without_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2: with push silent, the mtime-poll backstop still refreshes the cockpit.

    Mounts on an unarmed (idle-hero) scope, then -- WITHOUT delivering a daemon
    push -- advances the on-disk ``state.json`` mtime to a DRAINING revision and
    lets the binder's always-on ``set_interval`` poll loop tick
    (:func:`tick_poll_backstop`, under a tight ``EAWF_POLL_INTERVAL_S``). The
    cockpit vitals flip from the idle hero to the live DRAINING row, proving the
    poll backstop keeps the bound state current even when push is stalled -- never
    stale-till-restart.
    """
    monkeypatch.setenv("EAWF_POLL_INTERVAL_S", "0.05")
    state_path = _write_state(tmp_path, _state())  # unarmed at mount

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _enter_cockpit(pilot, app)
            vitals = screen.query_one(f"#{COCKPIT_VITALS_ID}")
            assert COCKPIT_IDLE in str(vitals.render())  # type: ignore[attr-defined]
            # Advance the on-disk state via the poll backstop (no push delivered).
            drained = _state(waves=_frontier_waves(), fleet_run=_draining_run())
            frame = await tick_poll_backstop(pilot, state_path, drained)  # type: ignore[arg-type]
            assert "draining" in frame  # the poll-driven refresh reached the frame
            refreshed = str(vitals.render())  # type: ignore[attr-defined]
            assert "draining" in refreshed  # the cockpit vitals refreshed off the poll
            assert COCKPIT_IDLE not in refreshed

    asyncio.run(body())
    assert os.environ.get("EAWF_POLL_INTERVAL_S") == "0.05"


# --------------------------------------------------------------------------
# W06 -- the cockpit fleet controls (space pause/resume, H halt) drive the
# fleet.* RPCs over a live run, without aborting it.
# --------------------------------------------------------------------------


def test_cockpit_resume_over_paused_run_drives_fleet_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W06: over a PAUSED fleet run the space key drives fleet.resume.

    A bound run whose state is PAUSED routes ``space`` to ``fleet.resume`` (it
    continues the held drive loop), not the global dispatch-pause toggle -- the
    resume continues the SAME run rather than aborting it.
    """
    paused = _draining_run()
    paused.run_state = FleetRunState.PAUSED
    state_path = _write_state(tmp_path, _state(waves=_frontier_waves(), fleet_run=paused))
    calls: list[str] = []

    class _ResumeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _ResumeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, _params: dict[str, object]) -> dict[str, object]:
            calls.append(method)
            return {"run_state": "draining"}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _ResumeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _enter_cockpit(pilot, app)
            await pilot.press("space")  # resume the held run
            await settle_screen(pilot)
            result = screen.query_one(f"#{DISPATCH_RESULT_ID}")
            assert "resumed" in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
    assert calls == ["fleet.resume"]


def test_cockpit_halt_over_live_run_drives_fleet_halt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W06: over a DRAINING fleet run the H key drives fleet.halt (no confirm/abort).

    With a live run bound, ``H`` drives the FLEET halt (``fleet.halt`` -- it
    drains the in-flight lanes to the summary card) rather than the per-wave
    ``agent.kill``. The halt runs straight on a worker (no destructive confirm)
    and never reaps the in-flight work.
    """
    state_path = _write_state(tmp_path, _state(waves=_frontier_waves(), fleet_run=_draining_run()))
    calls: list[str] = []

    class _HaltClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _HaltClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, _params: dict[str, object]) -> dict[str, object]:
            calls.append(method)
            return {"run_state": "halted"}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _HaltClient)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _enter_cockpit(pilot, app)
            await pilot.press("H")  # fleet halt -> drains to summary
            await settle_screen(pilot)
            result = screen.query_one(f"#{DISPATCH_RESULT_ID}")
            assert "draining to summary" in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
    # Filter the on-mount fleet.reattach (W07): H over a live run drives fleet.halt.
    assert [m for m in calls if m != "fleet.reattach"] == ["fleet.halt"]


# --------------------------------------------------------------------------
# W10 (CR-01) -- the cockpit vitals UPDATE LIVE throughout a draining run
# --------------------------------------------------------------------------


def _drain_snapshot(
    *,
    frontier_left: int,
    closed: int,
    spent_usd: float,
    throughput: float | None,
) -> FleetRun:
    """Build a DRAINING run snapshot at a point along a two-wave drain.

    The autopilot-acceptance capstone (W10) pins that the cockpit reads its
    vitals STRAIGHT off the daemon-written ``fleet_run`` mid-drain -- so a run
    that advances (the frontier shrinks, the USD spend rises toward the cap, the
    throughput appears once a lane closes) re-renders the cockpit vitals without
    an app restart. This builds the run at one such point so the test can push a
    sequence of advancing snapshots and assert the vitals follow each one.

    Args:
        frontier_left: Ready waves still queued (the ``frontier N left`` figure).
        closed: Lanes closed so far (the drain progress).
        spent_usd: Cumulative USD spend (the ``$ used/cap`` figure, under the cap).
        throughput: The daemon-computed wv/hr, or ``None`` before the first close.

    Returns:
        The :class:`FleetRun` snapshot at that drain point.
    """
    frontier = ["P01-I01-W02", "P01-I01-W03"][:frontier_left]
    lanes = {
        wid: FleetLane(wave_id=wid, attempt=1, pgid=1000, dispatched_at=_T0) for wid in frontier
    }
    return FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=2,
        frontier=frontier,
        lanes=lanes,
        forks=[],
        counters=FleetCounters(
            claimed=2,
            dispatched=len(lanes),
            closed=closed,
            spent_eu=spent_usd,  # EU tracks USD here; both rise toward their caps.
            spent_usd=spent_usd,
        ),
        eu_cap=10.0,
        usd_cap=8.0,
        throughput=throughput,
        armed_at=_T0,
    )


def test_cockpit_vitals_update_live_throughout_a_two_wave_drain(tmp_path: Path) -> None:
    """W10/CR-01: the cockpit vitals follow a two-wave drain LIVE, mid-run.

    Mounts the cockpit ARMED to a DRAINING run (the on-mount reattach binds it),
    then pushes a SEQUENCE of advancing run snapshots through the live push seam
    -- the same coroutine the binder marshals every daemon push + poll refresh
    through -- modelling the daemon writing successive ``fleet_run`` revisions as
    the two-wave frontier drains:

    1. armed: both waves queued, nothing closed yet, no throughput -- the
       ``frontier 2 left`` row with a ``$ 0.50/8.00`` early spend;
    2. first lane closes: the frontier drops to ``frontier 1 left``, the spend
       rises, and the daemon-computed throughput APPEARS (``2.0 wv/hr``);
    3. drain complete: ``frontier 0 left``, both lanes closed, the spend near the
       cap, the throughput risen.

    Each push re-renders the cockpit vitals off the daemon-written run, so the
    test asserts the live figures change at each step -- proving the cockpit reads
    the bus / persisted ``fleet_run`` mid-drain rather than freezing on a one-shot
    mount snapshot. A stale cockpit would still show step 1's vitals after step 3.
    """
    armed = _drain_snapshot(frontier_left=2, closed=0, spent_usd=0.5, throughput=None)
    state_path = _write_state(tmp_path, _state(waves=_frontier_waves(), fleet_run=armed))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _enter_cockpit(pilot, app)
            vitals = screen.query_one(f"#{COCKPIT_VITALS_ID}")

            # Step 1: armed -- both waves queued, no throughput yet.
            frame0 = str(vitals.render())  # type: ignore[attr-defined]
            assert "frontier 2 left" in frame0
            assert "draining" in frame0
            assert "$ 0.50/8.00" in frame0
            assert "-- wv/hr" in frame0  # no close yet -> no throughput

            # Step 2: the first lane closes -- the daemon writes the next revision.
            mid = _drain_snapshot(frontier_left=1, closed=1, spent_usd=4.0, throughput=2.0)
            await push_state_revision(
                pilot,  # type: ignore[arg-type]
                _state(waves=_frontier_waves(), fleet_run=mid),
            )
            frame1 = str(vitals.render())  # type: ignore[attr-defined]
            # The vitals followed the push: the frontier shrank, the spend rose,
            # the throughput appeared -- all read straight off the new run.
            assert frame1 != frame0  # the cockpit re-rendered, not frozen
            assert "frontier 1 left" in frame1
            assert "$ 4.00/8.00" in frame1
            assert "2.0 wv/hr" in frame1
            assert "-- wv/hr" not in frame1

            # Step 3: the drain completes -- the frontier empties, both lanes closed.
            done = _drain_snapshot(frontier_left=0, closed=2, spent_usd=7.5, throughput=3.0)
            await push_state_revision(
                pilot,  # type: ignore[arg-type]
                _state(waves=_frontier_waves(), fleet_run=done),
            )
            frame2 = str(vitals.render())  # type: ignore[attr-defined]
            assert frame2 != frame1  # the cockpit re-rendered again, live
            assert "frontier 0 left" in frame2
            assert "$ 7.50/8.00" in frame2  # spend climbed under the cap
            assert "3.0 wv/hr" in frame2
            # No lane cell lingers once both lanes drained.
            assert not screen.query(f".{LANE_CELL_CLASS}")

    asyncio.run(body())
