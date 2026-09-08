"""Every tracked frame, compared whole, plus the settle and capture-grid probes.

A frame matches when every row of the golden equals the captured compositor strip with
trailing spaces kept -- never right-stripped, never trimmed of blank rows. Each id is its
own parametrised case so a failure names the frame rather than the suite.
"""

from __future__ import annotations

import asyncio

import pytest

from .console_chassis.chassis.app import ConsoleApp
from .console_chassis.chassis.fixture import load_fixture
from .console_chassis.chassis.session import SIZES, FakeClock
from .console_chassis.harness.capture import capture_cells, capture_rows
from .console_chassis.harness.replay import Result, load_sequences, visible
from .console_chassis.harness.settle import settle

_INDEX, _STATES, _JOURNEYS = load_sequences()

#: Every frame id of the tracked contract, in the order the pack files carry them.
FRAME_IDS: tuple[str, ...] = tuple(state["id"] for state in _STATES)

#: Every journey id, whose steps carry their own settle-cycle counts.
JOURNEY_IDS: tuple[str, ...] = tuple(journey["id"] for journey in _JOURNEYS)

#: The contract's census, asserted rather than read from the pack's stale count fields.
EXPECTED_FRAMES = 261
EXPECTED_JOURNEYS = 25


def test_tracked_contract_carries_the_whole_census() -> None:
    assert len(FRAME_IDS) == EXPECTED_FRAMES
    assert len(JOURNEY_IDS) == EXPECTED_JOURNEYS
    assert len(set(FRAME_IDS)) == EXPECTED_FRAMES


@pytest.mark.parametrize("frame_id", FRAME_IDS)
def test_golden_frame_matches_whole(frame_id: str, replay: dict[str, Result]) -> None:
    result = replay[frame_id]
    assert result.ok, (
        f"{frame_id}: {result.detail}\n"
        f" exp |{visible(result.expected or '')}|\n"
        f" got |{visible(result.actual or '')}|"
    )


@pytest.mark.parametrize("frame_id", FRAME_IDS)
def test_golden_frame_settles_in_one_cycle(frame_id: str, replay: dict[str, Result]) -> None:
    result = replay[frame_id]
    assert result.settle_cycles == 1, f"{frame_id}: settle needed {result.settle_cycles} cycles"


@pytest.mark.parametrize("journey_id", JOURNEY_IDS)
def test_journey_step_settles_in_one_cycle(journey_id: str, replay: dict[str, Result]) -> None:
    result = replay[journey_id]
    noisy = [step for step in result.steps if step.get("cycles", 1) != 1]
    assert not noisy, f"{journey_id}: steps needed more than one settle cycle: {noisy}"


@pytest.mark.parametrize("size", SIZES)
def test_capture_probe_settles_to_a_clean_grid(size: tuple[int, int]) -> None:
    """The capture path yields H strips of cell width W and never an escape sequence."""
    width, height = size

    async def body() -> None:
        app = ConsoleApp(load_fixture(), FakeClock())
        async with app.run_test(size=size) as pilot:
            _text, cycles = await settle(pilot)
            rows = capture_rows(app)
            assert cycles == 1
            assert len(rows) == height
            assert set(capture_cells(app)) == {width}
            assert all("\x1b" not in row for row in rows)

    asyncio.run(body())
