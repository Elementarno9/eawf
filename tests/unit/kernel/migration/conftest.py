"""Shared fixtures for the epoch-2 importer-rule tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.allowlist import (
    LegacySymbolAllowlist,
    load_legacy_symbol_allowlist,
)
from eawf.kernel.migration.epoch2.corpus import Epoch1BacklogCorpus

MIGRATION_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
ALLOWLIST_PATH = MIGRATION_FIXTURES / "allowed_legacy_symbols.txt"
EPOCH1_FULL_CORPUS_PATH = MIGRATION_FIXTURES / "epoch1-full" / "backlog_corpus.json"


@pytest.fixture(scope="session")
def allowlist() -> LegacySymbolAllowlist:
    """The shared allowed-legacy-symbol allowlist."""
    return load_legacy_symbol_allowlist(ALLOWLIST_PATH)


@pytest.fixture(scope="session")
def epoch1_full() -> Epoch1BacklogCorpus:
    """The pinned epoch-1 backlog corpus slice."""
    return Epoch1BacklogCorpus.load(EPOCH1_FULL_CORPUS_PATH)
