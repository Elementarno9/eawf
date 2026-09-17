"""One replay of the tracked console contract, shared by every test in this directory.

The contract's ordering rule is that the 25 journeys run before the 261 frames in a
single app instance: the frames must still match in a session twenty-five journeys of
navigation have left deep in state, which is what proves the one canonical reset. A
session-scoped fixture is therefore the only honest shape -- a per-test app would prove
the resets independently and the ordering rule not at all.

The replay runs through the product console's own golden harness; the test-only chassis
that used to carry it is gone.
"""

from __future__ import annotations

import asyncio

import pytest

from eawf.surfaces.tui.console import harness
from eawf.surfaces.tui.console.harness import Contract, Result, load_contract

from .goldens import LAYOUT


@pytest.fixture(scope="session")
def contract() -> Contract:
    """Return the tracked contract: its index, frame states and journeys."""
    return load_contract(LAYOUT.sequences)


@pytest.fixture(scope="session")
def replay() -> dict[str, Result]:
    """Return one replay result per journey id and per frame id."""
    return {result.id: result for result in asyncio.run(harness.replay(LAYOUT))}


@pytest.fixture(scope="session")
def contract_ids(contract: Contract) -> tuple[str, ...]:
    """Return every journey id then every frame id of the tracked contract."""
    return contract.ids
