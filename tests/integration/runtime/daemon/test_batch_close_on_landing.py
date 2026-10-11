"""A Batch closes when its last Task's Run completes, and on the operator's close-landed verb.

The first half dispatches the Batch's only Task through ``runtime.task.dispatch`` with
a fake worker that files a passing report and ends its turn without editing anything,
so the Task delivered evidence rather than a commit. No other verb is sent: the Batch
and the Task are read back COMPLETED from the tree's ledgers. The second half drives
``runtime.delivery.close_landed`` over Batches whose work is, and is not, on the branch.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.batch_close_sweep import sweep_once
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery_close import DELIVERY_CLOSE_LANDED_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    TASK_URN,
    document_path,
    method_context,
    provision,
    rekeyed,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_run_completion_task import ReportingLauncher, make_tree
from tests.integration.runtime.daemon.test_task_dispatch import (  # noqa: F401  (autouse homes)
    TASK_KEY,
    _private_homes,
    dispatch,
    run_now,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
BRANCH: Final = "feature/eawf-v0.7"
SECOND_BATCH_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0008"
SECOND_TASK_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0043"


def git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def ledger_status(canary: CanaryProvision, collection: Epoch2Collection, key: str) -> str | None:
    """Return the standing ledger status of *key*, or ``None`` when it was never filed."""
    lines = read_ledger_records(ledger_path(document_path(canary), collection))
    return {item.record_key: item.status for item in effective_records(lines)}.get(key)


def live(canary: CanaryProvision, collection: Epoch2Collection) -> dict[str, Any]:
    """Return the live rows of *collection*."""
    return document_rows(read_document(document_path(canary)), collection)


def seed_plan(canary: CanaryProvision, batches: dict[str, dict[str, Any]]) -> None:
    """Seed an ACTIVE Track and Milestone and *batches* beside the canary's Tasks."""
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {"MLS-0030": seed_row("milestone", "ACTIVE")},
            "batch": batches,
        },
    )


def planned_batch(key: str, task_refs: list[str]) -> dict[str, Any]:
    """Return a PLANNED Batch keyed *key* listing *task_refs*."""
    row = rekeyed(seed_row("batch", "PLANNED"), key=key)
    row["task_refs"] = task_refs
    return row


# ---- a Run completing on a passing report closes the Batch ------------------


def test_the_last_tasks_passing_run_closes_its_batch_with_no_command(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    seed_plan(canary, {"BAT-0007": planned_batch("BAT-0007", [TASK_URN])})
    ctx = method_context(tmp_path / "runtime")
    launcher = ReportingLauncher(ctx, canary, verdict="pass")

    (answer,) = dispatch(ctx, canary, launcher, {"task": TASK_KEY})

    assert isinstance(answer, dict), answer
    assert run_now(ctx, canary).status.value == "COMPLETED"
    assert live(canary, Epoch2Collection.BATCH) == {}
    assert ledger_status(canary, Epoch2Collection.BATCH, "BAT-0007") == "COMPLETED"
    assert TASK_KEY not in live(canary, Epoch2Collection.TASK)
    assert ledger_status(canary, Epoch2Collection.TASK, TASK_KEY) == "COMPLETED"


def test_a_failing_report_leaves_the_batch_planned(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    seed_plan(canary, {"BAT-0007": planned_batch("BAT-0007", [TASK_URN])})
    ctx = method_context(tmp_path / "runtime")

    dispatch(ctx, canary, ReportingLauncher(ctx, canary, verdict="fail"), {"task": TASK_KEY})

    assert live(canary, Epoch2Collection.BATCH)["BAT-0007"]["status"] == "PLANNED"
    assert live(canary, Epoch2Collection.TASK)[TASK_KEY]["status"] == "PLANNED"


def test_a_ready_task_beside_an_unready_one_leaves_the_batch_planned(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    other = rekeyed(seed_row("task", "PLANNED"), key="EAWF-0043")
    seed(canary, {"task": {"EAWF-0043": other}})
    seed_plan(canary, {"BAT-0007": planned_batch("BAT-0007", [TASK_URN, SECOND_TASK_URN])})
    ctx = method_context(tmp_path / "runtime")

    dispatch(ctx, canary, ReportingLauncher(ctx, canary, verdict="pass"), {"task": TASK_KEY})

    assert live(canary, Epoch2Collection.TASK)[TASK_KEY]["status"] == "READY_TO_INTEGRATE"
    assert live(canary, Epoch2Collection.BATCH)["BAT-0007"]["status"] == "PLANNED"


# ---- the close-landed verb -----------------------------------------------------


def make_landing_tree(tmp_path: Path) -> tuple[CanaryProvision, Path]:
    """Return a canary on :data:`BRANCH` with two ready Batches, one landed and one not.

    BAT-0007's Task has its trailered commit on the branch; BAT-0008's has one only on
    a lease branch, so it has not landed.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", BRANCH)
    git(repo, "config", "user.email", "ci@example.com")
    git(repo, "config", "user.name", "ci")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "feat: a\n\nTask: EAWF-0042")
    git(repo, "checkout", "-q", "-b", "lease/run-2")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "feat: b\n\nTask: EAWF-0043")
    git(repo, "checkout", "-q", BRANCH)
    canary = provision(repo, code="BCV")
    second = rekeyed(seed_row("task", "READY_TO_INTEGRATE"), key="EAWF-0043")
    second["batch_ref"] = SECOND_BATCH_URN
    seed(
        canary,
        {
            "task": {
                "EAWF-0042": seed_row("task", "READY_TO_INTEGRATE"),
                "EAWF-0043": second,
            }
        },
    )
    seed_plan(
        canary,
        {
            "BAT-0007": planned_batch("BAT-0007", [TASK_URN]),
            "BAT-0008": planned_batch("BAT-0008", [SECOND_TASK_URN]),
        },
    )
    return canary, repo


def close_landed(tmp_path: Path, canary: CanaryProvision, **params: Any) -> dict[str, Any]:
    """Send ``runtime.delivery.close_landed`` and return its answer."""
    call = {"repo_root": str(canary.root), "actor": ACTOR, **params}
    return asyncio.run(
        methods.dispatch(DELIVERY_CLOSE_LANDED_METHOD, method_context(tmp_path / "rt"), call)
    )


def test_close_landed_closes_each_ready_landed_batch_and_names_the_rest(tmp_path: Path) -> None:
    canary, _repo = make_landing_tree(tmp_path)

    answer = close_landed(tmp_path, canary)

    by_key = {row["batch_key"]: row for row in answer["closures"]}
    assert by_key["BAT-0007"]["closed"] is True
    assert by_key["BAT-0007"]["completed_task_keys"] == ["EAWF-0042"]
    assert by_key["BAT-0008"]["closed"] is False
    assert by_key["BAT-0008"]["reason"].startswith("EAWF-0043 commit ")
    assert by_key["BAT-0008"]["reason"].endswith(f" not on {BRANCH}")
    assert ledger_status(canary, Epoch2Collection.BATCH, "BAT-0007") == "COMPLETED"
    assert live(canary, Epoch2Collection.BATCH)["BAT-0008"]["status"] == "PLANNED"


def test_close_landed_closes_a_batch_once_its_commit_is_cherry_picked(tmp_path: Path) -> None:
    canary, repo = make_landing_tree(tmp_path)
    close_landed(tmp_path, canary, batch_keys=["BAT-0007"])
    git(repo, "cherry-pick", "lease/run-2")

    answer = close_landed(tmp_path, canary, batch_keys=["BAT-0008"])

    (closure,) = answer["closures"]
    assert closure["closed"] is True, closure
    assert closure["head_sha"] == git(repo, "rev-parse", "HEAD")
    assert ledger_status(canary, Epoch2Collection.TASK, "EAWF-0043") == "COMPLETED"


def test_close_landed_on_a_settled_tree_is_a_no_op(tmp_path: Path) -> None:
    canary, _repo = make_landing_tree(tmp_path)
    close_landed(tmp_path, canary, batch_keys=["BAT-0007"])

    answer = close_landed(tmp_path, canary, batch_keys=["BAT-0007"])

    assert answer["closures"] == [
        {
            "batch_key": "BAT-0007",
            "closed": False,
            "reason": "is not an open batch of an active milestone",
            "head_sha": None,
            "completed_task_keys": [],
            "proof": None,
        }
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"batch_keys": ["not-a-key"]},
        {"batch_keys": "BAT-0007"},
        {"unexpected": True},
        {"actor": "lowercase"},
    ],
)
def test_close_landed_refuses_a_malformed_request(tmp_path: Path, params: dict[str, Any]) -> None:
    canary, _repo = make_landing_tree(tmp_path)

    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        close_landed(tmp_path, canary, **params)

    assert live(canary, Epoch2Collection.BATCH)["BAT-0007"]["status"] == "PLANNED"


def test_close_landed_without_an_actor_is_refused(tmp_path: Path) -> None:
    canary, _repo = make_landing_tree(tmp_path)
    call = {"repo_root": str(canary.root)}

    with pytest.raises(DaemonValidationError, match="actor"):
        asyncio.run(
            methods.dispatch(DELIVERY_CLOSE_LANDED_METHOD, method_context(tmp_path / "rt"), call)
        )


# ---- the daemon's own sweep ----------------------------------------------------


def test_the_sweep_closes_a_batch_whose_commit_landed_after_its_task_was_ready(
    tmp_path: Path,
) -> None:
    canary, repo = make_landing_tree(tmp_path)
    ctx = method_context(tmp_path / "rt")
    ctx.native_root_context(canary.root / ".ea")
    sweep_once(ctx, now=AT)
    assert live(canary, Epoch2Collection.BATCH)["BAT-0008"]["status"] == "PLANNED"
    git(repo, "cherry-pick", "lease/run-2")

    envelopes = sweep_once(ctx, now=AT)

    assert envelopes
    assert live(canary, Epoch2Collection.BATCH) == {}
    assert ledger_status(canary, Epoch2Collection.BATCH, "BAT-0008") == "COMPLETED"
    assert ledger_status(canary, Epoch2Collection.TASK, "EAWF-0043") == "COMPLETED"


def test_the_sweep_with_no_attached_tree_does_nothing(tmp_path: Path) -> None:
    assert sweep_once(method_context(tmp_path / "rt"), now=AT) == ()
