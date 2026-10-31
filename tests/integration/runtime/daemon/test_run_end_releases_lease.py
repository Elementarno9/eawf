"""A Run that stops through a domain verb gives its workspace lease back.

A lease held by a stopped Run blocks every exclusive lease after it until its
deadline, a day later. Two paths close that: the domain verbs that end a Run
withdraw its leases once the transition commits, and the reconcile pass
withdraws a lease whose Run had already stopped before either path existed.
Both go through the residue judgement, so a workspace holding work is
quarantined in place rather than removed.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.runtime.lease import LeaseStatus
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods.workspace_lease import RECONCILE_METHOD
from eawf.runtime.daemon.run_supervisor import supervisor_of
from eawf.runtime.workspace.lease import (
    issue_lease,
    read_lease,
    reconcile_root,
    workspace_path,
)
from tests.integration.runtime.daemon._delivery_verb_fixtures import ENDED_AT, FAILURE
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_worklease import RUN_URN, TASK_URN, WRITABLE
from tests.integration.runtime.daemon.test_worklease_recovery import (
    build_canary,
    quarantine_records,
)

pytestmark = pytest.mark.integration

ACTOR = "OP-0001"
RUN_KEY = "RUN-00000010"
TERM = timedelta(hours=24)

FAIL_PARAMS: dict[str, Any] = {
    "observations": ["run_report_bound"],
    "reason_code": "gate-red",
    "updates": {"ended_at": ENDED_AT, "failure": FAILURE},
}
FINISH_PARAMS: dict[str, Any] = {
    "observations": ["run_report_bound"],
    "updates": {"ended_at": ENDED_AT},
}


def leased_canary(
    tmp_path: Path, *, run_status: str
) -> tuple[CanaryProvision, Epoch2RootContext, str]:
    """Return a canary holding one Run at *run_status* and one live lease of it."""
    provisioned, context = build_canary(tmp_path / "repo", code="LRL")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", run_status)}})
    lease = issue_lease(
        context,
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=WRITABLE,
        now=datetime.now(UTC) - timedelta(minutes=1),
        ttl=TERM,
    )
    return provisioned, context, lease.lease_id


def drive(
    method: str, provisioned: CanaryProvision, tmp_path: Path, params: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch one Run verb against the seeded Run and return the envelope."""
    request: dict[str, Any] = {
        "repo_root": str(provisioned.root),
        "urn": RUN_URN,
        "expected_revision": 1,
        "idempotency_key": f"req-{method}",
        "actor": ACTOR,
        **params,
    }
    return asyncio.run(methods.dispatch(method, method_context(tmp_path / "runtime"), request))


@pytest.mark.parametrize(
    ("method", "params"),
    [("domain.run.fail", FAIL_PARAMS), ("domain.run.finish", FINISH_PARAMS)],
    ids=["fail", "finish"],
)
def test_a_run_ended_by_a_domain_verb_releases_its_lease(
    method: str, params: dict[str, Any], tmp_path: Path
) -> None:
    provisioned, context, lease_id = leased_canary(tmp_path, run_status="RUNNING")

    answer = drive(method, provisioned, tmp_path, params)

    assert answer["status"] == "ok", answer["errors"]
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.RELEASED
    assert quarantine_records(context) == []


def test_a_refused_run_verb_leaves_the_lease_active(tmp_path: Path) -> None:
    """No transition committed, so the Run still holds its workspace."""
    provisioned, context, lease_id = leased_canary(tmp_path, run_status="RUNNING")

    answer = drive("domain.run.finish", provisioned, tmp_path, {"updates": {"ended_at": ENDED_AT}})

    assert answer["status"] == "error"
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.ACTIVE


def test_a_run_failed_over_a_dirty_workspace_quarantines_it_in_place(tmp_path: Path) -> None:
    provisioned, context, lease_id = leased_canary(tmp_path, run_status="RUNNING")
    target = workspace_path(context, handle=read_lease(context, lease_id=lease_id).workspace_handle)
    (target / "src" / "module.py").write_text("x = 2\n", encoding="utf-8")

    answer = drive("domain.run.fail", provisioned, tmp_path, FAIL_PARAMS)

    assert answer["status"] == "ok", answer["errors"]
    lease = read_lease(context, lease_id=lease_id)
    assert lease.status is LeaseStatus.QUARANTINED
    assert [record.lease_id for record in quarantine_records(context)] == [lease_id]
    assert (target / "src" / "module.py").read_text(encoding="utf-8") == "x = 2\n"


@pytest.mark.parametrize("run_status", ["FAILED", "COMPLETED", "CANCELLED"])
def test_reconcile_releases_a_live_lease_whose_run_already_stopped(
    run_status: str, tmp_path: Path
) -> None:
    _, context, lease_id = leased_canary(tmp_path, run_status=run_status)

    report = reconcile_root(context, now=datetime.now(UTC), busy=supervisor_of(context).busy)

    assert (report.examined, report.expired, report.released, report.quarantined) == (1, 0, 1, 0)
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.RELEASED


def test_reconcile_quarantines_a_stopped_runs_lease_over_committed_work(tmp_path: Path) -> None:
    """Commits past the base are work nobody has shown to be integrated."""
    _, context, lease_id = leased_canary(tmp_path, run_status="FAILED")
    target = workspace_path(context, handle=read_lease(context, lease_id=lease_id).workspace_handle)
    (target / "src" / "module.py").write_text("x = 3\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "work"], cwd=target, check=True)

    report = reconcile_root(context, now=datetime.now(UTC), busy=supervisor_of(context).busy)

    assert (report.released, report.quarantined) == (0, 1)
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.QUARANTINED
    assert target.is_dir()


@pytest.mark.parametrize("run_status", ["RUNNING", "QUEUED", "SUSPENDED"])
def test_reconcile_leaves_a_live_runs_lease_alone(run_status: str, tmp_path: Path) -> None:
    _, context, lease_id = leased_canary(tmp_path, run_status=run_status)

    report = reconcile_root(context, now=datetime.now(UTC), busy=supervisor_of(context).busy)

    assert (report.released, report.quarantined, report.untouched) == (0, 0, (lease_id,))
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.ACTIVE


# ---- a Run whose worker is still exiting keeps its lease ------------------------------


@pytest.mark.parametrize(
    ("method", "params"),
    [("domain.run.fail", FAIL_PARAMS), ("domain.run.finish", FINISH_PARAMS)],
    ids=["fail", "finish"],
)
def test_a_run_ended_while_its_worker_is_supervised_keeps_its_lease(
    method: str, params: dict[str, Any], tmp_path: Path
) -> None:
    provisioned, context, lease_id = leased_canary(tmp_path, run_status="RUNNING")
    ctx = method_context(tmp_path / "runtime")
    request = {
        "repo_root": str(provisioned.root),
        "urn": RUN_URN,
        "expected_revision": 1,
        "idempotency_key": f"req-{method}",
        "actor": ACTOR,
        **params,
    }

    with supervisor_of(ctx.native_root_context(provisioned.root / ".ea")).launching(RUN_URN):
        answer = asyncio.run(methods.dispatch(method, ctx, request))

    assert answer["status"] == "ok", answer["errors"]
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.ACTIVE


def test_reconcile_keeps_a_stopped_runs_lease_while_its_worker_is_supervised(
    tmp_path: Path,
) -> None:
    _, context, lease_id = leased_canary(tmp_path, run_status="FAILED")
    supervisor = supervisor_of(context)

    with supervisor.launching(RUN_URN):
        held = reconcile_root(context, now=datetime.now(UTC), busy=supervisor.busy)
    freed = reconcile_root(context, now=datetime.now(UTC), busy=supervisor.busy)

    assert (held.released, held.quarantined, held.untouched) == (0, 0, (lease_id,))
    assert (freed.released, freed.quarantined) == (1, 0)
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.RELEASED


def test_the_reconcile_verb_keeps_a_supervised_runs_lease(tmp_path: Path) -> None:
    provisioned, context, lease_id = leased_canary(tmp_path, run_status="COMPLETED")
    ctx = method_context(tmp_path / "runtime")

    with supervisor_of(ctx.native_root_context(provisioned.root / ".ea")).launching(RUN_URN):
        report = asyncio.run(
            methods.dispatch(RECONCILE_METHOD, ctx, {"repo_root": str(provisioned.root)})
        )

    assert report["untouched"] == [lease_id]
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.ACTIVE
