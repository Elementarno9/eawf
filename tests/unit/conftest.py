"""Unit-tier collection wiring.

Every test collected under ``tests/unit/`` is tagged with the ``unit``
marker, derived from the directory rather than a per-file
``@pytest.mark.unit`` decorator. Pytest calls each conftest's
``pytest_collection_modifyitems`` with the full collected set, so this
hook marks only the items whose path is beneath this tier directory --
tier membership is a property of WHERE a test lives, with no per-file
edit. The companion EAWF024 lint keeps this tier honest by rejecting a
``tests/unit/`` file that imports ``subprocess`` / ``textual`` /
``CliRunner`` (a mislabeled integration/TUI test).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TIER_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag every test collected under ``tests/unit/`` with the ``unit`` marker."""
    for item in items:
        if _TIER_DIR in item.path.parents:
            item.add_marker("unit")
