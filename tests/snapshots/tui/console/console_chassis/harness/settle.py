"""Settle: pause, drain workers, then pump until two consecutive captures agree.

``pilot.pause()`` is idle-based, so a bare pause is never treated as evidence of a
finished frame. The cycle count is returned so a capture that needed more than one
cycle under a held clock is visible as a defect signal.
"""

from __future__ import annotations

from textual.pilot import Pilot

from ..harness.capture import capture

SETTLE_MAX_CYCLES = 5


async def settle(pilot: Pilot) -> tuple[str, int]:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    previous = capture(pilot.app)
    cycles = 1
    while cycles < SETTLE_MAX_CYCLES:
        await pilot.pause()
        current = capture(pilot.app)
        if current == previous:
            return current, cycles
        previous = current
        cycles += 1
    return previous, cycles
