"""The snapshot harness parks the state binder's daemon probe during quiesce.

:func:`~eawf.surfaces.tui.chassis.pilot_harness.quiesce_volatile_chrome`
forces ``app.degraded`` back to ``False`` before a golden capture. With no
daemon up, the binder's probe keeps counting socket failures after that, and
once the count crosses the threshold it flips ``degraded`` true again, so a
capture taken a moment later held whichever side of that flip the host had
reached. These tests run the probe at 50 ms with no daemon and prove the flag
stays down once quiesce has run, and pin
:meth:`~eawf.surfaces.tui.chassis.state_binding.StateBinding.park_daemon_probe` on its
own.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from textual.pilot import Pilot

from eawf.kernel.state.models import State
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.chassis.pilot_harness import quiesce_volatile_chrome
from eawf.surfaces.tui.chassis.state_binding import StateBinding, StateBindingCallbacks

_REPO_STATE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)
_SIZE = (120, 40)
_FAST_PROBE_S = "0.05"


@pytest.fixture
def _fast_probe_without_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe every 50 ms against a runtime dir that holds no daemon socket."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("EAWF_DAEMON_PROBE_INTERVAL_S", _FAST_PROBE_S)


@pytest.mark.usefixtures("_fast_probe_without_daemon")
def test_quiesce_parks_binder_probe_so_degraded_stays_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe_ticks: list[int] = []
    real_process = StateBinding._process_daemon_probe

    async def _counting_process(self: StateBinding) -> None:
        probe_ticks.append(1)
        await real_process(self)

    monkeypatch.setattr(StateBinding, "_process_daemon_probe", _counting_process)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as raw_pilot:
            pilot = cast("Pilot[object]", raw_pilot)
            binding = app._binding
            assert binding is not None
            assert binding._probe_task is not None
            # Leave the binder one failed probe short of the flip, so the next
            # tick trips degraded unless quiesce has parked the probe. No await
            # runs between this and the park inside quiesce.
            binding._is_degraded = False
            binding._consecutive_failures = binding._daemon_failure_threshold - 1

            await quiesce_volatile_chrome(pilot)
            ticks_at_quiesce = len(probe_ticks)
            await asyncio.sleep(1.5)

            assert app.degraded is False
            assert binding._probe_task is None
            assert len(probe_ticks) == ticks_at_quiesce

    asyncio.run(body())


@pytest.mark.usefixtures("_fast_probe_without_daemon")
def test_park_daemon_probe_stops_a_running_probe_before_it_flips() -> None:
    flips: list[bool] = []

    async def _on_state(_state: State) -> None:
        return None

    async def _on_degraded(degraded: bool) -> None:
        flips.append(degraded)

    async def body() -> None:
        binding = StateBinding(
            None,
            StateBindingCallbacks(on_state=_on_state, on_degraded=_on_degraded),
            daemon_failure_threshold=2,
        )
        await binding.connect()
        # connect() ran the first failed probe; the next tick would flip.
        await binding.park_daemon_probe()
        await asyncio.sleep(0.3)
        assert flips == []
        await binding.disconnect()

    asyncio.run(body())


def test_park_daemon_probe_is_a_noop_before_connect() -> None:
    async def _on_state(_state: State) -> None:
        return None

    async def _on_degraded(_degraded: bool) -> None:
        return None

    async def body() -> None:
        binding = StateBinding(
            None, StateBindingCallbacks(on_state=_on_state, on_degraded=_on_degraded)
        )
        await binding.park_daemon_probe()
        await binding.park_daemon_probe()
        assert binding._probe_task is None

    asyncio.run(body())
