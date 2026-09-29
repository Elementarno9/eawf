"""The native delivery loop, from a leased worktree to a completed Task, through the CLI.

A Run is dispatched for real and leased a worktree; the worker commits in
it; and from there every step is an ``eawf`` command an operator types:
the candidate is submitted and sealed, the evidence the integration's
diagnostics cite is recorded, the Batch is integrated through the daemon's
own assembly, the Task is readied, proved on the delivered commit,
assessed, and completed on the binding the assessment derived.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.runtime.candidate import candidate_identity
from eawf.kernel.store.tiers import Epoch2Collection
from tests.integration.runtime.daemon.methods.test_delivery_candidate_sealing import (
    SUCCESS,
    commit_in_lease,
    dispatched,
)
from tests.integration.runtime.daemon.test_delivery_landed_loop import (
    ACTOR,
    cli,
    eawf,
    gates_file,
    git,
    spec,
    stored,
)
from tests.integration.runtime.daemon.test_native_dispatch import root_ctx
from tests.integration.workflow.delivery import _completion_fixtures as world
from tests.integration.workflow.delivery.test_delivery_request_assembly import (
    REPAIR_TASK,
    batch_row,
    repair_task_row,
    seed_row,
)

pytestmark = pytest.mark.integration

__all__ = ["cli"]


def test_native_work_walks_from_a_lease_to_a_completed_task_through_the_cli(
    tmp_path: Path, cli: None
) -> None:
    canary, runtime, _ = dispatched(tmp_path)
    context = root_ctx(canary, runtime)
    world.seed_task(context, world.task_row(status=world.TaskStatus.RUNNING))
    world.seed_task(context, repair_task_row())
    seed_row(context, Epoch2Collection.BATCH, batch_row())
    root = canary.root
    base = git(root, "rev-parse", "main")
    submission_ref = commit_in_lease(canary, runtime)
    claim, report = SUCCESS["submission"], SUCCESS["report"]
    run = str(world.RUN)

    submitted = eawf(root, "task", "submit", run, "--task-ref", world.TASK,
                     "--submission-ref", submission_ref,
                     *[arg for path in claim["changed_paths"] for arg in ("--changed-path", path)],
                     "--resulting-tree-digest", claim["resulting_tree_digest"],
                     "--expected-run-revision", "1",
                     "--idempotency-key", "submit-1", "--actor", ACTOR)["result"]  # fmt: skip
    assert submitted["candidate_ref"] == candidate_identity(
        task_ref=world.TASK, resulting_tree_digest=claim["resulting_tree_digest"]
    )
    sealed = eawf(root, "task", "seal", run, "--candidate-ref", submitted["candidate_ref"],
                  "--resulting-tree-digest", report["resulting_tree_digest"],
                  "--verdict", report["verdict"], "--report-digest", report["report_digest"],
                  "--report-schema-ref", report["report_schema_ref"],
                  "--expected-run-revision", "1",
                  "--idempotency-key", "seal-1", "--actor", ACTOR)["result"]  # fmt: skip
    assert sealed["sealed"] is True
    recorded = eawf(root, "record", "evidence", world.BATCH, "--kind", "artifact",
                    "--summary", "integration diagnostics for the canary batch",
                    "--expected-revision", "1",
                    "--idempotency-key", "evd-1", "--actor", ACTOR)  # fmt: skip
    diagnostic = recorded["result"]["evidence_ref"]
    refs = spec(tmp_path, "refs.json", {
        "base": world.binding(generation=1, head_sha=base).model_dump(mode="json"),
        "exit_refs": {"repair_task": REPAIR_TASK, "rebase_task": REPAIR_TASK},
        "diagnostic_ref": diagnostic,
    })  # fmt: skip
    integrated = eawf(root, "batch", "integrate", world.BATCH, "--actor", ACTOR,
                      "--expected-batch-revision", "1", "--from-spec", refs,
                      "--wait")["result"]  # fmt: skip
    assert integrated["delivered"] is True
    ready = spec(tmp_path, "ready.json",
                 {"observations": ["run_report_bound"], "binding_refs": [diagnostic]})  # fmt: skip
    eawf(root, "task", "ready", world.TASK, "--expected-task-revision", "1",
         "--idempotency-key", "ready-1", "--actor", ACTOR, "--from-spec", ready)  # fmt: skip
    proved = eawf(root, "task", "prove", world.TASK, "--gates", gates_file(tmp_path),
                  "--expected-task-revision", "2",
                  "--idempotency-key", "prove-1", "--actor", ACTOR, "--wait")["result"]  # fmt: skip
    assert proved["passed"] is True
    out = tmp_path / "assessment.json"
    assessed = eawf(root, "task", "assess", world.TASK, "--actor", ACTOR, "--out", str(out))[
        "result"
    ]
    delivered = assessed["integrated_commit"]
    assert git(root, "rev-parse", f"{delivered}^") == base
    eawf(root, "task", "complete", world.TASK, "--expected-task-revision", "2",
         "--idempotency-key", "complete-1", "--actor", ACTOR,
         "--integrated-commit", delivered, "--assessment", str(out))  # fmt: skip

    task = stored(canary, Epoch2Collection.TASK, "EAWF-0042")
    assert task["status"] == "COMPLETED"
    assert task["integrated_binding"]["head_sha"] == delivered
