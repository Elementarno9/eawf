"""``eawf task drop`` retires a backlog row the epoch-2 cutover imported.

An imported epoch-1 backlog row is keyed by its epoch-1 id, such as
``B012``, and wraps the source record rather than stating a native URN, so
the native drop edge cannot address it and the row stayed in the backlog
for good. These tests drive the real ``task drop`` command into the real
daemon handler, in process, over a finished cutover whose Task ledger has
had its legacy import sealed into a snapshot, and read the tree back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.continuation import ContinuationEvent
from eawf.kernel.projection.connection import read_method
from eawf.kernel.store.compaction import plan_legacy_seal, seal_legacy_import
from eawf.kernel.store.ledger import (
    LedgerRecord,
    ledger_snapshot,
    read_ledger_records,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import MethodContext
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from tests.integration.kernel.migration._cutover_harness import (
    APPLIED_AT,
    apply_once,
    declared_canary,
    staged_corpus,
)

pytestmark = pytest.mark.integration

runner = CliRunner()

#: The imported backlog row the tests retire, and the reason they give.
LEGACY_KEY: Final = "B012"
REASON: Final = "Delivered under P01-I01-W01; the draft is obsolete."


def _seed_backlog_row(corpus: Path) -> None:
    """Add one open backlog row ``B012`` to the staged epoch-1 corpus."""
    document_path = corpus / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document["backlog"][LEGACY_KEY] = {
        "created_at": "2026-01-01T00:00:00Z",
        "id": LEGACY_KEY,
        "scope_id": "DEMO",
        "status": "open",
        "title": f"Backlog {LEGACY_KEY}",
    }
    document_path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


class _Tree:
    """A finished cutover with its Task ledger's legacy import sealed."""

    def __init__(self, root: Path) -> None:
        corpus = staged_corpus(root)
        _seed_backlog_row(corpus)
        self.target_root = declared_canary(root / ".ea")
        result = apply_once(corpus=corpus, target_root=self.target_root, applied_at=APPLIED_AT)
        self.repo_root = root
        self.document = self.target_root / "generations" / result.generation_id / "state.json"
        self.ledger = ledger_path(self.document, Epoch2Collection.TASK)
        plan = plan_legacy_seal(self.ledger)
        assert plan is not None, "the cutover filed imported Tasks in the ledger"
        seal_legacy_import(self.document, collection=Epoch2Collection.TASK, plan=plan)
        snapshot = ledger_snapshot(self.ledger)
        assert snapshot is not None
        self.snapshot = snapshot
        self.ctx = MethodContext(
            started_at=APPLIED_AT.isoformat(),
            pid=1,
            protocol_version="1",
            version="test",
            wal_dir=root / "runtime" / "wal",
        )

    def task_rows(self) -> dict[str, Any]:
        rows: dict[str, Any] = json.loads(self.document.read_text(encoding="utf-8"))["task"]
        return rows

    def lines(self) -> tuple[LedgerRecord, ...]:
        return read_ledger_records(self.ledger)

    def backlog_keys(self) -> set[str]:
        answer = asyncio.run(
            methods.dispatch(read_method("backlog"), self.ctx, {"repo_root": str(self.repo_root)})
        )
        return {row["key"] for row in answer["rows"]}


class _InProcessClient:
    """A daemon client that answers through the daemon's own dispatcher."""

    def __init__(self, ctx: MethodContext) -> None:
        self._ctx = ctx

    def __enter__(self) -> _InProcessClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(methods.dispatch(method, self._ctx, params))


def _no_escalate(verb: str, *, flags: object = None, runtime_dir: object = None) -> int:
    """Stand in for the gate that would spawn a real daemon."""
    return 0


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Tree:
    built = _Tree(tmp_path / "repo")
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", _no_escalate)
    monkeypatch.setattr(domain_cmd, "DaemonClient", lambda *a, **k: _InProcessClient(built.ctx))
    return built


def _drop(
    tree: _Tree, *, key: str, revision: int = 1, reason: str = REASON
) -> tuple[int, dict[str, Any]]:
    """Run ``eawf task drop`` on *key* and return its exit code and envelope."""
    result = runner.invoke(
        app,
        [
            "--json",
            "--workspace",
            str(tree.repo_root),
            "task",
            "drop",
            key,
            "--reason",
            reason,
            "--expected-revision",
            str(revision),
            "--idempotency-key",
            f"drop-{key}-{revision}",
            "--actor",
            "OPERATOR",
            "--yes",
        ],
    )
    return result.exit_code, json.loads(result.stdout)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_task_drop_retires_an_imported_backlog_key(tree: _Tree) -> None:
    assert tree.task_rows()[LEGACY_KEY]["status"] == "DRAFT"
    sealed_before = _sha256(tree.snapshot)
    before = tree.lines()

    code, envelope = _drop(tree, key=LEGACY_KEY)

    assert code == 0, envelope
    assert envelope["status"] == "ok"
    assert (envelope["revision_before"], envelope["revision_after"]) == (1, 2)
    after = tree.lines()
    assert after[: len(before)] == before
    (appended,) = after[len(before) :]
    assert (appended.record_key, appended.status) == (LEGACY_KEY, "DROPPED")
    assert [line.record_key for line in after].count(LEGACY_KEY) == 1
    event = ContinuationEvent.model_validate(appended.payload["continuation"][-1])
    assert (event.from_status, event.to_status) == ("DRAFT", "DROPPED")
    assert (event.reason, event.actor) == (REASON, "OPERATOR")
    assert _sha256(tree.snapshot) == sealed_before
    assert ledger_snapshot(tree.ledger) == tree.snapshot


def test_a_retired_legacy_key_leaves_the_backlog_once(tree: _Tree) -> None:
    assert LEGACY_KEY in tree.backlog_keys()

    first, _ = _drop(tree, key=LEGACY_KEY)

    assert first == 0
    assert LEGACY_KEY not in tree.backlog_keys()
    assert LEGACY_KEY not in tree.task_rows()
    ledger_before, document_before = tree.ledger.read_bytes(), tree.document.read_bytes()

    second, refusal = _drop(tree, key=LEGACY_KEY, revision=2)

    assert second != 0
    assert refusal["status"] == "error"
    assert [row["code"] for row in refusal["errors"]] == ["legacy_edge_refused"]
    assert tree.ledger.read_bytes() == ledger_before
    assert tree.document.read_bytes() == document_before


def test_a_retried_legacy_drop_replays_its_receipt(tree: _Tree) -> None:
    """A retry under the same key and parameters answers the first commit's receipt."""
    first_code, first = _drop(tree, key=LEGACY_KEY)
    ledger_before, document_before = tree.ledger.read_bytes(), tree.document.read_bytes()

    retry_code, retry = _drop(tree, key=LEGACY_KEY)

    assert (first_code, retry_code) == (0, 0), retry
    assert retry["status"] == "ok"
    assert retry["result"] == first["result"]
    assert tree.ledger.read_bytes() == ledger_before
    assert tree.document.read_bytes() == document_before


def test_a_legacy_drop_key_reused_for_other_parameters_conflicts(tree: _Tree) -> None:
    """Error path: the same key with another reason is not a retry of the first drop."""
    _drop(tree, key=LEGACY_KEY)
    ledger_before = tree.ledger.read_bytes()

    code, refusal = _drop(tree, key=LEGACY_KEY, reason="Another reason entirely.")

    assert code != 0
    assert [row["code"] for row in refusal["errors"]] == ["idempotency_conflict"]
    assert tree.ledger.read_bytes() == ledger_before


def test_a_legacy_drop_at_a_stale_revision_moves_nothing(tree: _Tree) -> None:
    """Error path: the revision the caller read is the compare-and-swap token."""
    before = tree.lines()

    code, refusal = _drop(tree, key=LEGACY_KEY, revision=2)

    assert code != 0
    assert [row["code"] for row in refusal["errors"]] == ["revision_conflict"]
    assert refusal["revision_before"] == 1
    assert tree.lines() == before
    assert tree.task_rows()[LEGACY_KEY]["status"] == "DRAFT"


def test_a_legacy_key_the_tree_never_imported_is_not_found(tree: _Tree) -> None:
    """Error path: a key neither the document nor the ledger holds."""
    before = tree.lines()

    code, refusal = _drop(tree, key="B999")

    assert code != 0
    assert [row["code"] for row in refusal["errors"]] == ["identity_not_found"]
    assert tree.lines() == before


def test_an_imported_row_already_dropped_at_the_cut_is_refused(tree: _Tree) -> None:
    """Boundary: a closed epoch-1 row imported as DROPPED takes no second drop."""
    dropped = next(
        key
        for key, row in tree.task_rows().items()
        if key.startswith("B") and row["status"] == "DROPPED"
    )
    before = tree.lines()

    code, refusal = _drop(tree, key=dropped)

    assert code != 0
    assert [row["code"] for row in refusal["errors"]] == ["legacy_edge_refused"]
    assert tree.lines() == before


def test_a_legacy_drop_addressed_by_its_projection_ref_retires_the_row(tree: _Tree) -> None:
    """Boundary: the ``legacy:task/<key>`` name the backlog shows addresses the row too."""
    code, envelope = _drop(tree, key=f"legacy:task/{LEGACY_KEY}")

    assert code == 0, envelope
    assert LEGACY_KEY not in tree.task_rows()
    assert tree.lines()[-1].record_key == LEGACY_KEY


def test_the_appended_row_keeps_the_imported_record_byte_for_byte(tree: _Tree) -> None:
    imported = tree.task_rows()[LEGACY_KEY]["payload"]

    _drop(tree, key=LEGACY_KEY)

    assert tree.lines()[-1].payload["payload"] == imported
