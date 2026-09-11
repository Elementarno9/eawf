"""Shared fixtures for the epoch-2 import property tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.lifecycle import LifecycleSourceIndex
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan, LifecycleImportPlan
from eawf.kernel.migration.epoch2.runs import report_ledger_rows
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot
from eawf.kernel.migration.epoch2.validation import CorpusIdentity, ImportValidationReport

MIGRATION_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
ALLOWLIST_PATH = MIGRATION_FIXTURES / "allowed_legacy_symbols.txt"
SPARSE_HISTORY_SNAPSHOT = MIGRATION_FIXTURES / "sparse-history" / "snapshot"
ATTEMPT_MAP_SNAPSHOT = MIGRATION_FIXTURES / "attempt-map" / "snapshot"
EPOCH1_FULL_SNAPSHOT = MIGRATION_FIXTURES / "epoch1-full" / "snapshot"
ALIAS_COLLISION_SNAPSHOT = MIGRATION_FIXTURES / "alias-collision" / "snapshot"
AMBIGUOUS_HISTORY_SNAPSHOT = MIGRATION_FIXTURES / "ambiguous-history" / "snapshot"

#: The one session the sparse-history corpus really holds. Every other
#: claim id in that corpus names a session that never existed.
RESOLVING_SESSION_ID = "S001"

#: A claim id the corpus names and no session row backs.
DANGLING_SESSION_ID = "S404"

#: The addressing slots the fixtures' imports are minted under. Epoch 1
#: recorded a project code and nothing about the workspace or repository
#: the corpus lands in, so the cutover supplies both.
CUTOVER_IDENTITY = CorpusIdentity(
    workspace_key="WSP-DEFAULT",
    project_key="PRJ-DEMO",
    repository_key="REP-DEMO",
)


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


def corpus_plan(snapshot_root: Path) -> CorpusImportPlan:
    """Import the corpus staged under ``snapshot_root``."""
    return CorpusImportPlan.build(
        snapshot=SourceSnapshot.read(snapshot_root),
        allowlist_path=ALLOWLIST_PATH,
    )


def validation_report(snapshot_root: Path) -> ImportValidationReport:
    """Validate the staged import of the corpus under ``snapshot_root``."""
    return ImportValidationReport.build(
        snapshot=SourceSnapshot.read(snapshot_root),
        allowlist_path=ALLOWLIST_PATH,
        identity=CUTOVER_IDENTITY,
    )


@pytest.fixture(scope="session")
def sparse_plan() -> LifecycleImportPlan:
    """The sparse-history corpus, mapped once for the whole session."""
    return sparse_history_plan()


@pytest.fixture(scope="session")
def attempt_plan() -> CorpusImportPlan:
    """The attempt-map corpus, imported once for the whole session."""
    return attempt_map_plan()


@pytest.fixture(scope="session")
def full_report() -> ImportValidationReport:
    """The full-shape corpus, imported and validated once."""
    return validation_report(EPOCH1_FULL_SNAPSHOT)


@pytest.fixture(scope="session")
def ambiguous_report() -> ImportValidationReport:
    """The ambiguous-history corpus, imported and validated once."""
    return validation_report(AMBIGUOUS_HISTORY_SNAPSHOT)


@pytest.fixture(scope="session")
def ambiguous_plan() -> CorpusImportPlan:
    """The ambiguous-history corpus, imported once for the whole session."""
    return corpus_plan(AMBIGUOUS_HISTORY_SNAPSHOT)
