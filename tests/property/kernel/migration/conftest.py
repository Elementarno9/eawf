"""Shared fixtures for the epoch-2 no-fabrication property tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.lifecycle import LifecycleSourceIndex
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan, LifecycleImportPlan
from eawf.kernel.migration.epoch2.runs import report_ledger_rows
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot

MIGRATION_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
ALLOWLIST_PATH = MIGRATION_FIXTURES / "allowed_legacy_symbols.txt"
SPARSE_HISTORY_SNAPSHOT = MIGRATION_FIXTURES / "sparse-history" / "snapshot"
ATTEMPT_MAP_SNAPSHOT = MIGRATION_FIXTURES / "attempt-map" / "snapshot"

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
    snapshot = SourceSnapshot.read(SPARSE_HISTORY_SNAPSHOT)
    return LifecycleSourceIndex.build(
        snapshot.document, report_rows=report_ledger_rows(snapshot.ledgers)
    )


def attempt_map_plan() -> CorpusImportPlan:
    """Import the attempt-map corpus, reading it once.

    The corpus is the only place every arm of the attempt-to-Run rule
    fires: the live epoch-1 corpus binds no report to any of its provider
    session ids, so its SUCCEEDED and FAILED arms cannot be exercised
    without inventing source rows that were never written.
    """
    return CorpusImportPlan.build(
        snapshot=SourceSnapshot.read(ATTEMPT_MAP_SNAPSHOT),
        allowlist_path=ALLOWLIST_PATH,
    )


@pytest.fixture(scope="session")
def sparse_plan() -> LifecycleImportPlan:
    """The sparse-history corpus, mapped once for the whole session."""
    return sparse_history_plan()


@pytest.fixture(scope="session")
def attempt_plan() -> CorpusImportPlan:
    """The attempt-map corpus, imported once for the whole session."""
    return attempt_map_plan()
