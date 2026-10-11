"""Scratch adoptions stage their binding locally; publish binds it by tree.

An adoption of a head the remote default branch does not carry writes a
staged row under the generation's ``local/staged/`` and leaves every
committed ledger untouched, while a head the default branch carries still
binds at once. The matcher binds a staged row to the one published commit
whose tree equals the staged tree and refuses zero or several with a named
reason.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.store.commit_policy import CommitPolicy, classify_path
from eawf.kernel.store.paths import LOCAL_DIRNAME, STAGED_DIRNAME, staged_dir
from eawf.kernel.store.tiers import StorageTier
from eawf.workflow.integration.publish_binding import (
    STAGED_TREE_AMBIGUOUS,
    STAGED_TREE_UNPUBLISHED,
    PublishedCommit,
    StagedBinding,
    StagedBindingRefusedError,
    bind_staged,
    stage_binding,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    BATCH_URN,
    TASK_URN,
    document_path,
)
from tests.integration.runtime.daemon.test_delivery_landed_loop import (
    _adopt,
    git,
    landed_canary,
)

TREE = "a" * 40
SCRATCH = "b" * 40
PUBLISHED = "c" * 40
OTHER = "d" * 40
AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _staged(**overrides: Any) -> StagedBinding:
    """Return a valid staged binding with *overrides* applied."""
    fields: dict[str, Any] = {
        "batch_ref": BATCH_URN,
        "task_refs": [TASK_URN],
        "tree_sha": TREE,
        "head_sha": SCRATCH,
        "report_verdict": "pass",
        "evidence_refs": ["A-LANDED-01"],
        "staged_by": "OP-0001",
        "staged_at": AT,
        **overrides,
    }
    return StagedBinding.model_validate(fields)


def _snapshot(directory: Path) -> dict[str, bytes]:
    """Return every file under *directory* by relative path, with its bytes."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _publish_default_branch(repo: Path, at: str) -> None:
    """Point the remote's default branch at *at*, as a clone records it."""
    git(repo, "update-ref", "refs/remotes/origin/main", at)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


# ---- adoption stages scratch work ------------------------------------------


def test_adopting_a_scratch_commit_stages_and_appends_no_ledger(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)
    _publish_default_branch(canary.root, base)
    generation = document_path(canary).parent
    ledgers = _snapshot(generation / "ledger")

    answer = _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    assert _snapshot(generation / "ledger") == ledgers
    files = sorted(staged_dir(document_path(canary)).glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == generation / LOCAL_DIRNAME / STAGED_DIRNAME
    row = StagedBinding.model_validate_json(files[0].read_bytes())
    assert row.head_sha == head
    assert row.tree_sha == git(canary.root, "rev-parse", f"{head}^{{tree}}")
    assert [str(ref) for ref in row.task_refs] == [TASK_URN]
    assert str(row.batch_ref) == BATCH_URN
    assert answer["generation"] is None
    assert answer["generation_id"] is None
    assert answer["staged_path"] == files[0].relative_to(canary.root / ".ea").as_posix()


def test_adopting_a_scratch_commit_twice_replays_the_staged_answer(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)
    _publish_default_branch(canary.root, base)

    first = _adopt(canary, tmp_path, base_commit=base, head_sha=head)
    again = _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    assert again == {**first, "replayed": True}
    assert len(list(staged_dir(document_path(canary)).glob("*.json"))) == 1


def test_adopting_a_published_commit_binds_and_stages_nothing(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)
    _publish_default_branch(canary.root, head)

    answer = _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    assert answer["generation"] == 2
    assert answer["staged_path"] is None
    assert not staged_dir(document_path(canary)).exists()


def test_adopting_without_a_remote_default_branch_binds_at_once(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)

    answer = _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    assert answer["generation"] == 2
    assert not staged_dir(document_path(canary)).exists()


# ---- the staged directory stays local -----------------------------------------


def test_staged_dir_sits_in_the_local_tier(tmp_path: Path) -> None:
    state = tmp_path / ".ea" / "generations" / "gen-x" / "state.json"
    assert staged_dir(state) == state.parent / "local" / "staged"


@pytest.mark.parametrize(
    "path",
    [
        ".ea/generations/gen-x/local/staged/a.json",
        ".ea/generations/gen-0123456789abcdef/local/staged/BAT-0001-" + TREE + ".json",
    ],
)
def test_staged_rows_are_never_committed(path: str) -> None:
    row = classify_path(path)
    assert row.policy is CommitPolicy.NOT_COMMITTED
    assert row.tier is StorageTier.LOCAL_STORE


def test_stage_binding_writes_one_file_per_batch_and_tree(tmp_path: Path) -> None:
    directory = tmp_path / "staged"
    other_task = f"{TASK_URN.rsplit('/', 1)[0]}/EAWF-0043"

    first = stage_binding(directory, _staged())
    amended = stage_binding(directory, _staged(head_sha=OTHER))
    other_tree = stage_binding(directory, _staged(tree_sha=OTHER, task_refs=[other_task]))

    assert first == amended
    assert first.name == f"BAT-0007-{TREE}.json"
    assert StagedBinding.model_validate_json(first.read_bytes()).head_sha == OTHER
    assert other_tree != first
    assert sorted(path.name for path in directory.glob("*.json")) == sorted(
        [first.name, other_tree.name]
    )


def test_restaging_the_same_tasks_on_a_new_tree_replaces_the_stale_row(tmp_path: Path) -> None:
    directory = tmp_path / "staged"
    stage_binding(directory, _staged())

    again = stage_binding(directory, _staged(tree_sha=OTHER, head_sha=PUBLISHED))

    assert [path.name for path in directory.glob("*.json")] == [again.name]


def test_restaging_a_superset_of_the_tasks_replaces_the_narrower_row(tmp_path: Path) -> None:
    directory = tmp_path / "staged"
    other_task = f"{TASK_URN.rsplit('/', 1)[0]}/EAWF-0043"
    stage_binding(directory, _staged())

    wider = stage_binding(directory, _staged(tree_sha=OTHER, task_refs=[TASK_URN, other_task]))

    assert [path.name for path in directory.glob("*.json")] == [wider.name]


def test_staging_keeps_a_row_carrying_a_task_the_new_row_does_not(tmp_path: Path) -> None:
    directory = tmp_path / "staged"
    other_task = f"{TASK_URN.rsplit('/', 1)[0]}/EAWF-0043"
    wider = stage_binding(directory, _staged(task_refs=[TASK_URN, other_task]))

    narrower = stage_binding(directory, _staged(tree_sha=OTHER))

    assert sorted(path.name for path in directory.glob("*.json")) == sorted(
        [wider.name, narrower.name]
    )


def test_staging_leaves_another_batchs_row_alone(tmp_path: Path) -> None:
    directory = tmp_path / "staged"
    other_batch = f"{BATCH_URN.rsplit('/', 1)[0]}/BAT-0008"
    kept = stage_binding(directory, _staged(batch_ref=other_batch))

    staged = stage_binding(directory, _staged(tree_sha=OTHER))

    assert sorted(path.name for path in directory.glob("*.json")) == sorted(
        [kept.name, staged.name]
    )


# ---- the matcher binds by tree ------------------------------------------------


def test_bind_staged_binds_the_one_commit_carrying_the_tree() -> None:
    bound = bind_staged(
        _staged(),
        [
            PublishedCommit(sha=OTHER, tree_sha=OTHER),
            PublishedCommit(sha=PUBLISHED, tree_sha=TREE),
        ],
    )
    assert bound.head_sha == PUBLISHED
    assert bound.staged == _staged()


def test_bind_staged_accepts_the_same_commit_offered_twice() -> None:
    commit = PublishedCommit(sha=PUBLISHED, tree_sha=TREE)
    assert bind_staged(_staged(), [commit, commit]).head_sha == PUBLISHED


def test_bind_staged_refuses_no_candidates() -> None:
    with pytest.raises(StagedBindingRefusedError, match=STAGED_TREE_UNPUBLISHED) as caught:
        bind_staged(_staged(), [])
    assert caught.value.code == STAGED_TREE_UNPUBLISHED


def test_bind_staged_never_matches_on_the_scratch_head() -> None:
    with pytest.raises(StagedBindingRefusedError) as caught:
        bind_staged(_staged(), [PublishedCommit(sha=SCRATCH, tree_sha=OTHER)])
    assert caught.value.code == STAGED_TREE_UNPUBLISHED
    assert "BAT-0007" in str(caught.value)


def test_bind_staged_refuses_several_commits_carrying_the_tree() -> None:
    with pytest.raises(StagedBindingRefusedError) as caught:
        bind_staged(
            _staged(),
            [
                PublishedCommit(sha=PUBLISHED, tree_sha=TREE),
                PublishedCommit(sha=OTHER, tree_sha=TREE),
            ],
        )
    assert caught.value.code == STAGED_TREE_AMBIGUOUS
    assert PUBLISHED in str(caught.value) and OTHER in str(caught.value)


def test_staged_binding_refusal_is_a_value_error() -> None:
    assert issubclass(StagedBindingRefusedError, ValueError)


# ---- the staged row's contract ------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_refs": []},
        {"evidence_refs": []},
        {"tree_sha": "a" * 39},
        {"head_sha": "A" * 40},
        {"staged_by": "op"},
        {"staged_at": datetime(2026, 10, 6, 12, 0)},
        {"unexpected": 1},
        {"task_refs": "not-a-list"},
    ],
)
def test_staged_binding_refuses_a_malformed_row(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _staged(**overrides)


def test_staged_binding_requires_every_field() -> None:
    fields = _staged().model_dump(mode="json")
    del fields["tree_sha"]
    with pytest.raises(ValidationError):
        StagedBinding.model_validate(fields)


def test_published_commit_refuses_a_short_sha() -> None:
    with pytest.raises(ValidationError):
        PublishedCommit(sha="c" * 41, tree_sha=TREE)
