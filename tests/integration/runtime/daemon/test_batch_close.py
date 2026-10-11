"""A Batch closes, with its Tasks, once every Task is ready and its work is on the branch.

Each test provisions a canary over a real git repository whose phase branch carries, or
does not carry, a commit naming a Task in its ``Task:`` trailer, seeds a Track, an
ACTIVE Milestone and the Batches under test, and drives the close directly. Whether a
Batch closed is read back from the tree: a completed Batch and its completed Tasks have
left the document for their ledgers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Final

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.batch_close import (
    BatchClosure,
    close_landed_batch,
    close_landed_batches,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    TASK_URN,
    document_path,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
)

ACTOR: Final = "OP-0001"
BRANCH: Final = "feature/eawf-v0.7"
SECOND_BATCH_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0008"


def git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, path: str, message: str) -> str:
    """Commit one new file at *path* with *message* and return the commit."""
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# {message}\n", encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def task(key: str, status: str, *, batch: str = BATCH_URN) -> dict[str, Any]:
    """Return a seeded Task keyed *key* in *status*, placed in *batch*."""
    row = rekeyed(seed_row("task", status), key=key)
    row["batch_ref"] = batch
    row.pop("active_run_ref", None)
    return row


def batch(key: str, status: str, task_keys: list[str]) -> dict[str, Any]:
    """Return a seeded Batch keyed *key* in *status*, listing *task_keys*."""
    row = rekeyed(seed_row("batch", status), key=key)
    row["task_refs"] = [f"{TASK_URN.rsplit('/', 1)[0]}/{name}" for name in task_keys]
    return row


def make_tree(
    tmp_path: Path, *, batches: dict[str, dict[str, Any]], tasks: dict[str, dict[str, Any]]
) -> tuple[CanaryProvision, Path]:
    """Provision a canary over a repository on :data:`BRANCH`, seeded with the rows."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", BRANCH)
    git(repo, "config", "user.email", "ci@example.com")
    git(repo, "config", "user.name", "ci")
    commit(repo, "src/module.py", "initial")
    canary = provision(repo, code="BCL")
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {"MLS-0030": seed_row("milestone", "ACTIVE")},
            "batch": batches,
            "task": tasks,
        },
    )
    return canary, repo


def context_of(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """Return the native context of *canary*."""
    return root_context(canary, tmp_path / "runtime")


def ledger_status(canary: CanaryProvision, collection: Epoch2Collection, key: str) -> str | None:
    """Return the standing ledger status of *key*, or ``None`` when it was never filed."""
    lines = read_ledger_records(ledger_path(document_path(canary), collection))
    return {item.record_key: item.status for item in effective_records(lines)}.get(key)


def ledger_payload(canary: CanaryProvision, collection: Epoch2Collection, key: str) -> Any:
    """Return the standing ledger payload of *key*."""
    lines = read_ledger_records(ledger_path(document_path(canary), collection))
    return next(item.payload for item in effective_records(lines) if item.record_key == key)


def live(canary: CanaryProvision, collection: Epoch2Collection) -> dict[str, Any]:
    """Return the live rows of *collection*."""
    return document_rows(read_document(document_path(canary)), collection)


def close(canary: CanaryProvision, tmp_path: Path, urn: str = BATCH_URN) -> BatchClosure:
    """Close the Batch *urn* names and return what the attempt did."""
    closure, _ = close_landed_batch(
        context_of(canary, tmp_path), parse_qualified_urn(urn), actor=ACTOR, now=AT
    )
    return closure


# ---- closes a ready, landed Batch --------------------------------------------


def test_a_ready_landed_batch_closes_with_its_tasks(tmp_path: Path) -> None:
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042", "EAWF-0043"])},
        tasks={
            "EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE"),
            "EAWF-0043": task("EAWF-0043", "READY_TO_INTEGRATE"),
        },
    )
    landed = commit(repo, "src/alpha.py", "feat: alpha\n\nTask: EAWF-0042")

    closure = close(canary, tmp_path)

    assert closure.closed, closure.reason
    assert closure.head_sha == landed
    assert closure.completed_task_keys == ("EAWF-0042", "EAWF-0043")
    assert "BAT-0007" not in live(canary, Epoch2Collection.BATCH)
    assert ledger_status(canary, Epoch2Collection.BATCH, "BAT-0007") == "COMPLETED"
    stored = ledger_payload(canary, Epoch2Collection.BATCH, "BAT-0007")
    assert stored["target_branch"] == BRANCH
    assert stored["current_head_binding"]["head_sha"] == landed
    for key in ("EAWF-0042", "EAWF-0043"):
        assert ledger_status(canary, Epoch2Collection.TASK, key) == "COMPLETED"
        binding = ledger_payload(canary, Epoch2Collection.TASK, key)["integrated_binding"]
        assert binding["head_sha"] == landed


def test_a_single_evidence_only_task_counts_as_landed(tmp_path: Path) -> None:
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE")},
    )

    closure = close(canary, tmp_path)

    assert closure.closed, closure.reason
    assert closure.completed_task_keys == ("EAWF-0042",)
    assert ledger_status(canary, Epoch2Collection.TASK, "EAWF-0042") == "COMPLETED"


def test_a_cancelled_task_does_not_hold_the_batch_open(tmp_path: Path) -> None:
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042", "EAWF-0043"])},
        tasks={
            "EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE"),
            "EAWF-0043": task("EAWF-0043", "CANCELLED"),
        },
    )

    closure = close(canary, tmp_path)

    assert closure.closed, closure.reason
    assert closure.completed_task_keys == ("EAWF-0042",)


def test_a_batch_pinned_partway_resumes_on_its_pinned_head(tmp_path: Path) -> None:
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE")},
    )
    head = git(repo, "rev-parse", "HEAD")
    row = batch("BAT-0007", "READY_TO_MERGE", ["EAWF-0042"])
    row["target_branch"] = BRANCH
    row["current_head_binding"] = {
        **row["current_head_binding"],
        "head_sha": head,
        "tree_sha": git(repo, "rev-parse", "HEAD^{tree}"),
    }
    seed(canary, {"batch": {"BAT-0007": row}})
    commit(repo, "src/later.py", "feat: later work on the branch\n\nTask: EAWF-0042")

    closure = close(canary, tmp_path)

    # The Task's commit landed after the pin, so the pinned head does not carry it.
    assert not closure.closed
    assert "EAWF-0042 commit" in closure.reason
    git(repo, "reset", "-q", "--hard", head)

    resumed = close(canary, tmp_path)

    assert resumed.closed, resumed.reason
    assert resumed.head_sha == head


# ---- leaves an unready or unlanded Batch alone -------------------------------


def test_a_batch_with_a_running_task_is_left_untouched(tmp_path: Path) -> None:
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042", "EAWF-0043"])},
        tasks={
            "EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE"),
            "EAWF-0043": task("EAWF-0043", "RUNNING"),
        },
    )
    before = live(canary, Epoch2Collection.BATCH)["BAT-0007"]

    closure = close(canary, tmp_path)

    assert not closure.closed
    assert closure.reason == "EAWF-0043 RUNNING"
    assert live(canary, Epoch2Collection.BATCH)["BAT-0007"] == before


def test_a_ready_task_whose_commit_is_off_the_branch_keeps_the_batch(tmp_path: Path) -> None:
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE")},
    )
    git(repo, "checkout", "-q", "-b", "lease/run-1")
    stray = commit(repo, "src/alpha.py", "Run RUN-1 changes\n\nTask: EAWF-0042")
    git(repo, "checkout", "-q", BRANCH)
    before = live(canary, Epoch2Collection.BATCH)["BAT-0007"]

    closure = close(canary, tmp_path)

    assert not closure.closed
    assert closure.reason == f"EAWF-0042 commit {stray[:12]} not on {BRANCH}"
    assert live(canary, Epoch2Collection.BATCH)["BAT-0007"] == before
    assert live(canary, Epoch2Collection.TASK)["EAWF-0042"]["status"] == "READY_TO_INTEGRATE"


def test_a_trailer_naming_a_longer_key_does_not_land_a_task(tmp_path: Path) -> None:
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE")},
    )
    commit(repo, "src/other.py", "feat: other\n\nTask: EAWF-00421")
    git(repo, "checkout", "-q", "-b", "lease/run-1")
    stray = commit(repo, "src/alpha.py", "feat: alpha\n\nTask: EAWF-0042")
    git(repo, "checkout", "-q", BRANCH)

    closure = close(canary, tmp_path)

    assert closure.reason == f"EAWF-0042 commit {stray[:12]} not on {BRANCH}"


# ---- boundaries and errors -----------------------------------------------------


def test_an_empty_batch_never_closes(tmp_path: Path) -> None:
    canary, _repo = make_tree(
        tmp_path, batches={"BAT-0007": batch("BAT-0007", "PLANNED", [])}, tasks={}
    )

    closure = close(canary, tmp_path)

    assert not closure.closed
    assert closure.reason == "holds no Task"
    assert live(canary, Epoch2Collection.BATCH)["BAT-0007"]["status"] == "PLANNED"


def test_a_listed_task_neither_tier_holds_is_missing(tmp_path: Path) -> None:
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042", "EAWF-0099"])},
        tasks={"EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE")},
    )

    closure = close(canary, tmp_path)

    assert not closure.closed
    assert closure.reason == "EAWF-0099 missing"


def test_a_batch_the_document_does_not_hold_is_not_live(tmp_path: Path) -> None:
    canary, _repo = make_tree(tmp_path, batches={}, tasks={})

    closure = close(canary, tmp_path)

    assert closure == BatchClosure(batch_key="BAT-0007", closed=False, reason="is not a live batch")


def test_a_task_urn_names_no_live_batch(tmp_path: Path) -> None:
    canary, _repo = make_tree(
        tmp_path, batches={}, tasks={"EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE")}
    )

    closure = close(canary, tmp_path, TASK_URN)

    assert not closure.closed and closure.reason == "is not a live batch"
    assert live(canary, Epoch2Collection.TASK)["EAWF-0042"]["status"] == "READY_TO_INTEGRATE"


def test_a_planned_batch_on_a_detached_head_names_no_branch(tmp_path: Path) -> None:
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE")},
    )
    git(repo, "checkout", "-q", "--detach", "HEAD")

    closure = close(canary, tmp_path)

    assert closure.reason == "names no target branch and the repository's HEAD names none"


def test_an_active_batch_whose_branch_is_gone_is_kept(tmp_path: Path) -> None:
    row = batch("BAT-0007", "ACTIVE", ["EAWF-0042"])
    row["target_branch"] = "feature/gone"
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": row},
        tasks={"EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE")},
    )

    closure = close(canary, tmp_path)

    assert closure.reason == "finds no branch feature/gone in the repository"


def test_an_unknown_key_is_reported_and_nothing_else_is_tried(tmp_path: Path) -> None:
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE")},
    )

    closures, envelopes = close_landed_batches(
        context_of(canary, tmp_path), ("BAT-0999",), actor=ACTOR, now=AT
    )

    assert [(item.batch_key, item.closed) for item in closures] == [("BAT-0999", False)]
    assert closures[0].reason == "is not an open batch of an active milestone"
    assert envelopes == ()
    assert live(canary, Epoch2Collection.BATCH)["BAT-0007"]["status"] == "PLANNED"


# ---- one exact-head proof per repository -------------------------------------


def test_an_unready_batch_on_its_pinned_head_keeps_a_ready_one_planned(tmp_path: Path) -> None:
    active = batch("BAT-0007", "READY_TO_MERGE", ["EAWF-0042"])
    canary, _repo = make_tree(
        tmp_path,
        batches={
            "BAT-0007": active,
            "BAT-0008": batch("BAT-0008", "PLANNED", ["EAWF-0043"]),
        },
        tasks={
            "EAWF-0042": task("EAWF-0042", "RUNNING"),
            "EAWF-0043": task("EAWF-0043", "READY_TO_INTEGRATE", batch=SECOND_BATCH_URN),
        },
    )
    context = context_of(canary, tmp_path)

    closures, _ = close_landed_batches(context, (), actor=ACTOR, now=AT)

    assert [(item.batch_key, item.closed) for item in closures] == [
        ("BAT-0007", False),
        ("BAT-0008", False),
    ]
    assert "batch_wip_admits" in closures[1].reason
    statuses = {key: row["status"] for key, row in live(canary, Epoch2Collection.BATCH).items()}
    assert statuses == {"BAT-0007": "READY_TO_MERGE", "BAT-0008": "PLANNED"}


def test_the_active_batch_closes_first_and_lets_the_planned_one_through(tmp_path: Path) -> None:
    active = batch("BAT-0007", "ACTIVE", ["EAWF-0042"])
    canary, _repo = make_tree(
        tmp_path,
        batches={
            "BAT-0008": batch("BAT-0008", "PLANNED", ["EAWF-0043"]),
            "BAT-0007": active,
        },
        tasks={
            "EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE"),
            "EAWF-0043": task("EAWF-0043", "READY_TO_INTEGRATE", batch=SECOND_BATCH_URN),
        },
    )

    closures, envelopes = close_landed_batches(
        context_of(canary, tmp_path), (), actor=ACTOR, now=AT
    )

    assert [(item.batch_key, item.closed) for item in closures] == [
        ("BAT-0007", True),
        ("BAT-0008", True),
    ]
    assert envelopes
    assert live(canary, Epoch2Collection.BATCH) == {}
    for key in ("BAT-0007", "BAT-0008"):
        assert ledger_status(canary, Epoch2Collection.BATCH, key) == "COMPLETED"


def test_a_batch_of_a_milestone_not_active_is_not_swept(tmp_path: Path) -> None:
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": task("EAWF-0042", "READY_TO_INTEGRATE")},
    )
    seed(canary, {"milestone": {"MLS-0030": seed_row("milestone", "PLANNED")}})

    closures, _ = close_landed_batches(context_of(canary, tmp_path), (), actor=ACTOR, now=AT)

    assert closures == ()
