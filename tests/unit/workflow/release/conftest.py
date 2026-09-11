"""Fixtures for the release-workflow behaviour tests.

The builders themselves live in :mod:`tests._release_helpers`, shared
with the kernel mirror; only the pytest fixtures are per-directory,
because a fixture is resolved by directory and cannot be shared from a
plain module.
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.release_config import ReleaseConfig
from tests._release_helpers import dev1_config


@pytest.fixture
def config() -> ReleaseConfig:
    """Return the authored dev1 checkpoint configuration."""
    return dev1_config()
