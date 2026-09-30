"""The memory import: index rows and store envelopes reconcile as one population."""

from __future__ import annotations

from typing import Any

import pytest

from eawf.kernel.migration.epoch2.envelopes import EnvelopeImportPlan, LedgerCollection
from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError
from eawf.kernel.migration.epoch2.memory import (
    MEMORY_STORE_SOURCE,
    MemoryUnionCensus,
    check_memory_union,
    latest_store_rows,
)
from tests.unit.kernel.migration.conftest import build_full_corpus_plan

SOURCE_SCHEMA_VERSION = "1.19"


def _index_row(mid: str) -> dict[str, Any]:
    return {
        "id": mid,
        "scope_id": "QR",
        "summary": f"{mid}: summary",
        "confidence": "medium",
        "status": "active",
        "store_record_id": mid,
        "tier": "working",
    }


def _store_row(mid: str, body: str) -> dict[str, Any]:
    return {
        "id": mid,
        "kind": "memory",
        "scope_id": "QR",
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": None,
        "summary": f"{mid}: summary",
        "payload": {"body": body, "confidence": "medium"},
        "blob_refs": [],
        "artifact_ids": [],
    }


def _plan(store_rows: tuple[dict[str, Any], ...]) -> EnvelopeImportPlan:
    return EnvelopeImportPlan.build(
        document={"memory_index": {"MEM-A": _index_row("MEM-A"), "MEM-B": _index_row("MEM-B")}},
        audit_ledger_rows=(),
        memory_store_rows=store_rows,
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )


def test_memory_import_reconciles_index_and_store_counts() -> None:
    plan = _plan(
        (
            _store_row("MEM-B", "first body"),
            _store_row("MEM-B", "revised body"),
            _store_row("MEM-C", "store only"),
            {"kind": "memory"},
        )
    )

    assert plan.memory == MemoryUnionCensus(
        document_rows=2, store_rows=2, union_rows=3, store_only_imported=1, ledger_lines=4
    )
    store = [row for row in plan.ledger_rows if row.source_collection == MEMORY_STORE_SOURCE]
    assert [(row.source_id, row.store_only) for row in store] == [("MEM-B", True), ("MEM-C", True)]
    assert store[0].payload["payload"]["body"] == "revised body"
    assert store[0].alias == "legacy:memory_index.store/MEM-B"
    assert {row.ledger for row in store} == {"memory"}


def test_memory_import_without_a_store_imports_the_index_alone() -> None:
    plan = _plan(())

    assert plan.memory.ledger_lines == 2
    assert plan.memory.store_only_imported == 0
    assert len(plan.for_ledger(LedgerCollection.MEMORY_INDEX)) == 2


def test_memory_import_over_the_full_corpus_balances() -> None:
    memory = build_full_corpus_plan().envelopes.memory

    assert memory.document_rows == 2
    assert memory.ledger_lines == memory.document_rows + memory.store_rows


def test_latest_store_rows_keeps_the_last_revision_and_skips_rows_without_an_id() -> None:
    rows = (_store_row("MEM-A", "one"), {"id": ""}, _store_row("MEM-A", "two"), {"id": 3})

    latest = latest_store_rows(rows)

    assert list(latest) == ["MEM-A"]
    assert latest["MEM-A"]["payload"]["body"] == "two"


@pytest.mark.parametrize(
    ("census", "message"),
    [
        (
            MemoryUnionCensus(
                document_rows=3, store_rows=1, union_rows=2, store_only_imported=0, ledger_lines=4
            ),
            "fewer than its largest input",
        ),
        (
            MemoryUnionCensus(
                document_rows=2, store_rows=2, union_rows=3, store_only_imported=2, ledger_lines=4
            ),
            "the index contributes",
        ),
        (
            MemoryUnionCensus(
                document_rows=2, store_rows=2, union_rows=3, store_only_imported=1, ledger_lines=3
            ),
            "the memory ledger receives 3 lines",
        ),
    ],
)
def test_check_memory_union_refuses_counts_that_do_not_reconcile(
    census: MemoryUnionCensus, message: str
) -> None:
    with pytest.raises(MigrationCountMismatchError, match=message):
        check_memory_union(census)


def test_check_memory_union_accepts_an_empty_population() -> None:
    check_memory_union(MemoryUnionCensus.build(document_ids=(), store_ids=(), ledger_lines=0))
