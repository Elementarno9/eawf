"""Shared fixtures for the epoch-2 migration integration tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(scope="module", autouse=True)
def _drop_inherited_state_override() -> Iterator[None]:
    """Keep an inherited ``EA_STATE`` from redirecting a migration test.

    Every test here builds its own tree under a tmp path and reaches it
    through ``-w`` or the cwd walk. A gate runner exports ``EA_STATE`` at its
    sandbox copy of the live state, and ``EA_STATE`` outranks both, so the
    rehearsal's backup, stage and apply would otherwise resolve against the
    sandbox instead of the tree the test built. Module scope matters: several
    corpus fixtures here are module-scoped and must already see the cleared
    variable when they build their tree.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("EA_STATE", raising=False)
        yield
