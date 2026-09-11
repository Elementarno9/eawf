"""One replay of the tracked console contract, shared by every test in this directory.

The contract's ordering rule is that the 25 journeys run before the 261 frames in a
single app instance: the frames must still match in a session twenty-five journeys of
navigation have left deep in state, which is what proves the one canonical reset. A
session-scoped fixture is therefore the only honest shape -- a per-test app would prove
the resets independently and the ordering rule not at all.
"""

from __future__ import annotations

import asyncio

import pytest

from .console_chassis.harness.replay import Result, load_sequences, run


@pytest.fixture(scope="session")
def replay() -> dict[str, Result]:
    """Return one replay result per journey id and per frame id."""
    return {result.id: result for result in asyncio.run(run())}


@pytest.fixture(scope="session")
def contract_ids() -> tuple[str, ...]:
    """Return every journey id then every frame id of the tracked contract."""
    _index, states, journeys = load_sequences()
    return tuple(journey["id"] for journey in journeys) + tuple(state["id"] for state in states)
