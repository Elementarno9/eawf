"""Nothing the console shows about a Run, Task or Batch outlives the failure that ended it.

Each test drives one way a daemon path used to leave state behind -- a Task RUNNING
under a Run that ended, a proof that failed as a whole re-run on every sweep, a proof
note kept after its Batch became unsettled, proof checkouts and processes that outlive
their proof, and an abandoned integration holding its repository's slot -- and reads the
outcome back from the tree, the proof notes the console draws, or the process table.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from unittest import mock

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.store.compaction import read_document, write_document
from eawf.platform.install.canary import CanaryProvision
from eawf.platform.process_liveness import pid_alive
from eawf.runtime.daemon import batch_proof, methods, native_dispatch
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon.batch_close import close_landed_batch, close_landed_batches
from eawf.runtime.daemon.batch_proof import proof_notes, prune_orphan_checkouts
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.domain_guards import integrating_batch_refs
from eawf.runtime.daemon.methods.task_dispatch import TASK_DISPATCH_METHOD
from eawf.runtime.daemon.native_dispatch import compile_launchers
from eawf.runtime.daemon.run_supervisor import (
    SupervisedTurn,
    conclude_run,
    end_exhausted_run,
    supervisor_of,
    sweep_lost_runs,
)
from eawf.runtime.runtimes.adapter import TurnOutcome
from eawf.runtime.workspace.lease import root_leases
from eawf.workflow.audit_dsl import registry
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    BATCH_URN,
    document_path,
    tree_root,
)
from tests.integration.runtime.daemon.test_batch_close import (
    ACTOR as CLOSE_ACTOR,
)
from tests.integration.runtime.daemon.test_batch_close import (
    AT,
    batch,
    close,
    context_of,
    git,
)
from tests.integration.runtime.daemon.test_batch_close import make_tree as make_repo_tree
from tests.integration.runtime.daemon.test_batch_close_ceiling import (
    close_second,
    file_generation,
    holder,
    world,
)
from tests.integration.runtime.daemon.test_batch_close_proof import (
    FAILING,
    PASSING,
    make_tree,
    proved_task,
    settle,
    uv_shim,  # noqa: F401 -- the proof tests' pytest shim, used by name below
)
from tests.integration.runtime.daemon.test_task_dispatch import (
    ACTOR,
    BUDGET,
    FIRST_RUN,
    TASK_KEY,
    ParkedLauncher,
    RefusingLauncher,
    _private_homes,  # noqa: F401 -- autouse: every host home reads as empty
    dispatch,
    method_ctx,
    run_now,
    task_now,
)
from tests.integration.runtime.daemon.test_task_dispatch import make_tree as make_task_tree

pytestmark = pytest.mark.integration

#: How long a test waits for a background proof or a stopped process to settle.
SETTLE_SECONDS: Final = 30.0


# ---- finding 1: a Run that ends hands its Task back --------------------------------


def park_and_lose(tmp_path: Path) -> tuple[CanaryProvision, MethodContext, ParkedLauncher]:
    """Dispatch the Task with a parked turn, then end the loop with the turn still parked.

    Ending the loop cancels the supervisor without ending the Run, which is what a daemon
    that died mid-turn leaves: a RUNNING Run and its Task RUNNING under it.
    """
    canary = make_task_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    launcher = ParkedLauncher("claude")
    params = {"repo_root": str(canary.root), "actor": ACTOR, "budget": BUDGET, "task": TASK_KEY}
    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    ):
        answer = asyncio.run(methods.dispatch(TASK_DISPATCH_METHOD, ctx, params))
    assert answer["stage"] == "announced"
    assert run_now(ctx, canary).status.value == "RUNNING"
    assert task_now(ctx, canary).status.value == "RUNNING"
    return canary, ctx, launcher


def assert_requeued(ctx: MethodContext, canary: CanaryProvision, code: str) -> None:
    """Assert the Run failed with *code* and its Task is PLANNED with no Run named."""
    run = run_now(ctx, canary)
    assert run.status.value == "FAILED"
    assert run.failure is not None
    assert run.failure.code == code
    requeued = task_now(ctx, canary)
    assert requeued.status.value == "PLANNED"
    assert requeued.active_run_ref is None
    assert requeued.claimed_by is None


def test_a_lost_worker_hands_its_task_back_and_the_task_is_dispatched_again(
    tmp_path: Path,
) -> None:
    canary, ctx, _ = park_and_lose(tmp_path)
    context = ctx.native_root_context(canary.root / ".ea")

    sweep_lost_runs(context, supervisor_of(context), actor=ACTOR, now=datetime.now(UTC))

    assert_requeued(ctx, canary, "provider-lost")
    launcher = ParkedLauncher("claude")
    (again,) = dispatch(ctx, canary, launcher, {"task": TASK_KEY})
    assert isinstance(again, dict), again
    assert again["run_key"] == "RUN-00000002"
    assert launcher.working is not None
    assert launcher.working.status.value == "RUNNING"
    assert str(launcher.working.active_run_ref) == again["run_ref"]


def test_a_run_ended_at_its_ceiling_hands_its_task_back(tmp_path: Path) -> None:
    canary, ctx, _ = park_and_lose(tmp_path)
    context = ctx.native_root_context(canary.root / ".ea")

    end_exhausted_run(
        context,
        run_now(ctx, canary).urn,
        actor=ACTOR,
        detail="wall clock 3600s of 3600s",
        now=datetime.now(UTC),
    )

    assert_requeued(ctx, canary, "budget-exhausted")


def test_a_refused_launch_hands_its_task_back(tmp_path: Path) -> None:
    canary = make_task_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")

    (answer,) = dispatch(ctx, canary, RefusingLauncher("claude"), {"task": TASK_KEY})

    assert isinstance(answer, DaemonValidationError)
    assert_requeued(ctx, canary, "dispatch-spawn-failed")
    assert run_now(ctx, canary).key == FIRST_RUN


def test_a_task_rebound_to_a_later_run_is_not_requeued_by_the_earlier_one(
    tmp_path: Path,
) -> None:
    canary, ctx, _ = park_and_lose(tmp_path)
    context = ctx.native_root_context(canary.root / ".ea")
    later = str(run_now(ctx, canary).urn).replace(FIRST_RUN, "RUN-00000009")
    path = document_path(canary)
    document = read_document(path)
    document["task"][TASK_KEY]["active_run_ref"] = later
    write_document(path, document)

    sweep_lost_runs(context, supervisor_of(context), actor=ACTOR, now=datetime.now(UTC))

    assert run_now(ctx, canary).status.value == "FAILED"
    assert task_now(ctx, canary).status.value == "RUNNING"


def end_run_behind_the_daemon(canary: CanaryProvision, status: str, **fields: Any) -> None:
    """Move the dispatched Run to *status* in the document, as a host or a control does."""
    path = document_path(canary)
    document = read_document(path)
    run = document["run"][FIRST_RUN]
    run.update(status=status, ended_at=datetime.now(UTC).isoformat(), **fields)
    write_document(path, document)


def test_a_run_cancelled_before_its_turn_ended_hands_its_task_back(tmp_path: Path) -> None:
    canary, ctx, _ = park_and_lose(tmp_path)
    context = ctx.native_root_context(canary.root / ".ea")
    end_run_behind_the_daemon(canary, "CANCELLED")
    (lease,) = root_leases(context)
    job = SupervisedTurn(
        context=context,
        urn=run_now(ctx, canary).urn,
        actor=ACTOR,
        attempt_ref="ATT-cancelled",
        lease_id=lease.lease_id,
        turn=None,
        pgid=1,
        workspace=tmp_path,
        before=None,
        publish=None,
    )

    conclude_run(job, TurnOutcome(exit_kind="interrupted"), now=datetime.now(UTC))

    assert run_now(ctx, canary).status.value == "CANCELLED"
    requeued = task_now(ctx, canary)
    assert requeued.status.value == "PLANNED"
    assert requeued.active_run_ref is None


def test_a_task_left_running_behind_an_ended_run_is_rebound_by_the_next_dispatch(
    tmp_path: Path,
) -> None:
    canary, ctx, _ = park_and_lose(tmp_path)
    end_run_behind_the_daemon(
        canary,
        "FAILED",
        failure={"code": "host-ended", "message": "the host ended the Run", "evidence_refs": []},
    )
    stranded = task_now(ctx, canary)
    assert stranded.status.value == "RUNNING"

    launcher = ParkedLauncher("claude")
    (again,) = dispatch(ctx, canary, launcher, {"task": TASK_KEY})

    assert isinstance(again, dict), again
    assert again["run_key"] == "RUN-00000002"
    rebound = launcher.working
    assert rebound is not None
    assert rebound.status.value == "RUNNING"
    assert str(rebound.active_run_ref) == again["run_ref"]
    assert rebound.revision == stranded.revision + 1


# ---- finding 2: a proof that fails as a whole is not re-run every sweep ------------


def wait_idle(canary: CanaryProvision) -> None:
    """Wait until no background proof of the canary's Batch runs."""
    key = (tree_root(canary).resolve(), BATCH_URN)
    deadline = time.monotonic() + SETTLE_SECONDS
    while key in batch_proof._RUNNING:
        assert time.monotonic() < deadline, "the proof never settled"
        time.sleep(0.05)


def test_a_proof_that_fails_as_a_whole_is_unmet_and_not_re_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def no_checkout(context: Any, key: Any, head: str) -> Path:
        calls.append(head)
        raise DaemonValidationError(
            f"validation_failed: proof_checkout_failed: the repository cannot check out {head}"
        )

    monkeypatch.setattr(batch_proof, "_checkout_for", no_checkout)
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", PASSING)},
    )
    head = git(repo, "rev-parse", "HEAD")

    first = close(canary, tmp_path)
    wait_idle(canary)
    later = [close(canary, tmp_path) for _ in range(3)]
    wait_idle(canary)

    assert first.proof == "proving"
    assert calls == [head]
    for closure in later:
        assert not closure.closed
        assert closure.proof == "unmet"
        assert closure.reason.startswith("proof failed: ")
        assert "cannot check out" in closure.reason
    assert proof_notes(tree_root(canary)) == {BATCH_URN: later[-1].reason}

    retried, _ = close_landed_batch(
        context_of(canary, tmp_path),
        parse_qualified_urn(BATCH_URN),
        actor=CLOSE_ACTOR,
        now=AT,
        rerun_failed=True,
    )
    wait_idle(canary)

    assert retried.proof == "proving"
    assert calls == [head, head]


def test_a_gate_raising_os_error_files_a_crashed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_sandbox(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(batch_proof, "run_leg", no_sandbox)
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", PASSING)},
    )

    closure = settle(canary, tmp_path)

    assert not closure.closed
    assert closure.proof == "crashed"
    assert closure.reason.startswith("CR-0042-1 crashed at ")


# ---- finding 3: a Batch that leaves proof eligibility drops its note ---------------


@pytest.mark.usefixtures("uv_shim")
def test_a_batch_that_becomes_unsettled_drops_its_proof_note_and_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    announced: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        batch_proof, "_announce", lambda _context, ref, note: announced.append((ref, note))
    )
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", FAILING)},
    )
    unmet = settle(canary, tmp_path)
    assert unmet.proof == "unmet"
    assert proof_notes(tree_root(canary)) == {BATCH_URN: unmet.reason}
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2

    path = document_path(canary)
    document = read_document(path)
    document["task"]["EAWF-0042"]["status"] = "RUNNING"
    write_document(path, document)
    closures, _ = close_landed_batches(context_of(canary, tmp_path), (), actor=CLOSE_ACTOR, now=AT)

    (closure,) = closures
    assert not closure.closed
    assert proof_notes(tree_root(canary)) == {}
    assert announced[-1] == (BATCH_URN, None)
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


# ---- finding 4: proof processes and checkouts do not outlive their proof -----------


def test_a_command_over_its_timeout_takes_its_whole_process_group_with_it(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "child.wait()\n"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        registry._run_in_group(
            [sys.executable, "-c", script, str(pid_file)],
            timeout=2,
            cwd=tmp_path,
            env=dict(os.environ),
        )

    grandchild = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + SETTLE_SECONDS
    while pid_alive(grandchild):
        assert time.monotonic() < deadline, f"pytest-like grandchild {grandchild} survived"
        time.sleep(0.05)


def orphan_checkout(repo: Path, scratch: Path, owner: int | None) -> Path:
    """Register a detached proof checkout under *scratch*, owned by *owner* if given."""
    scratch.mkdir()
    if owner is not None:
        (scratch / "owner").write_text(str(owner), encoding="utf-8")
    git(repo, "worktree", "add", "--detach", str(scratch / "checkout"), "HEAD")
    return scratch


def test_the_daemon_removes_proof_checkouts_a_stopped_daemon_left_on_start(
    tmp_path: Path,
) -> None:
    canary, repo = make_repo_tree(tmp_path, batches={}, tasks={})
    orphaned = orphan_checkout(repo, tmp_path / "eawf-proof-orphan", owner=None)
    held = orphan_checkout(repo, tmp_path / "eawf-proof-held", owner=os.getpid())
    ctx = method_ctx(tmp_path / "boot")
    ctx.state_path = str(tree_root(canary) / "state.json")

    asyncio.run(daemon_main._attach_bound_tree(ctx))

    listed = git(repo, "worktree", "list", "--porcelain")
    assert not orphaned.exists()
    assert "eawf-proof-orphan" not in listed
    assert (held / "checkout").is_dir()
    assert "eawf-proof-held" in listed
    assert prune_orphan_checkouts(repo) == 0


def test_a_repository_with_no_proof_checkouts_prunes_nothing(tmp_path: Path) -> None:
    _canary, repo = make_repo_tree(tmp_path, batches={}, tasks={})

    assert prune_orphan_checkouts(repo) == 0
    assert prune_orphan_checkouts(tmp_path / "not-a-repository") == 0


# ---- finding 5: an abandoned integration holds no slot ------------------------------


def test_an_integration_abandoned_by_a_later_batch_edit_holds_no_slot(tmp_path: Path) -> None:
    canary = world(
        tmp_path, holder("ACTIVE", updated_at="2026-09-19T00:00:00Z"), first_task="PLANNED"
    )
    file_generation(canary, tmp_path / "runtime", BATCH_URN)
    path = document_path(canary)

    assert integrating_batch_refs(read_document(path), path) == frozenset()
    closed, reason = close_second(canary, tmp_path)
    assert closed, reason


def test_an_integration_filed_after_the_last_batch_edit_still_holds_the_slot(
    tmp_path: Path,
) -> None:
    canary = world(tmp_path, holder("ACTIVE"), first_task="PLANNED")
    file_generation(canary, tmp_path / "runtime", BATCH_URN)
    path = document_path(canary)

    assert integrating_batch_refs(read_document(path), path) == frozenset({BATCH_URN})
    closed, _ = close_second(canary, tmp_path)
    assert not closed


def test_a_batch_with_no_row_is_never_integrating(tmp_path: Path) -> None:
    canary = world(tmp_path, holder("ACTIVE"), first_task="PLANNED")
    file_generation(canary, tmp_path / "runtime", BATCH_URN)
    path = document_path(canary)
    document = read_document(path)
    del document["batch"]["BAT-0007"]

    assert integrating_batch_refs(document, path) == frozenset()
    assert integrating_batch_refs({}, tmp_path / "absent" / "state.json") == frozenset()
