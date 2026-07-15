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

_TIER_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag every test collected under ``tests/tui/`` with the ``tui`` marker."""
    for item in items:
        if _TIER_DIR in item.path.parents:
            item.add_marker("tui")
