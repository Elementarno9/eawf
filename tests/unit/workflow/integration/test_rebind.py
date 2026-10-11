"""Which Batch a ledger row is rebound through, and what a document still names."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.content import StoredContent
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.workflow.integration.rebind import (
    RebindDropError,
    document_mentions,
    only_commits_rebound,
    plan_rebind,
    row_batch,
)
from eawf.workflow.integration.squash import SquashMap

TASKS = {"EAWF-0042": "BAT-0007"}
OLD, NEW = "a" * 40, "b" * 40


def _record(collection: Epoch2Collection, key: str, payload: dict[str, Any]) -> LedgerRecord:
    return LedgerRecord(
        collection=collection,
        record_key=key,
        status="x",
        recorded_at=datetime(2026, 10, 8, tzinfo=UTC),
        payload=payload,
    )


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_record(Epoch2Collection.BATCH, "BAT-0007", {"key": "BAT-0007"}), "BAT-0007"),
        (
            _record(
                Epoch2Collection.BATCH, "BMR-1-BAT-0008", {"batch_ref": "eawf://P/batch/BAT-0008"}
            ),
            "BAT-0008",
        ),
        (_record(Epoch2Collection.TASK, "EAWF-0042", {"key": "EAWF-0042"}), "BAT-0007"),
        (
            _record(Epoch2Collection.RECEIPT, "PRF-1", {"task_ref": "eawf://P/task/EAWF-0042"}),
            "BAT-0007",
        ),
        (_record(Epoch2Collection.RECEIPT, "PRF-2", {"task_ref": "eawf://P/task/EAWF-9999"}), None),
        (_record(Epoch2Collection.EVIDENCE, "EV-1", {}), None),
    ],
    ids=["batch-row", "batch-ref", "task-row", "receipt-task", "unplaced-task", "no-ref"],
)
def test_row_batch_reads_the_ref_the_row_carries(
    record: LedgerRecord, expected: str | None
) -> None:
    assert row_batch(record, TASKS) == expected


def test_document_mentions_only_bindings_to_source_commits() -> None:
    squash_map = SquashMap(
        branch="b", base="c" * 40, source_head=OLD, head=NEW, commits={OLD: NEW}, batches={}
    )

    assert document_mentions({}, squash_map) == ()
    assert document_mentions({"batch": {"X": {"head": NEW, "digest": "d" * 64}}}, squash_map) == ()
    assert document_mentions({"batch": {"X": {"binding": {"head_sha": OLD}}}}, squash_map) == (OLD,)
    assert document_mentions({"task": {"T": {"refs": [f"commit:{OLD}"]}}}, squash_map) == (OLD,)


def test_document_mentions_pass_over_a_sha_quoted_in_prose() -> None:
    squash_map = SquashMap(
        branch="b", base="c" * 40, source_head=OLD, head=NEW, commits={OLD: NEW}, batches={}
    )
    brief = {"campaign": {"CAM-1": {"brief": {"text": f"the population: {OLD}, and more"}}}}

    assert document_mentions(brief, squash_map) == ()


# ---- a map entry naming no commit drops the reference ---------------------------

ORPHAN = "c" * 40
REASON = "quotes a commit no published branch carries"


def _orphan_map(**overrides: Any) -> SquashMap:
    fields: dict[str, Any] = {
        "branch": "b",
        "base": "d" * 40,
        "source_head": NEW,
        "head": NEW,
        "commits": {ORPHAN: None},
        "dropped": {ORPHAN: REASON},
        "batches": {},
    }
    return SquashMap.model_validate({**fields, **overrides})


def _plan(tmp_path: Path, collection: Epoch2Collection, payload: dict[str, Any]) -> Any:
    state = tmp_path / "state.json"
    append_ledger_record(ledger_path(state, collection), _record(collection, "K-1", payload))
    return plan_rebind(
        state,
        _orphan_map(),
        {},
        trees={},
        parents={},
        recorded_at=datetime(2026, 10, 9, tzinfo=UTC),
    )


def test_a_dropped_commit_withholds_the_content_line_quoting_it(tmp_path: Path) -> None:
    content = {
        "payload_kind": "content_artifact",
        "artifact_ref": "artifact://content/run-00000001/" + "e" * 64,
        "run_ref": "eawf://EAWF/run/RUN-00000001",
        "lines": ["first", f'    ("P22-I01-W05", "{ORPHAN}"),', "last"],
        "total_lines": 3,
        "withheld_lines": 0,
        "recorded_at": "2026-10-06T23:14:44Z",
    }

    (row,) = _plan(tmp_path, Epoch2Collection.ARTIFACT, content)

    stored = StoredContent.model_validate(row.record.payload)
    assert stored.lines[0] == "first" and stored.lines[2] == "last"
    assert ORPHAN not in stored.lines[1] and REASON in stored.lines[1]
    assert stored.withheld_lines == 1
    assert row.rebinds == {ORPHAN: None}


def test_a_dropped_commit_withholds_receipt_text_and_match_lines(tmp_path: Path) -> None:
    payload = {
        "result": {
            "bounded_output": {
                "text": f"a\nsee {ORPHAN} here\nb",
                "matches": [{"path": "x.py", "line": 3, "text": f"x = '{ORPHAN}'"}],
            }
        }
    }

    (row,) = _plan(tmp_path, Epoch2Collection.RECEIPT, payload)

    output = row.record.payload["result"]["bounded_output"]
    assert output["text"].splitlines()[0::2] == ["a", "b"]
    assert REASON in output["text"].splitlines()[1]
    assert ORPHAN not in str(row.record.payload)
    assert REASON in output["matches"][0]["text"]


def test_a_dropped_commit_cannot_leave_a_binding_without_its_head(tmp_path: Path) -> None:
    with pytest.raises(RebindDropError, match="head_sha"):
        _plan(tmp_path, Epoch2Collection.RECEIPT, {"binding": {"head_sha": ORPHAN}})


def test_the_map_pairs_every_dropped_commit_with_a_reason() -> None:
    with pytest.raises(ValidationError, match="reason"):
        _orphan_map(dropped={})
    with pytest.raises(ValidationError, match="reason"):
        _orphan_map(commits={ORPHAN: NEW})
    assert _orphan_map().dropped == {ORPHAN: REASON}


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ({}, {}, True),
        ({"head_sha": OLD}, {"head_sha": NEW}, True),
        (
            {"binding": {"head_sha": OLD, "tree_sha": OLD}},
            {"binding": {"head_sha": NEW, "tree_sha": NEW}},
            True,
        ),
        ({"reason": f"read at {OLD}"}, {"reason": f"read at {NEW}"}, True),
        ({"head_sha": OLD}, {"head_sha": NEW, "extra": 1}, False),
        ({"head_sha": OLD, "note": "x"}, {"head_sha": NEW, "note": "y"}, False),
        # one hex digit short of a commit SHA is content, not a commit
        ({"head_sha": OLD[:-1]}, {"head_sha": NEW[:-1]}, False),
        ({"digest": "sha256:" + "a" * 64}, {"digest": "sha256:" + "b" * 64}, False),
    ],
)
def test_only_commits_rebound_tells_a_rebind_from_any_other_change(
    before: dict[str, Any], after: dict[str, Any], expected: bool
) -> None:
    assert only_commits_rebound(before, after) is expected
