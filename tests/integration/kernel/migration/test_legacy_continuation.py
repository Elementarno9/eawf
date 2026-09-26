"""An imported row moves only along the closed legacy edge table.

Each case drives ``domain.legacy.advance`` through the daemon's own
dispatch against a real cutover, over rows the production importer wrote.
The admitted edges commit, change the row's status and nothing of its
payload, and append one continuation event; every planted off-table edge
comes back ``legacy_edge_refused`` with the document byte-for-byte what it
was. The gate-bearing completion edges are the next suite's subject; here
they appear only as members of the pinned table.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.continuation import LEGACY_EDGES
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods import MethodContext
from tests.integration.kernel.migration._cutover_harness import CutoverTree
from tests.integration.kernel.migration._legacy_continuation import (
    BACKLOG_ROW,
    CLAIMED_GREEN,
    ITER,
    PHASE,
    PLANNED_GREEN,
    PLANNED_PHASE,
    advance,
    document_path,
    ledger,
    method_context,
    row,
    seeded_applied_tree,
)

#: The whole edge table, spelled out so widening it is a deliberate edit.
PINNED_EDGES = frozenset(
    {
        ("task", "PLANNED", "CLAIMED", False),
        ("task", "CLAIMED", "RUNNING", False),
        ("task", "RUNNING", "COMPLETED", True),
        ("task", "CLAIMED", "COMPLETED", True),
        ("task", "DRAFT", "DROPPED", False),
        ("batch", "ACTIVE", "COMPLETED", False),
        ("milestone", "ACTIVE", "COMPLETED", False),
        ("milestone", "PLANNED", "CANCELLED", False),
    }
)


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MethodContext:
    """Return a daemon context whose runtime directory is this test's own."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    return method_context(runtime)


@pytest.fixture
def tree(tmp_path: Path) -> CutoverTree:
    """Return a fresh cutover caught with work in flight."""
    return seeded_applied_tree(tmp_path / "repo")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refused(answer: dict[str, Any], code: str) -> dict[str, Any]:
    """Return the one refusal row of *answer*, asserting its code."""
    assert answer["status"] == "error", answer
    error: dict[str, Any]
    (error,) = answer["errors"]
    assert error["code"] == code, error
    return error


def test_edge_table_is_the_declared_one() -> None:
    declared = {
        (edge.collection.value, edge.from_status, edge.to_status, edge.runs_gates)
        for edge in LEGACY_EDGES
    }
    assert declared == PINNED_EDGES


def test_task_walks_planned_claimed_running_with_payload_untouched(
    tree: CutoverTree, ctx: MethodContext
) -> None:
    before = row(tree, Epoch2Collection.TASK, PLANNED_GREEN)
    assert before is not None and before["status"] == "PLANNED"

    for to in ("CLAIMED", "RUNNING"):
        answer = advance(tree, ctx, key=PLANNED_GREEN, collection="task", to=to)
        assert answer["status"] == "ok", answer
        assert answer["result"]["to_status"] == to
        assert answer["result"]["compacted"] is False

    after = row(tree, Epoch2Collection.TASK, PLANNED_GREEN)
    assert after is not None
    assert after["status"] == "RUNNING"
    assert after["payload"] == before["payload"]
    moves = [(event["from_status"], event["to_status"]) for event in after["continuation"]]
    assert moves == [("PLANNED", "CLAIMED"), ("CLAIMED", "RUNNING")]
    assert all(event["actor"] == "OP-0001" for event in after["continuation"])


def test_backlog_row_drops_and_stays_in_the_document(tree: CutoverTree, ctx: MethodContext) -> None:
    answer = advance(tree, ctx, key=BACKLOG_ROW, collection="task", to="DROPPED")

    assert answer["status"] == "ok", answer
    assert answer["result"]["compacted"] is False
    dropped = row(tree, Epoch2Collection.TASK, BACKLOG_ROW)
    assert dropped is not None and dropped["status"] == "DROPPED"
    assert all(line.record_key != BACKLOG_ROW for line in ledger(tree, Epoch2Collection.TASK))


@pytest.mark.parametrize(
    ("key", "collection", "to"),
    [
        (PLANNED_GREEN, "task", "COMPLETED"),
        (PLANNED_GREEN, "task", "RUNNING"),
        (CLAIMED_GREEN, "task", "PLANNED"),
        (BACKLOG_ROW, "task", "COMPLETED"),
        (ITER, "batch", "CANCELLED"),
        (PHASE, "milestone", "CANCELLED"),
        (PLANNED_PHASE, "milestone", "COMPLETED"),
    ],
)
def test_off_table_edge_is_refused_and_writes_nothing(
    tree: CutoverTree, ctx: MethodContext, key: str, collection: str, to: str
) -> None:
    before = _digest(document_path(tree))

    error = _refused(
        advance(tree, ctx, key=key, collection=collection, to=to), "legacy_edge_refused"
    )

    assert error["entity_ref"] == f"legacy:{collection}/{key}"
    assert _digest(document_path(tree)) == before


def test_batch_close_is_refused_while_a_task_is_open(tree: CutoverTree, ctx: MethodContext) -> None:
    before = _digest(document_path(tree))

    error = _refused(
        advance(tree, ctx, key=ITER, collection="batch", to="COMPLETED"),
        "transition_guard_failed",
    )

    assert error["guard"] == "task_terminal"
    assert CLAIMED_GREEN in error["message"]
    assert _digest(document_path(tree)) == before


def test_milestone_close_is_refused_while_a_batch_is_open(
    tree: CutoverTree, ctx: MethodContext
) -> None:
    error = _refused(
        advance(tree, ctx, key=PHASE, collection="milestone", to="COMPLETED"),
        "transition_guard_failed",
    )

    assert error["guard"] == "batch_terminal"


def test_milestone_cancel_needs_a_recorded_plan_ref(tree: CutoverTree, ctx: MethodContext) -> None:
    unproven = advance(
        tree, ctx, key=PLANNED_PHASE, collection="milestone", to="CANCELLED", evidence_refs=("D99",)
    )
    assert _refused(unproven, "transition_guard_failed")["guard"] == "artifact_or_decision_ref"
    assert row(tree, Epoch2Collection.MILESTONE, PLANNED_PHASE) is not None

    answer = advance(
        tree, ctx, key=PLANNED_PHASE, collection="milestone", to="CANCELLED", evidence_refs=("D01",)
    )

    assert answer["status"] == "ok", answer
    assert answer["result"]["compacted"] is True
    assert row(tree, Epoch2Collection.MILESTONE, PLANNED_PHASE) is None
    (line,) = [
        item
        for item in ledger(tree, Epoch2Collection.MILESTONE)
        if item.record_key == PLANNED_PHASE
    ]
    assert line.status == "CANCELLED"
    assert line.payload["origin"]["source_id"] == PLANNED_PHASE
    assert line.payload["continuation"][0]["evidence_refs"] == ["D01"]

    again = advance(
        tree, ctx, key=PLANNED_PHASE, collection="milestone", to="CANCELLED", evidence_refs=("D01",)
    )
    _refused(again, "legacy_edge_refused")


def test_unknown_record_is_not_found(tree: CutoverTree, ctx: MethodContext) -> None:
    _refused(
        advance(tree, ctx, key="P99-I01-W01", collection="task", to="CLAIMED"),
        "identity_not_found",
    )


def test_malformed_request_is_a_schema_refusal(tree: CutoverTree, ctx: MethodContext) -> None:
    answer = advance(tree, ctx, key=PLANNED_GREEN, collection="track", to="CLAIMED")

    _refused(answer, "schema_validation_failed")


def test_cli_verbs_forward_to_registered_daemon_methods() -> None:
    from eawf.runtime.daemon import methods
    from eawf.surfaces.cli.commands.domain_legacy import LEGACY_ADVANCE, RECORD_APPEND

    methods.ensure_all_methods_registered()

    assert {LEGACY_ADVANCE, RECORD_APPEND} <= set(methods.registered_methods())
