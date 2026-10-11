"""The daemon fails every dispatched Run a dead daemon left open, and frees its Task.

Each test seeds a provisioned canary with the Runs, dispatch attempts and Task a
dead daemon would leave, then reaps it as a freshly started daemon does: with an
empty supervisor and a start time later than every seeded Run. Statuses are read
back from the tree's document, the same rows the console draws.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.task_status import TaskStatus
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon import methods, run_reaper
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusalCode,
    TransactionRefusedError,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.domain_envelope import DOMAIN_TRANSITION_METHOD
from eawf.runtime.daemon.methods.domain_guards import task_run_open
from eawf.runtime.daemon.methods.task_dispatch import _DISPATCHABLE_STATUSES
from eawf.runtime.daemon.native_dispatch import DispatchAttempt, DispatchStage, append_attempt
from eawf.runtime.daemon.run_reaper import reap_lost_runs
from eawf.runtime.daemon.run_supervisor import RunSupervisor
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
    tree_root,
)

#: Who the reap is attributed to.
ACTOR: Final = "EAWFD"

#: The seeded Run and Task, as the seed records key them.
RUN_KEY: Final = "RUN-00000010"
TASK_KEY: Final = "EAWF-0042"

#: When the reaping daemon started: after every seeded Run was created.
STARTED_AT: Final = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary with nothing seeded yet."""
    return provision(tmp_path / "repo")


@pytest.fixture
def context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """The canary's native context, attached by a fresh daemon."""
    return root_context(canary, tmp_path / "runtime")


def running_task() -> dict[str, Any]:
    """Return the seeded Task, RUNNING under the seeded Run and claimed."""
    task = seed_row("task", "RUNNING")
    task["claimed_by"] = "OP-0001"
    return task


def dispatch_attempt(context: Epoch2RootContext, run: dict[str, Any]) -> None:
    """Record the compiled dispatch attempt a daemon writes before it leases and spawns."""
    with context.session([run["urn"]]) as session:
        append_attempt(
            session,
            DispatchAttempt(
                attempt_ref=f"ATT-{int(run['key'][4:]):032x}",
                run_ref=run["urn"],
                task_ref=run["scope"]["task_ref"],
                dispatch_key=f"dispatch-{run['key']}",
                stage=DispatchStage.COMPILED,
                compiled_spec_digest=f"sha256:{'a' * 64}",
                authority_capsule_digest=f"sha256:{'b' * 64}",
                route_policy_revision=1,
                provider_kind="codex",
                started_at=run["created_at"],
                recorded_at=run["created_at"],
            ),
        )


def seed_open(
    canary: CanaryProvision,
    context: Epoch2RootContext,
    run_status: str,
    *,
    dispatched: bool = True,
    **run_fields: Any,
) -> None:
    """Seed one Run in *run_status*, dispatched unless told otherwise, with its Task RUNNING."""
    run = seed_row("run", run_status)
    run.update(run_fields)
    seed(canary, {"run": {RUN_KEY: run}, "task": {TASK_KEY: running_task()}})
    if dispatched:
        dispatch_attempt(context, run)


def rows(canary: CanaryProvision, collection: Epoch2Collection) -> dict[str, Any]:
    """Return *collection*'s rows as the document holds them now."""
    return document_rows(read_document(document_path(canary)), collection)


def run_row(canary: CanaryProvision, key: str = RUN_KEY) -> dict[str, Any]:
    """Return one Run, from the document or, once compacted out, from its ledger."""
    row: dict[str, Any] | None = rows(canary, Epoch2Collection.RUN).get(key)
    if row is not None:
        return row
    ledger = ledger_path(document_path(canary), Epoch2Collection.RUN)
    for item in effective_records(read_ledger_records(ledger)):
        if item.record_key == key and "payload_kind" not in item.payload:
            return item.payload
    raise AssertionError(f"run {key} is in neither tier")


def task_now(canary: CanaryProvision) -> Task:
    """Return the seeded Task as the document holds it now."""
    return Task.model_validate(rows(canary, Epoch2Collection.TASK)[TASK_KEY])


def reap(context: Epoch2RootContext, supervisor: RunSupervisor | None = None) -> tuple[Any, ...]:
    """Reap the tree as a daemon started at :data:`STARTED_AT` does."""
    return reap_lost_runs(
        context,
        RunSupervisor() if supervisor is None else supervisor,
        actor=ACTOR,
        started_at=STARTED_AT,
        now=STARTED_AT + timedelta(seconds=5),
    )


def assert_reaped(canary: CanaryProvision) -> None:
    """Assert the Run failed as worker-lost and its Task went back to PLANNED."""
    run = run_row(canary)
    assert run["status"] == "FAILED"
    assert run["failure"]["code"] == "worker-lost"
    assert run["ended_at"] is not None
    task = task_now(canary)
    assert task.status is TaskStatus.PLANNED
    assert task.active_run_ref is None
    assert task.claimed_by is None


def test_a_running_run_with_no_worker_fails_and_its_task_leaves_running(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    seed_open(canary, context, "RUNNING")

    envelopes = reap(context)

    assert envelopes
    assert_reaped(canary)


def test_a_queued_run_with_no_worker_is_taken_up_then_failed(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    seed_open(canary, context, "QUEUED")

    reap(context)

    assert_reaped(canary)
    assert run_row(canary)["started_at"] is not None


def test_a_reaped_task_can_be_dispatched_again(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    seed_open(canary, context, "RUNNING")

    reap(context)

    task = task_now(canary)
    assert task.status in _DISPATCHABLE_STATUSES
    assert not task_run_open(read_document(document_path(canary)), task)


def test_a_supervised_run_is_never_reaped(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    seed_open(canary, context, "RUNNING")
    run_ref = rows(canary, Epoch2Collection.RUN)[RUN_KEY]["urn"]

    async def scenario() -> tuple[Any, ...]:
        supervisor = RunSupervisor()
        parked = asyncio.Event()

        async def park() -> None:
            await parked.wait()

        supervisor.supervise(run_ref, park())
        try:
            return reap(context, supervisor)
        finally:
            parked.set()

    assert asyncio.run(scenario()) == ()
    assert run_row(canary)["status"] == "RUNNING"
    assert task_now(canary).status is TaskStatus.RUNNING


def test_a_run_created_since_the_daemon_started_is_left_alone(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    seed_open(canary, context, "QUEUED", created_at=STARTED_AT.isoformat())

    assert reap(context) == ()
    assert run_row(canary)["status"] == "QUEUED"
    assert task_now(canary).status is TaskStatus.RUNNING


def test_a_tree_with_no_runs_is_a_no_op(context: Epoch2RootContext) -> None:
    assert reap(context) == ()


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED", "SUSPENDED"])
def test_a_run_not_open_is_untouched(
    canary: CanaryProvision, context: Epoch2RootContext, status: str
) -> None:
    run = seed_row("run", status)
    seed(canary, {"run": {RUN_KEY: run}})
    dispatch_attempt(context, run)
    before = run_row(canary)

    assert reap(context) == ()
    assert run_row(canary) == before


def test_a_second_pass_finds_nothing_left_to_reap(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    seed_open(canary, context, "RUNNING")
    reap(context)
    run, task = run_row(canary), task_now(canary)

    assert reap(context) == ()
    assert run_row(canary) == run
    assert task_now(canary) == task


def test_a_task_rebound_to_a_later_run_is_left_to_that_run(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    task = running_task()
    task["active_run_ref"] = task["active_run_ref"].replace(RUN_KEY, "RUN-00000011")
    run = seed_row("run", "RUNNING")
    seed(canary, {"run": {RUN_KEY: run}, "task": {TASK_KEY: task}})
    dispatch_attempt(context, run)

    reap(context)

    assert run_row(canary)["status"] == "FAILED"
    assert task_now(canary).status is TaskStatus.RUNNING


def test_a_refused_run_is_kept_and_the_others_still_reaped(
    canary: CanaryProvision, context: Epoch2RootContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, other = (
        seed_row("run", "RUNNING"),
        rekeyed(seed_row("run", "RUNNING"), key="RUN-00000011"),
    )
    seed(canary, {"run": {RUN_KEY: first, "RUN-00000011": other}})
    dispatch_attempt(context, first)
    dispatch_attempt(context, other)
    reap_one = run_reaper._reap

    def refuse_first(context: Epoch2RootContext, run: Any, **kwargs: Any) -> tuple[Any, ...]:
        if run.key == RUN_KEY:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.REVISION_CONFLICT,
                detail="the Run moved",
                entity_ref=str(run.urn),
                remediation="read the Run again",
            )
        return reap_one(context, run, **kwargs)

    monkeypatch.setattr(run_reaper, "_reap", refuse_first)

    reap(context)

    assert run_row(canary)["status"] == "RUNNING"
    assert run_row(canary, "RUN-00000011")["status"] == "FAILED"


def test_the_daemon_reaps_its_bound_tree_on_start(
    canary: CanaryProvision, context: Epoch2RootContext, tmp_path: Path
) -> None:
    seed_open(canary, context, "RUNNING")
    ctx: MethodContext = method_context(tmp_path / "boot")
    ctx.state_path = str(tree_root(canary) / "state.json")
    ctx.started_at = STARTED_AT.isoformat()

    asyncio.run(daemon_main._attach_bound_tree(ctx))

    assert_reaped(canary)


def test_a_run_a_host_session_started_is_never_reaped(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    seed_open(canary, context, "RUNNING", dispatched=False)

    assert reap(context) == ()
    assert run_row(canary)["status"] == "RUNNING"
    assert task_now(canary).status is TaskStatus.RUNNING


def requeue(ctx: MethodContext, canary: CanaryProvision) -> dict[str, Any]:
    """Ask the generic transition verb to hand the seeded Task back to PLANNED."""
    task = task_now(canary)
    return asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": str(task.urn),
                "to_status": "PLANNED",
                "expected_revision": task.revision,
                "idempotency_key": f"requeue-{TASK_KEY}",
                "actor": "OP-0001",
                "reason_code": "worker-lost",
            },
        )
    )


def test_an_operator_requeues_a_task_whose_run_failed(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    seed(
        canary,
        {"run": {RUN_KEY: seed_row("run", "FAILED")}, "task": {TASK_KEY: running_task()}},
    )

    answer = requeue(method_context(tmp_path / "rpc"), canary)

    assert answer["status"] == "ok", answer
    task = task_now(canary)
    assert task.status is TaskStatus.PLANNED
    assert task.active_run_ref is None
    assert task.claimed_by is None


def test_a_requeue_on_a_stale_revision_is_refused(canary: CanaryProvision, tmp_path: Path) -> None:
    seed(
        canary,
        {"run": {RUN_KEY: seed_row("run", "FAILED")}, "task": {TASK_KEY: running_task()}},
    )
    task = task_now(canary)

    answer = asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            method_context(tmp_path / "rpc"),
            {
                "repo_root": str(canary.root),
                "urn": str(task.urn),
                "to_status": "PLANNED",
                "expected_revision": task.revision + 1,
                "idempotency_key": f"requeue-{TASK_KEY}",
                "actor": "OP-0001",
                "reason_code": "worker-lost",
            },
        )
    )

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "revision_conflict"
    assert task_now(canary).status is TaskStatus.RUNNING
