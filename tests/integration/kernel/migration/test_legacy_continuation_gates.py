"""Completing an imported Task runs its decisive gates, and a red one stops it.

The gates are the ones the Task's source wave declared, carried through the
import in its payload, and they run through the same out-of-process runner
a wave close uses. Each case plants the outcome in the repository the gate
runs against -- a passing suite, a failing one, a suite that is missing --
so what decides the completion is a child interpreter's exit status, never
a receipt the caller presented. Every receipt lands in the generation's
receipt ledger; the fenced epoch-1 document and its gate-receipt store are
never touched.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.continuation import receipt_digest
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods import MethodContext
from tests.integration.kernel.migration._cutover_harness import CutoverTree
from tests.integration.kernel.migration._legacy_continuation import (
    CLAIMED_GREEN,
    GREEN_FILE,
    RUNNING_RED,
    RUNNING_UNGATED,
    advance,
    ledger,
    method_context,
    row,
    seeded_applied_tree,
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


def _refused(answer: dict[str, Any]) -> dict[str, Any]:
    """Return the one gate refusal of *answer*."""
    assert answer["status"] == "error", answer
    error: dict[str, Any]
    (error,) = answer["errors"]
    assert error["code"] == "transition_guard_failed", error
    return error


def test_completion_runs_the_gate_and_binds_its_receipt(
    tree: CutoverTree, ctx: MethodContext
) -> None:
    epoch1 = tree.target_root / "state.json"
    epoch1_before = hashlib.sha256(epoch1.read_bytes()).hexdigest()

    answer = advance(tree, ctx, key=CLAIMED_GREEN, collection="task", to="COMPLETED")

    assert answer["status"] == "ok", answer
    (binding,) = answer["result"]["gate_receipts"]
    assert binding["gate_id"] == "G-01"
    assert binding["status"] == "pass"
    (receipt,) = ledger(tree, Epoch2Collection.RECEIPT)
    assert receipt.record_key == binding["receipt_id"]
    assert receipt_digest(receipt.payload) == binding["receipt_digest"]
    assert receipt.payload["exit_status"] == 0
    assert receipt.payload["argv"][:2] == ["pytest", GREEN_FILE]
    assert receipt.payload["subject"] == f"legacy:task/{CLAIMED_GREEN}"

    assert row(tree, Epoch2Collection.TASK, CLAIMED_GREEN) is None
    (completed,) = [
        line for line in ledger(tree, Epoch2Collection.TASK) if line.record_key == CLAIMED_GREEN
    ]
    assert completed.status == "COMPLETED"
    (event,) = completed.payload["continuation"]
    assert event["gate_receipts"] == [binding]

    assert hashlib.sha256(epoch1.read_bytes()).hexdigest() == epoch1_before
    assert not (tree.target_root / "store" / "gate_receipt.jsonl").exists()


def test_red_gate_refuses_and_leaves_the_task_running(
    tree: CutoverTree, ctx: MethodContext
) -> None:
    error = _refused(advance(tree, ctx, key=RUNNING_RED, collection="task", to="COMPLETED"))

    assert error["guard"] == "G-01"
    assert "decisive gate G-01 did not pass" in error["message"]
    held = row(tree, Epoch2Collection.TASK, RUNNING_RED)
    assert held is not None
    assert held["status"] == "RUNNING"
    assert "continuation" not in held
    (receipt,) = ledger(tree, Epoch2Collection.RECEIPT)
    assert receipt.status == "fail"
    assert receipt.payload["exit_status"] != 0


def test_a_gate_that_now_fails_is_rerun_rather_than_trusted(
    tree: CutoverTree, ctx: MethodContext
) -> None:
    (tree.target_root.parent / GREEN_FILE).unlink()

    error = _refused(advance(tree, ctx, key=CLAIMED_GREEN, collection="task", to="COMPLETED"))

    assert error["guard"] == "G-01"
    held = row(tree, Epoch2Collection.TASK, CLAIMED_GREEN)
    assert held is not None and held["status"] == "CLAIMED"


def test_caller_supplied_receipts_alone_are_refused(tree: CutoverTree, ctx: MethodContext) -> None:
    answer = advance(
        tree,
        ctx,
        key=RUNNING_UNGATED,
        collection="task",
        to="COMPLETED",
        evidence_refs=("GR-00000000000000000000000000000000",),
    )

    error = _refused(answer)
    assert error["guard"] == "decisive_gates_absent"
    assert "caller-supplied receipts" in error["message"]
    held = row(tree, Epoch2Collection.TASK, RUNNING_UNGATED)
    assert held is not None and held["status"] == "RUNNING"
    assert ledger(tree, Epoch2Collection.RECEIPT) == ()
