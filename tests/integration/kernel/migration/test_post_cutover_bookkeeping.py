"""The bookkeeping a phase needs after the cutover, end to end on a real tree.

A repository cut over mid-phase still has to finish that phase: close the
claimed wave and its siblings, record the audit and the decisions the
close cites, drop the backlog it settled, and close the iter and the phase.
The epoch-1 verbs that used to do all of it are fenced, so this walks the
whole sequence through ``domain.legacy.advance`` and ``domain.record.append``
on the ``applied_tree`` cutover seeded with a claimed wave, and checks the
two things the cut promises: every write lands in the generation, and the
fenced epoch-1 surfaces keep their bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods import MethodContext
from tests.integration.kernel.migration._cutover_harness import SEEDED_SURFACES
from tests.integration.kernel.migration._legacy_continuation import (
    APPEND,
    BACKLOG_ROW,
    CLAIMED_GREEN,
    ITER,
    PHASE,
    PLANNED_GREEN,
    RED_FILE,
    RUNNING_RED,
    WAVES,
    advance,
    call,
    ledger,
    method_context,
    row,
    seeded_applied_tree,
)

AUDIT = {
    "id": "A900",
    "scope_id": ITER,
    "kind": "evaluation",
    "status": "complete",
    "created_at": "2026-03-04T00:00:00Z",
    "verdict": "pass",
}
DECISION = {
    "id": "D90",
    "scope_id": PHASE,
    "title": "Close the phase through the legacy continuation",
    "rationale": "The epoch-1 close verbs are fenced after the cutover.",
    "status": "active",
    "created_at": "2026-03-04T00:00:00Z",
}
ARTIFACT = {
    "id": "ART-90",
    "kind": "research",
    "uri": ".ea/artifacts/research/ART-90.md",
    "urn": "urn:eawf:v1:artifact:P04/ART-90",
    "created_at": "2026-03-04T00:00:00Z",
}


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MethodContext:
    """Return a daemon context whose runtime directory is this test's own."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    return method_context(runtime)


def _ok(answer: dict[str, Any]) -> dict[str, Any]:
    assert answer["status"] == "ok", answer
    result: dict[str, Any] = answer["result"]
    return result


def _digests(root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SEEDED_SURFACES
    }


def test_phase_closes_through_the_generation_alone(tmp_path: Path, ctx: MethodContext) -> None:
    tree = seeded_applied_tree(tmp_path / "repo")
    fenced = _digests(tree.target_root)

    _ok(advance(tree, ctx, key=CLAIMED_GREEN, collection="task", to="RUNNING"))
    _ok(advance(tree, ctx, key=CLAIMED_GREEN, collection="task", to="COMPLETED"))
    _ok(advance(tree, ctx, key=PLANNED_GREEN, collection="task", to="CLAIMED"))
    _ok(advance(tree, ctx, key=PLANNED_GREEN, collection="task", to="COMPLETED"))
    (tree.target_root.parent / RED_FILE).write_text(
        "def test_red():\n    assert True\n", encoding="utf-8"
    )
    _ok(advance(tree, ctx, key=RUNNING_RED, collection="task", to="COMPLETED"))

    for kind, record in (("audit", AUDIT), ("decision", DECISION), ("artifact", ARTIFACT)):
        filed = _ok(call(tree, APPEND, ctx, kind=kind, record=record))
        assert filed["record_key"] == record["id"]
        (line,) = [
            item for item in ledger(tree, Epoch2Collection(kind)) if item.record_key == record["id"]
        ]
        assert line.payload["payload"]["id"] == record["id"]

    _ok(advance(tree, ctx, key=BACKLOG_ROW, collection="task", to="DROPPED"))
    closed_batch = _ok(advance(tree, ctx, key=ITER, collection="batch", to="COMPLETED"))
    assert closed_batch["compacted"] is True

    unaudited = advance(
        tree, ctx, key=PHASE, collection="milestone", to="COMPLETED", evidence_refs=("D90",)
    )
    assert unaudited["errors"][0]["guard"] == "audit_ref"
    _ok(
        advance(
            tree,
            ctx,
            key=PHASE,
            collection="milestone",
            to="COMPLETED",
            evidence_refs=(AUDIT["id"], DECISION["id"]),
        )
    )

    for collection, key in (
        *((Epoch2Collection.TASK, wave) for wave in WAVES),
        (Epoch2Collection.BATCH, ITER),
        (Epoch2Collection.MILESTONE, PHASE),
    ):
        assert row(tree, collection, key) is None
        (line,) = [item for item in ledger(tree, collection) if item.record_key == key]
        assert line.status == "COMPLETED"
    dropped = row(tree, Epoch2Collection.TASK, BACKLOG_ROW)
    assert dropped is not None and dropped["status"] == "DROPPED"
    assert len(ledger(tree, Epoch2Collection.RECEIPT)) == len(WAVES)
    assert _digests(tree.target_root) == fenced


@pytest.mark.parametrize(
    ("kind", "record"),
    [
        ("audit", {**AUDIT, "unexpected": "field"}),
        ("decision", {key: value for key, value in DECISION.items() if key != "rationale"}),
        ("artifact", {**ARTIFACT, "created_at": "not a date"}),
        ("audit", {**AUDIT, "id": "A001"}),
    ],
)
def test_malformed_or_duplicate_record_is_refused(
    tmp_path: Path, ctx: MethodContext, kind: str, record: dict[str, Any]
) -> None:
    tree = seeded_applied_tree(tmp_path / "repo")
    before = ledger(tree, Epoch2Collection(kind))

    answer = call(tree, APPEND, ctx, kind=kind, record=record)

    assert answer["status"] == "error", answer
    assert answer["errors"][0]["code"] == "schema_validation_failed"
    assert ledger(tree, Epoch2Collection(kind)) == before
