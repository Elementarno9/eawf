"""Publish keeps per-Task commits as they were made, interleaved Batches included.

A Batch now closes as soon as its Tasks are ready and their commits are on the phase
branch, so the commits a push carries are the per-Task commits cherry-picked there, in
whatever order the Tasks landed. Publish places each under its one Batch and rewrites
none of them; it reads a completed Task's placement from its ledger, and passes over
the staged row of a Batch that already closed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.workflow.integration.publish import (
    PUBLISH_COMMIT_MIXED,
    PUBLISH_COMMIT_UNASSIGNED,
    PUBLISH_TASK_UNPLACED,
    PublishRefusedError,
    ScratchCommit,
    plan_publish,
    publish,
    read_membership,
)
from eawf.workflow.integration.publish_binding import PublishedCommit
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    TASK_URN,
    document_path,
    seed_row,
)
from tests.integration.workflow.integration.test_publish import (
    BATCH_B_URN,
    TASK_B_URN,
    Scratch,
    _range,
    _stage,
    git,
    make_repo,
)

#: Two Batches' work, interleaved the way cherry-picks land it.
INTERLEAVED: Final = [
    ("src/a.py", "feat: a\n\nTask: EAWF-0042"),
    ("src/b.py", "feat: b\n\nTask: EAWF-0043"),
    ("src/c.py", "fix: c\n\nTask: EAWF-0042"),
    (".ea/notes/n.txt", "state: record a note"),
    ("src/d.py", "feat: d\n\nTask: EAWF-0043"),
]


def _no_bind(_branch: str, _targets: object) -> int:
    raise AssertionError("nothing is staged, so nothing is bound")


def _row(index: int, message: str) -> ScratchCommit:
    return ScratchCommit(sha=f"{index:040x}", tree_sha=f"{index + 100:040x}", message=message)


PLACED: Final = {"EAWF-0042": "BAT-0007", "EAWF-0043": "BAT-0008"}


# ---- the plan, without a repository ------------------------------------------


def test_interleaved_commits_are_filed_under_their_batches_in_order() -> None:
    commits = [_row(index, message) for index, (_, message) in enumerate(INTERLEAVED)]

    plan = plan_publish("a" * 40, "b" * 40, commits, PLACED)

    assert list(plan.batches) == ["BAT-0007", "BAT-0008"]
    assert [item.sha for item in plan.batches["BAT-0007"]] == [commits[0].sha, commits[2].sha]
    assert [item.sha for item in plan.batches["BAT-0008"]] == [commits[1].sha, commits[4].sha]


def test_a_single_commit_is_one_batch_of_one() -> None:
    plan = plan_publish("a" * 40, "b" * 40, [_row(1, "feat: a\n\nTask: EAWF-0042")], PLACED)

    assert {key: len(found) for key, found in plan.batches.items()} == {"BAT-0007": 1}


def test_a_range_of_state_commits_carries_no_batch() -> None:
    plan = plan_publish("a" * 40, "b" * 40, [_row(1, "state: a"), _row(2, "state: b")], PLACED)

    assert plan.batches == {}


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("no trailer at all", PUBLISH_COMMIT_UNASSIGNED),
        ("feat: a\n\nTask: EAWF-9999", PUBLISH_TASK_UNPLACED),
        ("feat: a\n\nTask: EAWF-0042\nTask: EAWF-0043", PUBLISH_COMMIT_MIXED),
        ("feat: a\n\nBatch: BAT-0008\nTask: EAWF-0042", PUBLISH_COMMIT_MIXED),
    ],
)
def test_a_commit_not_in_exactly_one_batch_is_refused(message: str, code: str) -> None:
    with pytest.raises(PublishRefusedError) as caught:
        plan_publish(
            "a" * 40, "b" * 40, [_row(1, "feat: ok\n\nTask: EAWF-0042"), _row(2, message)], PLACED
        )

    assert caught.value.code == code
    assert isinstance(caught.value, ValueError)


# ---- the publish, on a repository ------------------------------------------------


def test_publish_accepts_interleaved_commits_and_rewrites_none(tmp_path: Path) -> None:
    made = make_repo(tmp_path, messages=INTERLEAVED)

    result = publish(made.root, membership=read_membership(made.root / ".ea"), bind=_no_bind)

    assert result.noop
    assert _range(made.root, made.base) == list(made.commits)
    assert result.new_tip == result.old_tip == made.tip
    assert result.batch_commits == {"BAT-0007": made.commits[2], "BAT-0008": made.commits[4]}


def test_staged_rows_are_offered_every_commit_of_their_batch(tmp_path: Path) -> None:
    made = make_repo(tmp_path, messages=INTERLEAVED)
    _stage(made, batch=BATCH_URN, task=TASK_URN, at=made.commits[2])
    _stage(made, batch=BATCH_B_URN, task=TASK_B_URN, at=made.commits[4])
    offered: dict[str, tuple[PublishedCommit, ...]] = {}

    def bind(_branch: str, targets: dict[str, tuple[PublishedCommit, ...]]) -> int:
        offered.update(targets)
        return len(targets)

    result = publish(made.root, membership=read_membership(made.root / ".ea"), bind=bind)  # type: ignore[arg-type]

    assert result.bound == 2 and not result.noop
    assert [item.sha for item in offered[BATCH_URN]] == [made.commits[0], made.commits[2]]
    assert [item.sha for item in offered[BATCH_B_URN]] == [made.commits[1], made.commits[4]]
    assert _range(made.root, made.base)[: len(made.commits)] == list(made.commits)


def _commit_state(made: Scratch) -> None:
    """Commit the state a test rewrote, as the daemon's state commit would."""
    git(made.root, "add", "-A", ".ea")
    git(made.root, "commit", "-q", "-m", "state: record the closes")


def _complete_and_compact(made: Scratch, key: str) -> None:
    """File *key* COMPLETED in the Task ledger and take it out of the document."""
    path = document_path(made.canary)
    row = seed_row("task", "COMPLETED")
    append_ledger_record(
        ledger_path(path, Epoch2Collection.TASK),
        LedgerRecord(
            collection=Epoch2Collection.TASK,
            record_key=key,
            status="COMPLETED",
            recorded_at=AT,
            payload=row,
        ),
    )
    document = read_document(path)
    del document["task"][key]
    write_document(path, document)
    _commit_state(made)


def test_a_completed_tasks_commit_is_placed_from_its_ledger(tmp_path: Path) -> None:
    made = make_repo(tmp_path, messages=INTERLEAVED[:2])
    _complete_and_compact(made, "EAWF-0042")

    membership = read_membership(made.root / ".ea")
    result = publish(made.root, membership=membership, bind=_no_bind)

    assert membership.task_batches["EAWF-0042"] == "BAT-0007"
    assert result.batch_commits == {"BAT-0007": made.commits[0], "BAT-0008": made.commits[1]}


def test_a_closed_batchs_staged_row_needs_no_binding(tmp_path: Path) -> None:
    made = make_repo(tmp_path, messages=INTERLEAVED[:2])
    _stage(made, batch=BATCH_URN, task=TASK_URN, at=made.commits[0])
    path = document_path(made.canary)
    document = read_document(path)
    del document["batch"]["BAT-0007"]
    write_document(path, document)
    _commit_state(made)

    membership = read_membership(made.root / ".ea")
    result = publish(made.root, membership=membership, bind=_no_bind)

    assert membership.staged == ()
    assert result.noop
