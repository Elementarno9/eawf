"""Shared fixtures for the epoch-2 importer-rule tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.allowlist import (
    LegacySymbolAllowlist,
    load_legacy_symbol_allowlist,
)
from eawf.kernel.migration.epoch2.corpus import Epoch1BacklogCorpus
from eawf.kernel.migration.epoch2.plan import LifecycleImportPlan
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot

MIGRATION_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
ALLOWLIST_PATH = MIGRATION_FIXTURES / "allowed_legacy_symbols.txt"
EPOCH1_FULL_CORPUS_PATH = MIGRATION_FIXTURES / "epoch1-full" / "backlog_corpus.json"
EPOCH1_FULL_SNAPSHOT = MIGRATION_FIXTURES / "epoch1-full" / "snapshot"


@pytest.fixture(scope="session")
def allowlist() -> LegacySymbolAllowlist:
    """The shared allowed-legacy-symbol allowlist."""
    return load_legacy_symbol_allowlist(ALLOWLIST_PATH)


@pytest.fixture(scope="session")
def epoch1_full() -> Epoch1BacklogCorpus:
    """The pinned epoch-1 backlog corpus slice."""
    return Epoch1BacklogCorpus.load(EPOCH1_FULL_CORPUS_PATH)


@pytest.fixture(scope="session")
def full_lifecycle_plan() -> LifecycleImportPlan:
    """Every lifecycle row of the full-shape snapshot, mapped once."""
    return LifecycleImportPlan.build(
        snapshot=SourceSnapshot.read(EPOCH1_FULL_SNAPSHOT),
        allowlist_path=ALLOWLIST_PATH,
    )
