"""The console draws the record's own facts where it once drew unknown tokens.

A Task frame states each criterion with the newest receipt proving it, the Run it names
and the commit it was integrated at; a Batch frame counts the Runs of its Tasks; the
Attention buckets count failed, running, over-budget and rejected work from the records
that state it and name in words the one bucket no record feeds; the transcript states how
its Run ended and what it cost; and the unattended rows state the queue the daemon read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.economics.spend import RunUsageView
from eawf.kernel.projection.attention import (
    LOST_HOLE_REASON,
    AttentionBucket,
    BucketSource,
    build_attention_view,
)
from eawf.kernel.projection.compute import (
    CRITERION_FACT,
    PROOF_RECEIPT_KIND,
    RUN_STATE_KIND,
    VERDICT_OBSERVATION_KIND,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.operations import (
    NOT_QUEUED,
    NOT_STARTED,
    QUEUE_UNREAD,
    build_operations_view,
)
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.projection.spine import (
    NO_CANDIDATE_REASON,
    UNPRODUCED_REASON,
    build_spine_view,
)
from eawf.kernel.projection.transcript import RUN_NOT_ENDED, build_transcript_view
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.runtime.dispatch_queue import (
    DispatchControl,
    DispatchPlan,
    DispatchQueueView,
    QueuedRun,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods.delivery_completion import PROOF_PAYLOAD_KIND
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.renderers import attention as attention_renderer
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.batch_detail import NO_BRANCH
from eawf.surfaces.tui.console.renderers.budget_lines import cost_line
from eawf.surfaces.tui.console.renderers.read_model import UNKNOWN_WORD
from eawf.surfaces.tui.console.session import Session

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
ROOT = "eawf://EAWF/EAWF/EAWF"
HEAD = "8634be4" + "0" * 33


def _urn(kind: str, key: str) -> str:
    return f"{ROOT}/{kind}/{key}"


def _row(kind: str, key: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "urn": _urn(kind, key), "revision": 1, "status": status, **extra}


def _criterion(ident: str, text: str, gates: list[str]) -> dict[str, Any]:
    return {"id": ident, "text": text, "kind": "behavioral", "gate_ids": gates}


DOCUMENT: dict[str, Any] = {
    "batch": {
        "BAT-0100": _row(
            "batch", "BAT-0100", "ACTIVE", milestone_ref=_urn("milestone", "MLS-0100")
        ),
    },
    "task": {
        "TSK-0001": _row(
            "task",
            "TSK-0001",
            "COMPLETED",
            intent="Bound the replay",
            batch_ref=_urn("batch", "BAT-0100"),
            active_run_ref=_urn("run", "RUN-00000002"),
            integrated_binding={"head_sha": HEAD},
            criteria=[
                _criterion("CR-001", "the replay is bounded", ["G-01"]),
                _criterion("CR-002", "the digest is equal", []),
            ],
        ),
        "TSK-0002": _row(
            "task", "TSK-0002", "RUNNING", intent="Seal", batch_ref=_urn("batch", "BAT-0100")
        ),
    },
    "run": {
        "RUN-00000001": _row(
            "run",
            "RUN-00000001",
            "RUNNING",
            created_at="2026-09-17T09:00:00Z",
            scope={"purpose": "implement", "task_ref": _urn("task", "TSK-0002")},
        ),
        "RUN-00000002": _row(
            "run",
            "RUN-00000002",
            "FAILED",
            created_at="2026-09-17T10:00:00Z",
            failure={"code": "gate_red", "message": "gate G-01 went red"},
            scope={"purpose": "implement", "task_ref": _urn("task", "TSK-0001")},
        ),
    },
}


def _proof(key: str, *, criteria: list[str], result: str, ended: str) -> dict[str, Any]:
    """Return a proof receipt as the daemon lists it for a Task frame."""
    return {
        "payload_kind": PROOF_PAYLOAD_KIND,
        "key": key,
        "urn": _urn("task", "TSK-0001"),
        "revision": 1,
        "status": result,
        "task_ref": _urn("task", "TSK-0001"),
        "receipt": {
            "id": f"RCP-{key[-4:]}",
            "gate_id": "G-01",
            "criterion_ids": criteria,
            "result": result,
            "ended_at": ended,
        },
    }


PROOFS = (
    _proof("PRF-0001", criteria=["CR-001"], result="fail", ended="2026-09-17T10:00:00Z"),
    _proof("PRF-0002", criteria=["CR-001"], result="pass", ended="2026-09-17T11:00:00Z"),
)


def _projection(
    route: str,
    document: dict[str, Any] | None = None,
    ledger_rows: dict[Epoch2Collection, tuple[dict[str, Any], ...]] | None = None,
) -> RouteProjection:
    return build_route_projection(
        route=route,
        document=DOCUMENT if document is None else document,
        cursor=41208,
        scope_id=SCOPE,
        generated_at=AT,
        ledger_rows=ledger_rows or {},
    )


def _frame(route: str, subject: str | None, model: Any, **live: object) -> str:
    session = Session()
    session.route = route
    session.subj_id = subject
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=160,
        h=40,
        projection=model,
        live=live,
    )
    return "\n".join(render_route(view))


# ---------- a Task states its criteria, its proof, its Run and its commit ----------


def test_a_task_row_states_each_criterion_and_its_integrated_commit() -> None:
    task = next(r for r in _projection("task.detail").rows if r.key == "TSK-0001")
    assert task.facts[f"{CRITERION_FACT}1"] == (
        "CR-001 · behavioral · gates G-01 · the replay is bounded"
    )
    assert task.facts[f"{CRITERION_FACT}2"] == "CR-002 · behavioral · no gate · the digest is equal"
    assert task.facts["integrated"] == HEAD
    assert task.facts["run"] == "RUN-00000002"


def test_the_task_criteria_column_is_produced_and_candidates_name_their_missing_producer() -> None:
    view = build_spine_view(_projection("task.detail"))
    task = view.rows[view.index_of("TSK-0001") or 0]
    assert task.field("criteria").state is TruthState.KNOWN
    assert task.field("criteria").value == "2"
    assert view.unproduced() == ("candidates",)
    assert task.field("candidates").missing_reason == NO_CANDIDATE_REASON


def test_the_task_frame_draws_each_criterion_with_its_newest_receipt() -> None:
    projection = _projection("task.detail", ledger_rows={Epoch2Collection.RECEIPT: PROOFS})
    frame = _frame("task.detail", "TSK-0001", build_spine_view(projection))
    assert "CR-001 · behavioral · gates G-01 · pass RCP-0002 · the replay is bounded" in frame
    assert "CR-002 · behavioral · no gate · ∅ no receipt · the digest is equal" in frame
    assert "1 of 2 criteria pass on their newest receipt · 2 receipts filed" in frame
    assert "LAST RUN" in frame and "RUN-00000002" in frame
    assert "INTEGRATED   8634be4" in frame
    assert NO_CANDIDATE_REASON in frame
    assert UNPRODUCED_REASON not in frame


def test_a_proof_receipt_is_a_notice_that_names_its_task_and_criteria() -> None:
    projection = _projection("task.detail", ledger_rows={Epoch2Collection.RECEIPT: PROOFS})
    proofs = [r for r in projection.rows if r.collection is Epoch2Collection.RECEIPT]
    assert [p.facts["kind"] for p in proofs] == [PROOF_RECEIPT_KIND] * 2
    assert proofs[1].facts["task"] == "TSK-0001"
    assert proofs[1].facts["criteria"] == "CR-001"
    assert PROOF_RECEIPT_KIND == PROOF_PAYLOAD_KIND


def test_a_task_with_no_receipt_says_none_is_filed() -> None:
    frame = _frame("task.detail", "TSK-0002", build_spine_view(_projection("task.detail")))
    assert "∅ no proof receipt is filed for this Task" in frame
    assert "ACTIVE RUN" in frame
    assert "∅ not integrated" in frame


# ---------- a Batch counts the Runs of its Tasks and names why no pull request is read ----------


def test_a_batch_states_how_many_runs_ran_its_tasks() -> None:
    view = build_spine_view(_projection("batch.detail"))
    batch = view.rows[view.index_of("BAT-0100") or 0]
    assert batch.field("runs").value == "2"
    frame = _frame("batch.detail", "BAT-0100", view)
    assert "2 runs · 1 running · newest RUN-00000002" in frame
    assert NO_BRANCH in frame


def test_a_batch_with_no_run_says_none_is_held() -> None:
    document = {**DOCUMENT, "run": {}}
    view = build_spine_view(_projection("batch.detail", document))
    assert view.rows[view.index_of("BAT-0100") or 0].field("runs").value == "0"
    assert "∅ no Run of its Tasks is held" in _frame("batch.detail", "BAT-0100", view)


# ---------- Attention counts what the records state ----------


def _attention_rows() -> dict[Epoch2Collection, tuple[dict[str, Any], ...]]:
    runs = DOCUMENT["run"]
    return {
        Epoch2Collection.RUN: (
            {**runs["RUN-00000001"], "payload_kind": RUN_STATE_KIND},
            {**runs["RUN-00000002"], "payload_kind": RUN_STATE_KIND},
            {
                "payload_kind": "child_ceiling_breach",
                "key": "CHILD-CEILING-RUN-00000003",
                "urn": _urn("run", "RUN-00000001"),
                "revision": 1,
                "status": "breached",
            },
        ),
        Epoch2Collection.BATCH: (
            {
                "payload_kind": VERDICT_OBSERVATION_KIND,
                "key": "BAT-0100-AUD-1",
                "urn": _urn("batch", "BAT-0100"),
                "revision": 1,
                "status": "verified_false",
                "subject": "CR-001",
                "batch_ref": _urn("batch", "BAT-0100"),
                "verdict": "verified_false",
            },
        ),
    }


def _attention() -> Any:
    return build_register_view(_projection("attention", {}, _attention_rows()))


def test_attention_counts_failed_active_over_budget_and_rejected_from_their_records() -> None:
    view = build_attention_view(_attention())
    placed = {i.key: i.bucket for i in view.items}
    assert placed == {
        "RUN-00000002": AttentionBucket.FAILED,
        "RUN-00000001": AttentionBucket.ACTIVE,
        "CHILD-CEILING-RUN-00000003": AttentionBucket.OVER_BUDGET,
        "BAT-0100-AUD-1": AttentionBucket.REJECTED,
    }
    assert all(i.read_only for i in view.items)
    counts = {c.bucket: c for c in view.bucket_counts() if c.need is None}
    for bucket in (
        AttentionBucket.FAILED,
        AttentionBucket.ACTIVE,
        AttentionBucket.OVER_BUDGET,
        AttentionBucket.REJECTED,
    ):
        assert (counts[bucket].count, counts[bucket].source) == (1, BucketSource.DERIVED)
    assert counts[AttentionBucket.LOST].source is BucketSource.HOLE
    assert counts[AttentionBucket.LOST].reason == LOST_HOLE_REASON


def test_the_rail_states_counts_and_draws_no_hole() -> None:
    rail = attention_renderer.rail_lines(_attention(), None, notices=2)
    by_label = {line[1:25].strip(): line[25:].strip() for line in rail[1:]}
    assert by_label["failed"] == "1"
    assert by_label["active"] == "1"
    assert by_label["rejected"] == "1"
    # a ceiling breach and two budget notices are all over budget
    assert by_label["over budget"] == "3"
    # a Run that stopped answering is counted under stalled, never under a second name
    assert "lost" not in by_label
    assert UNKNOWN_WORD not in "".join(by_label.values())


def _attention_frame(bucket: str | None) -> str:
    session = Session()
    session.route = "attention"
    session.bucket = bucket
    register = _attention()
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=160,
        h=40,
        register=register,
        attention=register,
    )
    return "\n".join(render_route(view))


def test_the_frame_offers_no_lost_bucket_beside_stalled() -> None:
    frame = _attention_frame(None)
    assert "stalled" in frame
    assert "lost" not in frame


def test_a_running_run_is_listed_only_once_its_bucket_is_chosen() -> None:
    unfiltered = _attention_frame(None)
    assert " ACTIVE " not in unfiltered
    assert " FAILED  1" in unfiltered
    assert " ACTIVE  1" in _attention_frame(AttentionBucket.ACTIVE.value)


def test_a_failed_run_lists_its_reason_and_its_task() -> None:
    register = _attention()
    failed = next(r for r in register.rows if r.key == "RUN-00000002")
    assert failed.facts["subject"] == "TSK-0001"
    assert failed.facts["question"] == "failed · gate G-01 went red"


# ---------- the transcript states its Run's outcome and cost ----------


@pytest.mark.parametrize(
    ("run", "outcome"),
    [
        ("RUN-00000002", "FAILED · gate G-01 went red"),
        ("RUN-00000001", RUN_NOT_ENDED),
    ],
)
def test_the_transcript_foot_states_the_runs_outcome(run: str, outcome: str) -> None:
    model = build_transcript_view(_projection("transcript"))
    assert model.unproduced() == ()
    frame = _frame("transcript", run, model)
    assert outcome in frame
    assert "no producer reports" not in frame
    assert f"cost {UNKNOWN_WORD} · usage not read yet" in frame


def test_the_transcript_foot_states_the_cost_the_usage_read_answered() -> None:
    usage = RunUsageView(
        run_key="RUN-00000002",
        tokens=1200,
        cost_microusd=4_620_000,
        quality="measured",
        cap_tokens=None,
        cap_cost_microusd=20_000_000,
        wall_seconds=None,
        typical_seconds=None,
        typical_runs=0,
    )
    model = build_transcript_view(_projection("transcript"))
    frame = _frame("transcript", "RUN-00000002", model, usage=usage)
    assert f"OUTCOME   FAILED · gate G-01 went red · {cost_line(4_620_000, 20_000_000)}" in frame


# ---------- the unattended rows state the queue the daemon read ----------


def _queue(*runs: QueuedRun) -> DispatchQueueView:
    return DispatchQueueView(
        runs=runs,
        plan=DispatchPlan(slots=4, in_use=1),
        control=DispatchControl(),
        read_at=AT,
    )


def test_the_unattended_columns_name_the_read_they_wait_on_until_it_arrives() -> None:
    model = build_operations_view(_projection("unattended"))
    assert {spec.name for spec in model.unproduced()} == set()
    row = model.rows[0]
    assert row.field("queue_state").missing_reason == QUEUE_UNREAD
    assert row.field("progress").missing_reason == QUEUE_UNREAD


def test_the_unattended_columns_are_stated_from_the_dispatch_queue() -> None:
    queue = _queue(
        QueuedRun(run_key="RUN-00000001", state="RUNNING", started_at=AT),
        QueuedRun(run_key="RUN-00000002", state="QUEUED"),
    )
    model = build_operations_view(_projection("unattended"), queue=queue)
    first, second = model.rows[0], model.rows[1]
    assert (first.field("queue_state").value, first.field("progress").value) == (
        "RUNNING",
        "opaque",
    )
    assert second.field("queue_state").value == "QUEUED"
    assert second.field("progress").missing_reason == NOT_STARTED


def test_a_run_the_queue_does_not_hold_says_so() -> None:
    model = build_operations_view(_projection("unattended"), queue=_queue())
    assert model.rows[0].field("queue_state").missing_reason == NOT_QUEUED
