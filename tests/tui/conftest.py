"""TUI-tier collection wiring.

Every test collected under ``tests/tui/`` is tagged with the ``tui``
marker, derived from the directory rather than a per-file
``@pytest.mark.tui`` decorator. Pytest calls each conftest's
``pytest_collection_modifyitems`` with the full collected set, so this
hook marks only the items whose path is beneath this tier directory --
tier membership is a property of WHERE a test lives, with no per-file
edit. The ``tui`` tier is where a test may legitimately import
``textual`` and mount widgets (the unit tier forbids it, per EAWF024).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual import _wait

_TIER_DIR = Path(__file__).parent

# A pilot pause waits until the process idles, sampling in sleeps of
# ``SLEEP_GRANULARITY`` (20 ms by default) and calling the process idle once
# one sleep burns under ``SLEEP_IDLE`` of CPU. Every settle pauses at least
# twice, so the 20 ms sample floor was most of each key's cost. Sampling every
# 5 ms keeps the same 5 % CPU ratio as the idle test and the same 1 s cap.
_wait.SLEEP_GRANULARITY = 1 / 200
_wait.SLEEP_IDLE = _wait.SLEEP_GRANULARITY / 20.0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag every test collected under ``tests/tui/`` with the ``tui`` marker."""
    for item in items:
        if _TIER_DIR in item.path.parents:
            item.add_marker("tui")
