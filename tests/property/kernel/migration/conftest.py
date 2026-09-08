"""Shared fixtures for the epoch-2 no-fabrication property tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.lifecycle import LifecycleSourceIndex
from eawf.kernel.migration.epoch2.plan import LifecycleImportPlan
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot

MIGRATION_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
ALLOWLIST_PATH = MIGRATION_FIXTURES / "allowed_legacy_symbols.txt"
SPARSE_HISTORY_SNAPSHOT = MIGRATION_FIXTURES / "sparse-history" / "snapshot"

#: The one session the sparse-history corpus really holds. Every other
#: claim id in that corpus names a session that never existed.
RESOLVING_SESSION_ID = "S001"

#: A claim id the corpus names and no session row backs.
DANGLING_SESSION_ID = "S404"


def sparse_history_plan() -> LifecycleImportPlan:
    """Map the sparse-history corpus, reading it once."""
    return LifecycleImportPlan.build(
        snapshot=SourceSnapshot.read(SPARSE_HISTORY_SNAPSHOT),
        allowlist_path=ALLOWLIST_PATH,
    )


def sparse_history_index() -> LifecycleSourceIndex:
    """The resolution populations of the sparse-history corpus.

    Built without the plan so a property test can drive one mapper over
    generated input without re-reading the snapshot per example.
    """
    return LifecycleSourceIndex.build(SourceSnapshot.read(SPARSE_HISTORY_SNAPSHOT).document)


@pytest.fixture(scope="session")
def sparse_plan() -> LifecycleImportPlan:
    """The sparse-history corpus, mapped once for the whole session."""
    return sparse_history_plan()
