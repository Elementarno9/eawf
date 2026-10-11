"""``eawf batch squash`` writes a branch again as one commit per Batch and a state commit.

A scratch repository carries Task commits of two Batches, interleaved with a
``state:`` commit, past its published base. The squash writes one commit per
Batch, ordered by each Batch's last source commit and naming the Batch and its
Tasks by trailer, then one trailing ``state:`` commit whose tree is the source
tip's. The source branch never moves, and the answer maps every source commit
to the commit now carrying it.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.workflow.integration.publish import (
    PUBLISH_MERGE_COMMIT,
    PublishRefusedError,
    ScratchCommit,
)
from eawf.workflow.integration.squash import (
    SQUASH_EMPTY_RANGE,
    SQUASH_TARGET_EXISTS,
    SQUASH_UNPLACEABLE,
    SquashGroup,
    SquashMap,
    group_message,
    plan_squash,
    squash,
)
from tests.integration.workflow.integration.test_publish import make_repo

pytestmark = pytest.mark.integration

runner = CliRunner()

BRANCH = "feature/work"
TASKS = {"EAWF-0042": "BAT-0007", "EAWF-0043": "BAT-0008", "EAWF-0044": "BAT-0007"}


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every temporary directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    """Write *files*, commit them with *message* and return the commit."""
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def new_repo(tmp_path: Path) -> tuple[Path, str]:
    """Return a repository on ``feature/work`` and its published base commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "ci@example.com")
    git(repo, "config", "user.name", "ci")
    git(repo, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    base = commit(repo, {"src/f.py": "x = 1\n", ".ea/state.json": "{}\n"}, "base")
    git(repo, "checkout", "-q", "-b", BRANCH)
    return repo, base


def interleaved(tmp_path: Path) -> tuple[Path, str, list[str]]:
    """Two Batches' Task commits interleaved with a state commit, past *base*."""
    repo, base = new_repo(tmp_path)
    shas = [
        commit(repo, {"src/a.py": "a = 1\n"}, "feat: add a\n\nTask: EAWF-0042"),
        commit(repo, {"src/b.py": "b = 1\n"}, "fix: add b\n\nTask: EAWF-0043"),
        commit(repo, {"src/a2.py": "a = 2\n"}, "feat: add a2\n\nTask: EAWF-0044"),
        commit(repo, {".ea/state.json": '{"n": 1}\n'}, "state: record the Tasks"),
        commit(repo, {"src/b.py": "b = 2\n"}, "fix: bump b\n\nTask: EAWF-0043"),
    ]
    return repo, base, shas


def _message(repo: Path, sha: str) -> str:
    return git(repo, "show", "-s", "--format=%B", sha)


def test_squash_writes_one_commit_per_batch_and_a_trailing_state_commit(tmp_path: Path) -> None:
    repo, base, shas = interleaved(tmp_path)

    result = squash(
        repo,
        base=base,
        source="HEAD",
        branch=None,
        task_batches=TASKS,
        into="scratch/squashed",
        on_conflict="merge",
    )

    assert git(repo, "rev-parse", BRANCH) == shas[-1]
    assert result.tree_equal
    assert git(repo, "rev-parse", "scratch/squashed^{tree}") == git(
        repo, "rev-parse", "HEAD^{tree}"
    )
    rewritten = git(repo, "rev-list", "--reverse", f"{base}..scratch/squashed").splitlines()
    assert rewritten == [item.sha for item in result.commits]
    assert [item.batch_keys for item in result.commits] == [("BAT-0007",), ("BAT-0008",), ()]
    assert _message(repo, rewritten[0]).endswith(
        "Batch: BAT-0007\nTask: EAWF-0042\nTask: EAWF-0044"
    )
    assert _message(repo, rewritten[1]).startswith("fix: add b\n\n- fix: add b\n- fix: bump b")
    assert _message(repo, rewritten[2]) == "state: record the Tasks"
    assert result.merged == ()
    assert result.map == SquashMap(
        branch=BRANCH,
        base=base,
        source_head=shas[-1],
        head=rewritten[2],
        commits={
            shas[0]: rewritten[0],
            shas[2]: rewritten[0],
            shas[1]: rewritten[1],
            shas[4]: rewritten[1],
            shas[3]: rewritten[2],
        },
        batches={"BAT-0007": rewritten[0], "BAT-0008": rewritten[1]},
    )


def test_a_plan_only_squash_creates_no_branch(tmp_path: Path) -> None:
    repo, base, _ = interleaved(tmp_path)
    refs = git(repo, "for-each-ref")

    result = squash(
        repo,
        base=base,
        source="HEAD",
        branch=BRANCH,
        task_batches=TASKS,
        into=None,
        on_conflict="merge",
    )

    assert result.into is None
    assert result.tree_equal
    assert git(repo, "for-each-ref") == refs


def _rests_on_a_later_batch(tmp_path: Path) -> tuple[Path, str, list[str]]:
    """BAT-0007's only commit rewrites a line BAT-0008 wrote before it."""
    repo, base = new_repo(tmp_path)
    shas = [
        commit(repo, {"src/f.py": "x = 2\n"}, "fix: set x\n\nTask: EAWF-0043"),
        commit(repo, {"src/f.py": "x = 3\n"}, "feat: raise x\n\nTask: EAWF-0042"),
        commit(repo, {"src/g.py": "g = 1\n"}, "fix: add g\n\nTask: EAWF-0043"),
    ]
    return repo, base, shas


def test_merge_joins_two_batches_whose_work_does_not_replay_apart(tmp_path: Path) -> None:
    repo, base, _ = _rests_on_a_later_batch(tmp_path)

    result = squash(
        repo, base=base, source="HEAD", branch=None, task_batches=TASKS, into=None,
        on_conflict="merge",
    )  # fmt: skip

    assert result.tree_equal
    assert result.merged == (("BAT-0007", "BAT-0008"),)
    assert [item.batch_keys for item in result.commits] == [("BAT-0007", "BAT-0008")]


def test_carry_keeps_the_batches_apart_and_names_the_carried_path(tmp_path: Path) -> None:
    repo, base, shas = _rests_on_a_later_batch(tmp_path)

    result = squash(
        repo, base=base, source="HEAD", branch=None, task_batches=TASKS, into="scratch/c",
        on_conflict="carry",
    )  # fmt: skip

    assert result.tree_equal
    assert result.merged == ()
    first, second = result.commits
    assert (first.batch_keys, first.carried) == (("BAT-0007",), ("src/f.py",))
    assert (second.batch_keys, second.carried) == (("BAT-0008",), ("src/f.py",))
    assert git(repo, "show", f"{first.sha}:src/f.py") == "x = 3"
    assert result.map.commits == {shas[1]: first.sha, shas[0]: second.sha, shas[2]: second.sha}


def test_a_commit_naming_no_batch_is_folded_into_the_one_before(tmp_path: Path) -> None:
    repo, base = new_repo(tmp_path)
    commit(repo, {"src/a.py": "a\n"}, "feat: add a\n\nTask: EAWF-0042")
    stray = commit(repo, {"docs/a.md": "a\n"}, "docs: describe a")
    commit(repo, {"src/b.py": "b\n"}, "fix: add b\n\nTask: EAWF-0043")

    result = squash(
        repo, base=base, source="HEAD", branch=None, task_batches=TASKS, into=None,
        on_conflict="merge",
    )  # fmt: skip

    assert result.commits[0].folded == (f"{stray[:12]} docs: describe a",)
    assert result.map.commits[stray] == result.commits[0].sha


def test_a_state_commit_straddling_ea_replays_its_other_paths_with_a_batch(
    tmp_path: Path,
) -> None:
    repo, base = new_repo(tmp_path)
    commit(repo, {"src/a.py": "a\n"}, "feat: add a\n\nTask: EAWF-0042")
    straddle = commit(
        repo, {".ea/state.json": '{"n": 2}\n', ".secrets.baseline": "{}\n"}, "state: refresh"
    )
    commit(repo, {".secrets.baseline": '{"v": 1}\n'}, "fix: tune the baseline\n\nTask: EAWF-0042")

    result = squash(
        repo, base=base, source="HEAD", branch=None, task_batches=TASKS, into=None,
        on_conflict="merge",
    )  # fmt: skip

    batch_commit, state_commit = result.commits
    assert result.tree_equal
    assert git(repo, "show", f"{batch_commit.sha}:.secrets.baseline") == '{"v": 1}'
    assert git(repo, "show", f"{batch_commit.sha}:.ea/state.json") == "{}"
    assert state_commit.check_failures == ()
    assert result.map.commits[straddle] == state_commit.sha


def test_squash_refusals_leave_every_ref_as_it_was(tmp_path: Path) -> None:
    repo, base, _ = interleaved(tmp_path)
    git(repo, "branch", "scratch/taken")
    refs = git(repo, "for-each-ref")

    with pytest.raises(PublishRefusedError) as exists:
        squash(
            repo, base=base, source="HEAD", branch=None, task_batches=TASKS,
            into="scratch/taken", on_conflict="merge",
        )  # fmt: skip
    with pytest.raises(PublishRefusedError) as empty:
        squash(
            repo, base="HEAD", source="HEAD", branch=None, task_batches=TASKS, into=None,
            on_conflict="merge",
        )  # fmt: skip
    with pytest.raises(PublishRefusedError) as unplaced:
        squash(
            repo, base=base, source="HEAD", branch=None, task_batches={}, into=None,
            on_conflict="merge",
        )  # fmt: skip

    assert (exists.value.code, empty.value.code, unplaced.value.code) == (
        SQUASH_TARGET_EXISTS,
        SQUASH_EMPTY_RANGE,
        SQUASH_UNPLACEABLE,
    )
    assert git(repo, "for-each-ref") == refs


def test_squash_refuses_a_merge_commit(tmp_path: Path) -> None:
    repo, base = new_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"src/s.py": "s\n"}, "feat: side\n\nTask: EAWF-0042")
    git(repo, "checkout", "-q", BRANCH)
    commit(repo, {"src/m.py": "m\n"}, "feat: main\n\nTask: EAWF-0042")
    git(repo, "merge", "-q", "--no-edit", "side")

    with pytest.raises(PublishRefusedError) as refused:
        squash(
            repo, base=base, source="HEAD", branch=None, task_batches=TASKS, into=None,
            on_conflict="merge",
        )  # fmt: skip

    assert refused.value.code == PUBLISH_MERGE_COMMIT


def test_each_check_runs_on_every_rewritten_commit_and_names_what_failed(
    tmp_path: Path,
) -> None:
    repo, base, _ = interleaved(tmp_path)

    result = squash(
        repo, base=base, source="HEAD", branch=None, task_batches=TASKS, into=None,
        on_conflict="merge", checks=("test -f src/a.py", "test -f src/b.py", "true"),
    )  # fmt: skip

    assert [item.check_failures for item in result.commits] == [
        ("test -f src/b.py: exit 1",),
        (),
        (),
    ]


def test_tidy_rewrites_an_intermediate_snapshot_and_leaves_the_tip_tree(tmp_path: Path) -> None:
    repo, base, shas = interleaved(tmp_path)

    result = squash(
        repo, base=base, source="HEAD", branch=None, task_batches=TASKS, into=None,
        on_conflict="carry", tidy=("cp src/a.py src/a2.py", "touch stray.txt"),
        checks=("test ! -e stray.txt",),
    )  # fmt: skip

    first, last, state = result.commits
    assert git(repo, "show", f"{first.sha}:src/a2.py") == "a = 1"
    assert git(repo, "show", f"{last.sha}:src/a2.py") == "a = 2"
    assert [item.check_failures for item in result.commits] == [(), (), ()]
    assert result.tree_equal
    assert git(repo, "rev-parse", f"{state.sha}^{{tree}}") == git(repo, "rev-parse", "HEAD^{tree}")
    assert git(repo, "rev-parse", f"{state.sha}^") == last.sha
    assert shas[-1] == git(repo, "rev-parse", "HEAD")


def test_tidy_is_skipped_for_a_single_batch(tmp_path: Path) -> None:
    repo, base = new_repo(tmp_path)
    commit(repo, {"src/a.py": "a\n"}, "feat: add a\n\nTask: EAWF-0042")

    result = squash(
        repo, base=base, source="HEAD", branch=None, task_batches=TASKS, into=None,
        on_conflict="carry", tidy=("cp src/f.py src/a.py",),
    )  # fmt: skip

    assert git(repo, "show", f"{result.commits[0].sha}:src/a.py") == "a"
    assert result.tree_equal


# ---- the pure planning half --------------------------------------------------


def _row(index: int, message: str) -> ScratchCommit:
    return ScratchCommit(sha=f"{index:040x}", tree_sha="0" * 40, message=message)


def test_plan_of_one_commit_is_one_group() -> None:
    only = _row(1, "feat: a\n\nTask: EAWF-0042")

    plan = plan_squash("b" * 40, only.sha, [only], TASKS)

    assert plan.groups == (SquashGroup(batch_keys=("BAT-0007",), commits=(only,)),)
    assert plan.state_commits == ()


def test_plan_joins_batches_a_commit_names_together_and_orders_by_last_commit() -> None:
    rows = [
        _row(1, "feat: a\n\nTask: EAWF-0042"),
        _row(2, "fix: b\n\nTask: EAWF-0043"),
        _row(3, "chore: c\n\nBatch: BAT-0009"),
        _row(4, "feat: both\n\nTask: EAWF-0042\nTask: EAWF-0043"),
        _row(5, "state: x"),
    ]

    plan = plan_squash("b" * 40, rows[-1].sha, rows, TASKS)

    assert [group.batch_keys for group in plan.groups] == [("BAT-0009",), ("BAT-0007", "BAT-0008")]
    assert plan.groups[1].commits == (rows[0], rows[1], rows[3])
    assert plan.state_commits == (rows[4],)


def test_plan_folds_a_leading_unassigned_commit_into_the_first_batch() -> None:
    rows = [_row(1, "docs: lead"), _row(2, "feat: a\n\nTask: EAWF-0042")]

    plan = plan_squash("b" * 40, rows[-1].sha, rows, TASKS)

    assert plan.groups[0].folded == (rows[0],)
    assert plan.groups[0].commits == tuple(rows)


def test_plan_refuses_an_empty_range_and_a_range_with_no_batch() -> None:
    with pytest.raises(PublishRefusedError, match=SQUASH_EMPTY_RANGE):
        plan_squash("b" * 40, "b" * 40, [], TASKS)
    with pytest.raises(PublishRefusedError, match=SQUASH_UNPLACEABLE):
        plan_squash("b" * 40, "c" * 40, [_row(1, "docs: x")], TASKS)


def test_group_message_keeps_a_single_commit_and_replaces_its_trailers() -> None:
    row = _row(1, "fix: b\n\nWhy.\n\nTask: EAWF-0043\nCo-Authored-By: X <x@example.com>\n")
    group = SquashGroup(batch_keys=("BAT-0008",), commits=(row,))

    assert group_message(group, ["EAWF-0043"]) == (
        "fix: b\n\nWhy.\n\nBatch: BAT-0008\nTask: EAWF-0043\n"
    )


def test_group_message_lists_at_most_eight_subjects() -> None:
    rows = tuple(_row(i, f"{'feat' if i == 5 else 'fix'}: step {i}") for i in range(10))
    group = SquashGroup(batch_keys=("BAT-0007",), commits=rows)

    message = group_message(group, ["EAWF-0042"])

    assert message.startswith("feat: step 5\n\n- fix: step 0\n")
    assert "- fix: step 7\n- and 2 more\n\nBatch: BAT-0007\nTask: EAWF-0042\n" in message


# ---- the CLI ------------------------------------------------------------------


def test_cli_squashes_into_a_new_branch_and_writes_the_map(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    map_file = tmp_path / "map.json"

    result = runner.invoke(
        app,
        [
            "--workspace", str(made.root), "--json", "batch", "squash", "--onto", "origin/main",
            "--on-conflict", "merge", "--into", "scratch/squashed", "--map-out", str(map_file),
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    answer = orjson.loads(result.stdout)["result"]
    assert answer["tree_equal"] is True
    assert [item["batch_keys"] for item in answer["commits"]] == [["BAT-0007"], ["BAT-0008"]]
    loaded = SquashMap.model_validate_json(map_file.read_bytes())
    assert loaded.head == git(made.root, "rev-parse", "scratch/squashed")
    assert git(made.root, "rev-parse", "HEAD") == made.tip


def test_cli_refuses_an_unknown_conflict_strategy(tmp_path: Path) -> None:
    made = make_repo(tmp_path)

    result = runner.invoke(
        app,
        [
            "--workspace", str(made.root), "batch", "squash", "--onto", "origin/main",
            "--on-conflict", "rebase",
        ],
    )  # fmt: skip

    assert result.exit_code != 0
    assert "--on-conflict must be 'merge' or 'carry'" in result.output
