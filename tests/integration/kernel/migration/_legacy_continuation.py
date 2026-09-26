"""A cut-over tree with work still in flight, and a way to drive it.

The pinned corpus closed every phase before it was frozen, so a real apply
over it leaves nothing for a continuation to move. The suites here need
what the live repository will hold at its cut: an active phase, an active
iter inside it, and waves in each of the states a cut can catch -- planned,
claimed, running -- with the gates their epoch-1 specs declared. The seed
edits a copy of the corpus before the apply, so every row the suites move
was written by the production importer rather than by hand.

The gates are real ``pytest`` runs over two files planted at the
repository root, one that passes and one that fails, so a completion's
gate outcome is decided by a child interpreter exactly as a live close's
is.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Final

from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import MethodContext
from tests.integration.kernel.migration._cutover_harness import (
    APPLIED_AT,
    CutoverTree,
    apply_once,
    declared_canary,
    plan_over,
    staged_corpus,
)

ACTOR: Final = "OP-0001"
ADVANCE: Final = "domain.legacy.advance"
APPEND: Final = "domain.record.append"

#: The seeded scope: one active phase with one active iter holding three
#: gated waves, and one planned phase a cancel can take. The planned
#: phase's iter holds the one wave that declares no gate, which no edge can
#: ever complete, so it is kept out of the iter the suites close.
PHASE: Final = "P04"
PLANNED_PHASE: Final = "P05"
ITER: Final = "P04-I01"
PLANNED_ITER: Final = "P05-I01"
CLAIMED_GREEN: Final = "P04-I01-W01"
PLANNED_GREEN: Final = "P04-I01-W02"
RUNNING_RED: Final = "P04-I01-W03"
RUNNING_UNGATED: Final = "P05-I01-W01"
WAVES: Final = (CLAIMED_GREEN, PLANNED_GREEN, RUNNING_RED)
BACKLOG_ROW: Final = "B001"

GREEN_FILE: Final = "test_green.py"
RED_FILE: Final = "test_red.py"
_OPENED: Final = "2026-02-01T00:00:00Z"


def _criterion(gate_id: str) -> dict[str, Any]:
    """Return one deterministic criterion scored by *gate_id*."""
    return {
        "id": "CR-01",
        "text": "Gate-fire proof: the planted suite exits zero under pytest",
        "kind": "behavioral",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "gate_ids": [gate_id],
        "quality_dimension": "reliability",
        "measurable_signal": "pytest exits zero over the planted suite",
    }


def _gate(test_file: str) -> dict[str, Any]:
    """Return one required, blocking gate running *test_file*."""
    return {
        "id": "G-01",
        "criterion_id": "CR-01",
        "kind": "command_exit_zero",
        "args": {"argv": ["pytest", test_file, "-q", "-p", "no:cacheprovider"]},
        "policy": "block",
        "cadence": "every-wave",
        "required": True,
    }


def _wave(wave_id: str, status: str, test_file: str | None) -> dict[str, Any]:
    """Return one seeded wave, gated on *test_file* when one is named."""
    wave: dict[str, Any] = {
        "id": wave_id,
        "iter_id": wave_id.rsplit("-", 1)[0],
        "opened_at": _OPENED,
        "status": status,
        "title": f"Land {wave_id}",
    }
    if test_file is not None:
        wave["success_criteria"] = [_criterion("G-01")]
        wave["gates"] = [_gate(test_file)]
    return wave


def seed_active_scope(document_path: Path) -> None:
    """Put an active phase, iter and in-flight waves into one corpus document."""
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document["phases"][PHASE] = {
        "id": PHASE,
        "opened_at": _OPENED,
        "scope_id": PHASE,
        "status": "active",
        "title": f"Deliver {PHASE}",
        "iter_ids": [ITER],
    }
    document["phases"][PLANNED_PHASE] = {
        "id": PLANNED_PHASE,
        "opened_at": _OPENED,
        "scope_id": PLANNED_PHASE,
        "status": "planned",
        "title": f"Deliver {PLANNED_PHASE}",
        "iter_ids": [PLANNED_ITER],
    }
    document["iters"][ITER] = {
        "id": ITER,
        "opened_at": _OPENED,
        "phase_id": PHASE,
        "status": "active",
        "title": f"Close {ITER}",
        "wave_ids": list(WAVES),
    }
    document["iters"][PLANNED_ITER] = {
        "id": PLANNED_ITER,
        "opened_at": _OPENED,
        "phase_id": PLANNED_PHASE,
        "status": "planned",
        "title": f"Close {PLANNED_ITER}",
        "wave_ids": [RUNNING_UNGATED],
    }
    document["waves"][CLAIMED_GREEN] = _wave(CLAIMED_GREEN, "claimed", GREEN_FILE)
    document["waves"][PLANNED_GREEN] = _wave(PLANNED_GREEN, "pending", GREEN_FILE)
    document["waves"][RUNNING_RED] = _wave(RUNNING_RED, "in_progress", RED_FILE)
    document["waves"][RUNNING_UNGATED] = _wave(RUNNING_UNGATED, "in_progress", None)
    document["current"] = {"phase_id": PHASE, "iter_id": ITER, "wave_id": CLAIMED_GREEN}
    document_path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def seeded_applied_tree(root: Path) -> CutoverTree:
    """Return a finished cutover over a corpus caught with work in flight.

    Args:
        root: A directory to build the corpus, the target and the
            repository the gates run in; the target tree is ``root/.ea``.

    Returns:
        The applied tree, with the passing and failing suites planted at
        ``root``.
    """
    corpus = staged_corpus(root)
    seed_active_scope(corpus / "document.json")
    target_root = declared_canary(root / ".ea")
    result = apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)
    (root / GREEN_FILE).write_text("def test_green():\n    assert True\n", encoding="utf-8")
    (root / RED_FILE).write_text("def test_red():\n    assert False\n", encoding="utf-8")
    return CutoverTree(
        corpus=corpus,
        target_root=target_root,
        generation_id=result.generation_id,
        plan=plan_over(corpus),
    )


def method_context(runtime_root: Path) -> MethodContext:
    """Return a daemon context whose WAL lives under *runtime_root*."""
    return MethodContext(
        started_at=APPLIED_AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=runtime_root / "wal",
    )


def call(tree: CutoverTree, method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Dispatch one native verb against *tree* and return its envelope."""
    wire = {"repo_root": str(tree.target_root.parent), **params}
    return asyncio.run(methods.dispatch(method, ctx, wire))


def advance(
    tree: CutoverTree,
    ctx: MethodContext,
    *,
    key: str,
    collection: str,
    to: str,
    evidence_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Ask for one legacy move on behalf of the test operator."""
    return call(
        tree,
        ADVANCE,
        ctx,
        key=key,
        collection=collection,
        to=to,
        reason="continue the imported record after the cutover",
        evidence_refs=list(evidence_refs),
        actor=ACTOR,
    )


def document_path(tree: CutoverTree) -> Path:
    """Return the selected generation's document."""
    return tree.target_root / "generations" / tree.generation_id / "state.json"


def row(tree: CutoverTree, collection: Epoch2Collection, key: str) -> dict[str, Any] | None:
    """Return one document row, or ``None`` when the document does not hold it."""
    return document_rows(read_document(document_path(tree)), collection).get(key)


def ledger(tree: CutoverTree, collection: Epoch2Collection) -> tuple[LedgerRecord, ...]:
    """Return every line of one generation ledger."""
    return read_ledger_records(ledger_path(document_path(tree), collection))
