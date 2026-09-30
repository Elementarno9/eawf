"""The console record reads: what each answers for an empty tree, a hit and a miss."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods.console_records import (
    MISSING_ENDING,
    BatchRecordsRead,
    TargetResolve,
    read_conflict_frames,
    read_health_verdicts,
    read_proof_receipts,
    resolve_target,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    provision,
    seed,
    seed_row,
)

AT: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
BATCH: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"


@pytest.fixture
def document(tmp_path: Path) -> Path:
    """Return the document of a canary holding one Batch and nothing on any ledger."""
    canary = provision(tmp_path / "repo")
    seed(canary, {Epoch2Collection.BATCH.value: {"BAT-0007": seed_row("batch", "ACTIVE")}})
    return document_path(canary)


def test_a_key_the_document_holds_resolves(document: Path) -> None:
    assert resolve_target(document, "BAT-0007").ending is None


def test_a_key_nothing_holds_is_missing(document: Path) -> None:
    assert resolve_target(document, "BAT-0999").ending == MISSING_ENDING


def test_a_key_only_a_ledger_line_names_is_not_missing(document: Path) -> None:
    append_ledger_record(
        ledger_path(document, Epoch2Collection.RECEIPT),
        LedgerRecord(
            collection=Epoch2Collection.RECEIPT,
            record_key="PRF-0000000000000000-EAWF-0001",
            status="pass",
            recorded_at=AT,
            payload={"receipt": {"id": "RCP-0042"}},
        ),
    )
    assert resolve_target(document, "RCP-0042").ending is None
    assert resolve_target(document, "RCP-0043").ending == MISSING_ENDING


def test_a_tree_with_no_ledger_yet_reads_empty_runs(document: Path) -> None:
    batch = BatchRecordsRead.model_validate({"urn": BATCH}).urn
    assert read_conflict_frames(ledger_path(document, Epoch2Collection.BATCH), batch) == ()
    assert read_proof_receipts(ledger_path(document, Epoch2Collection.RECEIPT), "RCP-1") == ()


def test_a_tree_that_never_certified_holds_no_verdict(document: Path) -> None:
    tree = next(parent for parent in document.parents if parent.name == ".ea")
    assert read_health_verdicts(tree).verdicts == ()


@pytest.mark.parametrize("key", ["", "bat-0007", "BAT", "BAT-0007 ", 7])
def test_a_key_outside_the_entity_grammar_is_refused(key: object) -> None:
    with pytest.raises(ValidationError):
        TargetResolve.model_validate({"key": key})


def test_an_unknown_parameter_is_refused() -> None:
    with pytest.raises(ValidationError):
        TargetResolve.model_validate({"key": "BAT-0007", "route": "git.pr"})
