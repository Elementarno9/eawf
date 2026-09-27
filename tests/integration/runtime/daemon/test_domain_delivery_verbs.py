"""The verbs that finish a Task, a Run and a Batch, driven against a canary.

The table-driven suite beside this one proves each verb commits once and
refuses once. This one covers what a table cannot: the integrated binding
a completion derives rather than accepts, the Batch membership a promotion
writes in the same commit, the three distinct facts a Batch merge is
judged by, and one walk that carries a Task from its backlog row to an
accepted Milestone through nothing but registered verbs.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    effective_records,
    read_ledger_records,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from tests.integration.runtime.daemon._delivery_verb_fixtures import (
    BRANCH,
    EVIDENCE_URN,
    RUN_URN,
    completion_lines,
    completion_params,
    landed_line,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    APPROVAL_URN,
    AT,
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    acceptance_bundle_row,
    approval_rows,
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

ACTOR = "OP-0001"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _canary(
    tmp_path: Path, rows: dict[str, dict[str, Any]], lines: tuple[LedgerRecord, ...] = ()
) -> CanaryProvision:
    """Provision a canary holding *rows* and *lines*."""
    canary = provision(tmp_path / "repo", code="DLV")
    seed(canary, rows)
    _append(canary, lines)
    return canary


def _append(canary: CanaryProvision, lines: tuple[LedgerRecord, ...]) -> None:
    """Append ledger lines to the collections they declare."""
    for line in lines:
        append_ledger_record(ledger_path(document_path(canary), line.collection), line)


def _call(
    canary: CanaryProvision, tmp_path: Path, method: str, urn: str, revision: int, **params: Any
) -> dict[str, Any]:
    """Dispatch one verb against *canary* and return its envelope."""
    ctx = method_context(tmp_path / "runtime")
    payload: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": urn,
        "expected_revision": revision,
        "idempotency_key": f"{method}-{urn.rsplit('/', 1)[1]}-{revision}",
        "actor": ACTOR,
        **params,
    }
    return asyncio.run(methods.dispatch(method, ctx, payload))


def _row(canary: CanaryProvision, collection: Epoch2Collection, key: str) -> dict[str, Any]:
    """Return one record from whichever tier holds it now."""
    path = document_path(canary)
    row: dict[str, Any] | None = read_document(path).get(collection.value, {}).get(key)
    if row is not None:
        return row
    lines = effective_records(read_ledger_records(ledger_path(path, collection)))
    return next(item.payload for item in lines if item.record_key == key)


def _ok(answer: dict[str, Any]) -> int:
    """Assert *answer* committed and return the revision it left."""
    assert answer["status"] == "ok", answer["errors"]
    return int(answer["revision_after"])


# ---- promotion writes Batch membership ------------------------------------


def _promotion(**batch: Any) -> dict[str, dict[str, Any]]:
    """Return a draft Task beside the Batch it is about to be promoted into."""
    row = seed_row("batch", batch.pop("status", "ACTIVE"))
    row.update(batch)
    return {"task": {"EAWF-0042": seed_row("task", "DRAFT")}, "batch": {"BAT-0007": row}}


def _promote(canary: CanaryProvision, tmp_path: Path) -> dict[str, Any]:
    """Promote the seeded draft into the seeded Batch."""
    return _call(
        canary,
        tmp_path,
        "domain.task.promote",
        TASK_URN,
        1,
        updates={
            "batch_ref": BATCH_URN,
            "criteria": seed_row("task", "PLANNED")["criteria"],
            "due_scope": MILESTONE_URN,
        },
    )


def test_promotion_appends_the_task_to_its_batch(tmp_path: Path) -> None:
    canary = _canary(tmp_path, _promotion())

    _ok(_promote(canary, tmp_path))

    batch = _row(canary, Epoch2Collection.BATCH, "BAT-0007")
    assert batch["task_refs"] == [TASK_URN]
    assert batch["revision"] == 2


def test_promotion_into_a_batch_already_listing_the_task_leaves_it_alone(
    tmp_path: Path,
) -> None:
    canary = _canary(tmp_path, _promotion(task_refs=[TASK_URN]))

    _ok(_promote(canary, tmp_path))

    batch = _row(canary, Epoch2Collection.BATCH, "BAT-0007")
    assert batch["task_refs"] == [TASK_URN]
    assert batch["revision"] == 1


def test_promotion_into_a_planned_batch_appends_as_well(tmp_path: Path) -> None:
    canary = _canary(tmp_path, _promotion(status="PLANNED"))

    _ok(_promote(canary, tmp_path))

    assert _row(canary, Epoch2Collection.BATCH, "BAT-0007")["task_refs"] == [TASK_URN]


def test_promotion_into_a_batch_past_active_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    canary = _canary(tmp_path, _promotion(status="READY_TO_MERGE"))
    before = document_path(canary).read_bytes()

    answer = _promote(canary, tmp_path)

    assert answer["errors"][0]["code"] == DomainErrorCode.TRANSITION_GUARD_FAILED.value
    assert answer["errors"][0]["guard"] == "promotion_contract_complete"
    assert "READY_TO_MERGE" in answer["errors"][0]["message"]
    assert document_path(canary).read_bytes() == before


def test_batch_ready_counts_a_placed_task_its_list_does_not_name(tmp_path: Path) -> None:
    """A Task placed before promotion wrote membership still holds the Batch open."""
    running = seed_row("task", "RUNNING")
    canary = _canary(
        tmp_path,
        {"batch": {"BAT-0007": seed_row("batch", "ACTIVE")}, "task": {"EAWF-0042": running}},
    )

    answer = _call(
        canary,
        tmp_path,
        "domain.batch.ready",
        BATCH_URN,
        1,
        updates={
            "current_head_binding": seed_row("batch", "READY_TO_MERGE")["current_head_binding"]
        },
    )

    assert answer["errors"][0]["guard"] == "tasks_ready_to_integrate"


# ---- task completion derives its binding ----------------------------------


def _ready_task(tmp_path: Path, *, lines: tuple[LedgerRecord, ...] = ()) -> CanaryProvision:
    """Return a canary holding the shared world's Task, ready to integrate."""
    return _canary(tmp_path, {"task": {"EAWF-0042": world.task_row()}}, lines)


def test_completion_records_the_binding_it_derives_not_the_one_presented(
    tmp_path: Path,
) -> None:
    canary = _ready_task(tmp_path, lines=completion_lines())
    forged = seed_row("task", "COMPLETED")["integrated_binding"]

    _ok(
        _call(
            canary,
            tmp_path,
            "domain.task.complete",
            TASK_URN,
            1,
            updates={"integrated_binding": forged},
            **completion_params(),
        )
    )

    binding = _row(canary, Epoch2Collection.TASK, "EAWF-0042")["integrated_binding"]
    assert binding["head_sha"] == world.SECOND_HEAD
    assert binding["tree_sha"] == world.TREE
    assert binding["policy_revision"] == 1
    assert binding != forged


def test_completion_with_a_leg_left_to_rerun_is_proof_stale(tmp_path: Path) -> None:
    canary = _ready_task(tmp_path, lines=completion_lines())
    before = document_path(canary).read_bytes()

    answer = _call(
        canary, tmp_path, "domain.task.complete", TASK_URN, 1, **completion_params(receipts=[])
    )

    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.PROOF_STALE.value
    assert row["guard"] == "integrated_binding_pinned"
    assert "G-01" in row["message"]
    assert answer["revision_before"] == 1
    assert document_path(canary).read_bytes() == before


def test_completion_naming_another_commit_is_refused(tmp_path: Path) -> None:
    canary = _ready_task(tmp_path, lines=completion_lines())
    params = completion_params()
    params["integrated_commit"] = world.THIRD_HEAD

    answer = _call(canary, tmp_path, "domain.task.complete", TASK_URN, 1, **params)

    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.TRANSITION_GUARD_FAILED.value
    assert "completion_commit_mismatch" in row["message"]


def test_completion_on_receipts_no_proof_run_filed_is_refused(tmp_path: Path) -> None:
    """A receipt the caller wrote, however well keyed, completes nothing."""
    canary = _ready_task(tmp_path, lines=completion_lines(filed=False))

    answer = _call(canary, tmp_path, "domain.task.complete", TASK_URN, 1, **completion_params())

    assert answer["errors"][0]["code"] == DomainErrorCode.TRANSITION_GUARD_FAILED.value
    assert "completion_receipt_unfiled" in answer["errors"][0]["message"]


def test_completion_without_integration_is_refused_even_on_a_pass(tmp_path: Path) -> None:
    """DEL-001: a sealed pass the Batch never integrated completes nothing."""
    canary = _ready_task(tmp_path, lines=(world.bundle_line(world.bundle_row()),))

    answer = _call(canary, tmp_path, "domain.task.complete", TASK_URN, 1, **completion_params())

    assert "completion_generation_unselected" in answer["errors"][0]["message"]


def test_completion_of_a_running_task_is_an_illegal_edge(tmp_path: Path) -> None:
    canary = _canary(
        tmp_path,
        {"task": {"EAWF-0042": world.task_row(status=TaskStatus.RUNNING)}},
        completion_lines(),
    )

    answer = _call(canary, tmp_path, "domain.task.complete", TASK_URN, 1, **completion_params())

    assert answer["errors"][0]["code"] == DomainErrorCode.ILLEGAL_TRANSITION.value


def test_completion_at_a_stale_revision_is_a_conflict(tmp_path: Path) -> None:
    canary = _ready_task(tmp_path, lines=completion_lines())

    answer = _call(canary, tmp_path, "domain.task.complete", TASK_URN, 2, **completion_params())

    assert answer["errors"][0]["code"] == DomainErrorCode.REVISION_CONFLICT.value


def test_completion_rejects_an_unparsable_commit(tmp_path: Path) -> None:
    canary = _ready_task(tmp_path, lines=completion_lines())
    params = completion_params()
    params["integrated_commit"] = "not-a-sha"

    answer = _call(canary, tmp_path, "domain.task.complete", TASK_URN, 1, **params)

    assert answer["errors"][0]["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert "integrated_commit" in answer["errors"][0]["message"]


# ---- the Batch's merge facts ----------------------------------------------


def test_batch_complete_refuses_a_landing_of_another_head(tmp_path: Path) -> None:
    """A read-back that landed some other head does not reconcile this one."""
    other = seed_row("batch", "MERGING")
    other["current_head_binding"] = {
        **other["current_head_binding"],
        "head_sha": world.THIRD_HEAD,
    }
    canary = _canary(
        tmp_path,
        {"batch": {"BAT-0007": seed_row("batch", "MERGED_PENDING_RECONCILIATION")}},
        (landed_line(other),),
    )

    answer = _call(canary, tmp_path, "domain.batch.complete", BATCH_URN, 1)

    assert answer["errors"][0]["guard"] == "reconciliation_matched"


def test_observe_merge_needs_the_presented_observation_too(tmp_path: Path) -> None:
    """A filed read-back does not stand in for the caller presenting the fact."""
    canary = _canary(
        tmp_path,
        {"batch": {"BAT-0007": seed_row("batch", "MERGING")}},
        (landed_line(seed_row("batch", "MERGING")),),
    )

    answer = _call(canary, tmp_path, "domain.batch.observe_merge", BATCH_URN, 1)

    assert answer["errors"][0]["guard"] == "host_merge_observed"


# ---- the whole walk -------------------------------------------------------


def _cursor(canary: CanaryProvision) -> int:
    """Return the tree's committed canonical sequence."""
    return int(read_document(document_path(canary)).get("canonical_sequence", 0))


def _create(canary: CanaryProvision, tmp_path: Path, urn: str, spec: dict[str, Any]) -> None:
    """Admit one record through its kind's create verb."""
    kind = urn.rsplit("/", 2)[1]
    answer = asyncio.run(
        methods.dispatch(
            f"domain.{kind}.create",
            method_context(tmp_path / "runtime"),
            {
                "repo_root": str(canary.root),
                "urn": urn,
                "expected_revision": _cursor(canary),
                "idempotency_key": f"create-{kind}",
                "actor": ACTOR,
                "spec": spec,
            },
        )
    )
    assert answer["status"] == "ok", answer["errors"]


def _reconcile(canary: CanaryProvision, tmp_path: Path, head_sha: str) -> dict[str, Any]:
    """File the read-back of a target branch that carries *head_sha*."""
    return asyncio.run(
        methods.dispatch(
            "runtime.delivery.reconcile_merge",
            method_context(tmp_path / "runtime"),
            {
                "repo_root": str(canary.root),
                "urn": BATCH_URN,
                "actor": ACTOR,
                "idempotency_key": "reconcile-1",
                "observation": {
                    "batch_ref": BATCH_URN,
                    "target_branch": BRANCH,
                    "observed_at": AT.isoformat(),
                    "target_head_sha": head_sha,
                    "contained_shas": [head_sha],
                },
            },
        )
    )


def test_a_task_walks_from_backlog_to_an_accepted_milestone(tmp_path: Path) -> None:
    milestone = seed_row("milestone", "ACTIVE")
    milestone["required_batch_refs"] = [BATCH_URN]
    canary = _canary(
        tmp_path,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {"MLS-0030": milestone},
            "batch": {"BAT-0007": seed_row("batch", "PLANNED")},
            **approval_rows(),
        },
    )
    _create(canary, tmp_path, TASK_URN, {"key": "EAWF-0042", "priority": "P1", "intent": "Ship"})
    criteria = [item.model_dump(mode="json") for item in world.criteria("CR-01", "CR-02")]
    task = _ok(
        _call(
            canary,
            tmp_path,
            "domain.task.promote",
            TASK_URN,
            1,
            updates={"batch_ref": BATCH_URN, "criteria": criteria, "due_scope": MILESTONE_URN},
        )
    )
    batch = _ok(
        _call(
            canary,
            tmp_path,
            "domain.batch.activate",
            BATCH_URN,
            2,
            updates={"target_branch": BRANCH},
        )
    )
    task = _ok(_call(canary, tmp_path, "domain.task.claim", TASK_URN, task))
    _create(
        canary,
        tmp_path,
        RUN_URN,
        {
            "key": "RUN-00000010",
            "scope": {
                "scope_kind": "task",
                "purpose": "implement",
                "task_ref": TASK_URN,
                "write_set": ["src/eawf/sample.py"],
            },
        },
    )
    run = _ok(
        _call(
            canary, tmp_path, "domain.run.start", RUN_URN, 1, updates={"started_at": AT.isoformat()}
        )
    )
    task = _ok(
        _call(
            canary,
            tmp_path,
            "domain.task.start",
            TASK_URN,
            task,
            updates={"active_run_ref": RUN_URN},
        )
    )
    task = _ok(
        _call(
            canary,
            tmp_path,
            "domain.task.ready",
            TASK_URN,
            task,
            observations=["run_report_bound"],
            binding_refs=[EVIDENCE_URN],
        )
    )
    _append(canary, completion_lines())
    _ok(_call(canary, tmp_path, "domain.task.complete", TASK_URN, task, **completion_params()))
    _ok(
        _call(
            canary,
            tmp_path,
            "domain.run.finish",
            RUN_URN,
            run,
            observations=["run_report_bound"],
            updates={"ended_at": AT.isoformat()},
        )
    )
    integrated = _row(canary, Epoch2Collection.TASK, "EAWF-0042")["integrated_binding"]
    batch = _ok(
        _call(
            canary,
            tmp_path,
            "domain.batch.ready",
            BATCH_URN,
            batch,
            updates={"current_head_binding": integrated},
        )
    )
    batch = _ok(_call(canary, tmp_path, "domain.batch.merge", BATCH_URN, batch))
    reconciled = _reconcile(canary, tmp_path, integrated["head_sha"])
    assert reconciled["outcome"] == "landed"
    batch = _ok(
        _call(
            canary,
            tmp_path,
            "domain.batch.observe_merge",
            BATCH_URN,
            batch,
            observations=["host_merge_observed"],
            binding_refs=[reconciled["record_key"]],
        )
    )
    _ok(
        _call(
            canary,
            tmp_path,
            "domain.batch.complete",
            BATCH_URN,
            batch,
            binding_refs=[reconciled["record_key"]],
        )
    )
    review = _ok(
        _call(
            canary,
            tmp_path,
            "domain.milestone.open_review",
            MILESTONE_URN,
            1,
            updates={"acceptance_bundle_revision": 1},
        )
    )
    _ok(
        _call(
            canary,
            tmp_path,
            "domain.milestone.accept",
            MILESTONE_URN,
            review,
            updates={"accepted_binding": seed_row("milestone", "COMPLETED")["accepted_binding"]},
            approval_receipt_ref=APPROVAL_URN,
            acceptance_bundle=acceptance_bundle_row(),
        )
    )

    assert _row(canary, Epoch2Collection.TASK, "EAWF-0042")["status"] == "COMPLETED"
    assert _row(canary, Epoch2Collection.RUN, "RUN-00000010")["status"] == "COMPLETED"
    assert _row(canary, Epoch2Collection.BATCH, "BAT-0007")["status"] == "COMPLETED"
    assert _row(canary, Epoch2Collection.BATCH, "BAT-0007")["task_refs"] == [TASK_URN]
    assert _row(canary, Epoch2Collection.MILESTONE, "MLS-0030")["status"] == "COMPLETED"
