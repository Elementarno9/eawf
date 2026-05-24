from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Skeleton fixture for a throwaway repository directory.

    Phase 1+ tests will populate this with the canonical .ea/ skeleton via
    eawf.platform.install. For now it returns a bare temp directory.
    """
    return tmp_path
