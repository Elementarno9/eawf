"""Every tracked journey: the after{} projection and the frame at every step.

Journeys replay before the frames in the same app instance, so what these cases prove is
the reset as much as the render: a journey that leaves the session twenty-five steps deep
must not move any later frame.
"""

from __future__ import annotations

import pytest

from .console_chassis.harness.replay import Result, load_sequences, visible

_INDEX, _STATES, _JOURNEYS = load_sequences()

#: Every journey id of the tracked contract.
JOURNEY_IDS: tuple[str, ...] = tuple(journey["id"] for journey in _JOURNEYS)

#: Steps summed over the 25 journeys, asserted rather than read from a stale count field.
EXPECTED_STEPS = 188


def test_tracked_journeys_carry_every_step() -> None:
    assert len(JOURNEY_IDS) == 25
    assert sum(len(journey["steps"]) for journey in _JOURNEYS) == EXPECTED_STEPS


@pytest.mark.parametrize("journey_id", JOURNEY_IDS)
def test_golden_journey_matches_projection_and_frame(
    journey_id: str, replay: dict[str, Result]
) -> None:
    result = replay[journey_id]
    failed = [step for step in result.steps if not step["ok"]]
    assert result.ok, (
        f"{journey_id}: {result.detail}\n"
        f" steps: {failed}\n"
        f" exp |{visible(result.expected or '')}|\n"
        f" got |{visible(result.actual or '')}|"
    )


@pytest.mark.parametrize("journey_id", JOURNEY_IDS)
def test_golden_journey_replays_every_recorded_step(
    journey_id: str, replay: dict[str, Result]
) -> None:
    recorded = next(journey for journey in _JOURNEYS if journey["id"] == journey_id)
    assert len(replay[journey_id].steps) == len(recorded["steps"])
