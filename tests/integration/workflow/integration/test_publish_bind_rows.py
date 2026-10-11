"""``runtime.publish.bind_staged`` files each staged row of a Batch on its own generation.

A Batch whose work lands as one commit per Task stages one row per adopted Task, so the
verb may bind several rows of one Batch at once. Each files the generation after the one
the row before it selected. The verb reads every changed path as git names it, quoting
aside, and refuses a change it cannot read back rather than digest an empty diff.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.store.paths import ledger_path, staged_dir
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import publish as publish_module
from eawf.runtime.daemon.methods.delivery import read_generation_ledger
from eawf.runtime.daemon.methods.publish import PUBLISH_BIND_STAGED_METHOD
from eawf.workflow.integration.publish_binding import StagedBinding, stage_binding
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    TASK_URN,
    document_path,
    method_context,
)
from tests.integration.workflow.integration.test_publish import (
    ACTOR,
    AUDIT_ID,
    BRANCH,
    TASK_B_URN,
    Scratch,
    canary_runtime_under_tmp,
    git,
    make_repo,
)

pytestmark = pytest.mark.integration

__all__ = ["canary_runtime_under_tmp"]


def _stage(repo: Scratch, *, at: str, tasks: tuple[str, ...] = (TASK_URN,)) -> Path:
    """Stage an adoption of BAT-0007's work at the scratch commit *at*."""
    row = StagedBinding.model_validate(
        {
            "batch_ref": BATCH_URN,
            "task_refs": tasks,
            "tree_sha": git(repo.root, "rev-parse", f"{at}^{{tree}}"),
            "head_sha": at,
            "report_verdict": "pass",
            "evidence_refs": [AUDIT_ID],
            "staged_by": ACTOR,
            "staged_at": AT,
        }
    )
    return stage_binding(staged_dir(document_path(repo.canary)), row)


def _bind(repo: Scratch, tmp_path: Path, *shas: str) -> Any:
    """Bind the staged rows, offering *shas* as BAT-0007's commits, oldest first."""
    commits = [
        {"sha": sha, "tree_sha": git(repo.root, "rev-parse", f"{sha}^{{tree}}")} for sha in shas
    ]
    params = {
        "repo_root": str(repo.root),
        "branch": BRANCH,
        "candidates": [{"batch_ref": BATCH_URN, "commits": commits}],
    }
    return asyncio.run(
        methods.dispatch(PUBLISH_BIND_STAGED_METHOD, method_context(tmp_path / "rt"), params)
    )


def _generations(repo: Scratch) -> Any:
    ledger = ledger_path(document_path(repo.canary), Epoch2Collection.BATCH)
    return read_generation_ledger(ledger, BATCH_URN)  # type: ignore[arg-type]


def test_two_rows_of_one_batch_file_successive_generations(tmp_path: Path) -> None:
    made = make_repo(
        tmp_path,
        messages=[
            ("src/a.py", "feat: a\n\nTask: EAWF-0042"),
            ("src/b.py", "feat: b\n\nTask: EAWF-0043"),
        ],
    )
    _stage(made, at=made.commits[0])
    _stage(made, at=made.commits[1], tasks=(TASK_B_URN,))

    answer = _bind(made, tmp_path, *made.commits)

    bound = [(row["head_sha"], row["generation_id"]) for row in answer["bound"]]
    assert [sha for sha, _ in bound] == list(made.commits)
    assert len({generation for _, generation in bound}) == 2
    generations = _generations(made).generations
    assert [item.generation for item in generations] == [2, 3]
    assert generations[1].parent_generation_id == generations[0].id
    assert _generations(made).head.integrated_revision.head_sha == made.tip
    assert list(staged_dir(document_path(made.canary)).glob("*.json")) == []


def test_bind_reads_a_changed_path_git_would_quote(tmp_path: Path) -> None:
    made = make_repo(tmp_path, messages=[("src/café one.py", "feat: a\n\nTask: EAWF-0042")])
    _stage(made, at=made.tip)

    _bind(made, tmp_path, made.tip)

    assert _generations(made).head.changed_paths == ("src/café one.py",)


def test_bind_refuses_a_diff_it_cannot_read_and_files_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = make_repo(tmp_path, messages=[("src/a.py", "feat: a\n\nTask: EAWF-0042")])
    staged = _stage(made, at=made.tip)
    run = subprocess.run

    def failing_diff(argv: list[str], **kwargs: Any) -> Any:
        if "--binary" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: bad object")
        return run(argv, **kwargs)

    monkeypatch.setattr(publish_module.subprocess, "run", failing_diff)

    with pytest.raises(methods.DaemonValidationError, match="publish_commit_unread"):
        _bind(made, tmp_path, made.tip)

    assert staged.exists()
    assert _generations(made).generations == ()
