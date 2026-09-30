"""A cutover carries every memory note's body onto the memory ledger, and the counts balance.

The pinned corpus holds two ``memory_index`` rows and no memory store. The
suite adds a store beside them -- two revisions of one indexed note and one
note only the store held -- and applies the cutover, so the rows it reads
back were written by the production importer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eawf.kernel.migration.epoch2.memory import MemoryUnionCensus
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot
from eawf.kernel.state.enums import MemoryStatus
from eawf.platform.memory.book import generation_memory_ledger, read_book
from tests.integration.kernel.migration._cutover_harness import (
    ALLOWLIST,
    APPLIED_AT,
    apply_once,
    declared_canary,
    staged_corpus,
)


def _envelope(mid: str, body: str, *, expired: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"body": body, "confidence": "low"}
    if expired:
        payload["expired_at"] = "2026-02-15T00:00:00Z"
    return {
        "id": mid,
        "kind": "memory",
        "scope_id": "DEMO",
        "created_at": "2026-01-15T00:00:00Z",
        "updated_at": None,
        "summary": f"{mid}: {body}",
        "payload": payload,
        "blob_refs": [],
        "artifact_ids": [],
    }


def _seed_memory_store(corpus: Path) -> None:
    """Give the corpus a memory store and index tiers the note model reads."""
    document_path = corpus / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    for row in document["memory_index"].values():
        row["tier"] = "working"
    document_path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    rows = (
        _envelope("MEM01", "first body"),
        _envelope("MEM01", "revised body"),
        _envelope("MEM03", "only the store held this", expired=True),
    )
    (corpus / "store" / "memory.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def test_cutover_imports_memory_bodies_with_reconciling_counts(tmp_path: Path) -> None:
    corpus = staged_corpus(tmp_path)
    _seed_memory_store(corpus)

    plan = CorpusImportPlan.build(snapshot=SourceSnapshot.read(corpus), allowlist_path=ALLOWLIST)
    assert plan.envelopes.memory == MemoryUnionCensus(
        document_rows=2, store_rows=2, union_rows=3, store_only_imported=1, ledger_lines=4
    )

    target_root = declared_canary(tmp_path / ".ea")
    apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)
    ledger = generation_memory_ledger(target_root)
    assert ledger is not None
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 4

    book = read_book(ledger)
    assert sorted(book) == ["MEM01", "MEM02", "MEM03"]
    assert book["MEM01"].note.body == "revised body"
    assert book["MEM01"].note.status is MemoryStatus.ACTIVE
    assert book["MEM01"].note.created_at is not None
    assert book["MEM02"].note.body == ""
    assert book["MEM02"].note.status is MemoryStatus.SUPERSEDED
    assert book["MEM03"].note.status is MemoryStatus.PRUNED
    assert book["MEM03"].note.body == "only the store held this"
