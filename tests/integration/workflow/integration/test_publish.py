"""``eawf publish`` keeps every per-Task commit and binds the staged adoptions to them.

A fixture repository carries five scratch commits across two Batches past
its remote default branch, with each Batch's adoption staged at the
commit that ends its work. Publish rewrites none of them: it binds the
staged rows to the commits carrying their trees through the daemon, adds
one ``state:`` commit holding the rows that writes, built through ``git
commit`` so the hooks run, and changes nothing when run again. It refuses,
changing nothing, a dirty tree and a range it cannot place in Batches.
"""

from __future__ import annotations

import asyncio
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.store.commit_policy import CommitPolicy, classify_path
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path, staged_dir
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.publish import PUBLISH_BIND_STAGED_METHOD
from eawf.surfaces.cli.app import app
from eawf.workflow.integration.publish import (
    PUBLISH_COMMIT_MIXED,
    PUBLISH_COMMIT_UNASSIGNED,
    PUBLISH_DETACHED_HEAD,
    PUBLISH_DIRTY_TREE,
    PUBLISH_MERGE_COMMIT,
    PUBLISH_NO_REMOTE_DEFAULT,
    PUBLISH_TASK_UNPLACED,
    PublishRefusedError,
    ScratchCommit,
    plan_publish,
    publish,
    read_membership,
)
from eawf.workflow.integration.publish_binding import (
    STAGED_TREE_UNPUBLISHED,
    PublishedCommit,
    StagedBinding,
    stage_binding,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    TASK_URN,
    document_path,
    method_context,
    rekeyed,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_delivery_landed_loop import cli
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

__all__ = ["cli"]

runner = CliRunner()

ACTOR = "OP-0001"
AUDIT_ID = "A-PUBLISH-01"
BRANCH = "feature/eawf-v0.7"
BATCH_B_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0008"
TASK_B_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0043"
SHA_RE = re.compile(rb"\b[0-9a-f]{40}\b")


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass(frozen=True)
class Scratch:
    """A repository with scratch work past its remote default branch.

    Attributes:
        canary: The provisioned epoch-2 tree.
        root: The repository.
        hooks: The repository's hooks directory.
        base: The published commit.
        commits: The scratch commits, oldest first.
    """

    canary: CanaryProvision
    root: Path
    hooks: Path
    base: str
    commits: tuple[str, ...]

    @property
    def tip(self) -> str:
        return self.commits[-1]


def _hook(hooks: Path, name: str, body: str) -> None:
    """Install one executable shell hook."""
    path = hooks / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _commit(root: Path, path: str, content: str, message: str) -> str:
    """Write *content* to *path*, commit it with *message* and return the commit."""
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(root, "add", path)
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def _stage(scratch_repo: Scratch, *, batch: str, task: str, at: str) -> Path:
    """Stage an adoption of the scratch commit *at*, as adopt-landed does."""
    row = StagedBinding(
        batch_ref=batch,  # type: ignore[arg-type]
        task_refs=(task,),  # type: ignore[arg-type]
        tree_sha=git(scratch_repo.root, "rev-parse", f"{at}^{{tree}}"),
        head_sha=at,
        report_verdict="pass",  # type: ignore[arg-type]
        evidence_refs=(AUDIT_ID,),
        staged_by=ACTOR,
        staged_at=AT,
    )
    return stage_binding(staged_dir(document_path(scratch_repo.canary)), row)


def make_repo(tmp_path: Path, messages: list[tuple[str, str]] | None = None) -> Scratch:
    """Return a repository whose branch carries scratch commits past origin.

    Args:
        tmp_path: Where the repository is made.
        messages: ``(path, message)`` per scratch commit; omitted, five
            commits across two Batches.
    """
    root = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    root.mkdir()
    hooks.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "ci@example.com")
    git(root, "config", "user.name", "ci")
    # A local hooks path keeps the run hermetic and lets a test install hooks.
    git(root, "config", "core.hooksPath", str(hooks))
    # The machine-local tiers the repository's own ignore file keeps out.
    (root / ".gitignore").write_text(
        "\n".join(
            [
                ".ea/**/*.lock",
                ".ea/local/",
                ".ea/generations/gen-*/local/",
                ".ea/generations/gen-*/indexes/",
                ".ea/store/",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (root / "src").mkdir()
    (root / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")
    canary = provision_canary(repo_root=root, ref=canary_ref("PUB"), provisioned_at=AT)
    milestone = seed_row("milestone", "ACTIVE")
    milestone["required_batch_refs"] = [BATCH_URN, BATCH_B_URN]
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {"MLS-0030": milestone},
            "batch": {
                "BAT-0007": seed_row("batch", "ACTIVE"),
                "BAT-0008": rekeyed(seed_row("batch", "ACTIVE"), key="BAT-0008"),
            },
            "task": {
                "EAWF-0042": world.task_row(status=TaskStatus.RUNNING),
                "EAWF-0043": world.task_row(
                    urn=TASK_B_URN, batch_ref=BATCH_B_URN, status=TaskStatus.RUNNING
                ),
            },
        },
    )
    append_ledger_record(
        ledger_path(document_path(canary), Epoch2Collection.AUDIT),
        LedgerRecord(
            collection=Epoch2Collection.AUDIT,
            record_key=AUDIT_ID,
            status="appended",
            recorded_at=AT,
            payload={"payload": {"id": AUDIT_ID}},
        ),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", base)
    git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(root, "checkout", "-q", "-b", BRANCH)
    if messages is None:
        messages = [
            ("src/alpha.py", "feat: add the alpha module\n\nTask: EAWF-0042"),
            ("src/alpha.py", "wip alpha\n\nTask: EAWF-0042"),
            ("src/beta.py", "fix: correct the beta module\n\nTask: EAWF-0043"),
            ("src/beta.py", "more beta\n\nTask: EAWF-0043"),
            ("tests/test_beta.py", "beta tests\n\nTask: EAWF-0043"),
        ]
    commits = tuple(
        _commit(root, path, f"# {index}\n", message)
        for index, (path, message) in enumerate(messages)
    )
    return Scratch(canary=canary, root=root, hooks=hooks, base=base, commits=commits)


@pytest.fixture
def scratch(tmp_path: Path) -> Scratch:
    """Five scratch commits across two Batches, each Batch's adoption staged."""
    made = make_repo(tmp_path)
    _stage(made, batch=BATCH_URN, task=TASK_URN, at=made.commits[1])
    _stage(made, batch=BATCH_B_URN, task=TASK_B_URN, at=made.commits[4])
    return made


def eawf_publish(root: Path) -> tuple[int, dict[str, Any]]:
    """Run ``eawf --json publish`` and return its exit code and JSON answer."""
    result = runner.invoke(app, ["--workspace", str(root), "--json", "publish"])
    return result.exit_code, orjson.loads(result.stdout)


def _snapshot(root: Path) -> dict[str, Any]:
    """Return everything a refused publish must leave as it was."""
    generation = root / ".ea" / "generations"
    return {
        "head": git(root, "rev-parse", "HEAD"),
        "branch": git(root, "symbolic-ref", "HEAD"),
        "refs": git(root, "for-each-ref", "--format=%(refname) %(objectname)"),
        "status": git(root, "status", "--porcelain"),
        "files": {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(generation.rglob("*"))
            if path.is_file() and not path.name.endswith(".lock")
        },
    }


def _range(root: Path, base: str) -> list[str]:
    """Return ``base..HEAD``, oldest first."""
    return git(root, "rev-list", "--reverse", f"{base}..HEAD").splitlines()


def _no_bind(_branch: str, _targets: object) -> int:
    raise AssertionError("nothing is staged, so nothing is bound")


# ---- CR-1: every commit kept, then one state commit ------------------------


def test_publish_keeps_the_five_commits_and_adds_one_state_commit(
    scratch: Scratch, cli: None
) -> None:
    _hook(scratch.hooks, "pre-commit", f'echo ran >> "{scratch.root.parent}/hook.log"')

    code, answer = eawf_publish(scratch.root)

    assert code == 0, answer
    published = _range(scratch.root, scratch.base)
    assert published[:-1] == list(scratch.commits)
    state = published[-1]
    assert git(scratch.root, "show", "-s", "--format=%s", state).startswith("state: ")
    state_paths = git(scratch.root, "diff-tree", "--no-commit-id", "--name-only", "-r", state)
    assert state_paths
    assert all(
        classify_path(path).policy is CommitPolicy.COMMITTED for path in state_paths.splitlines()
    )
    # The state commit adds bookkeeping only, so outside .ea the tip is the scratch tip.
    assert git(scratch.root, "diff", "--name-only", scratch.tip, "HEAD", "--", ".", ":!.ea") == ""
    assert answer["batch_commits"] == {
        "BAT-0007": scratch.commits[1],
        "BAT-0008": scratch.commits[4],
    }
    assert "archive_ref" not in answer
    assert answer["bound"] == 2
    assert git(scratch.root, "symbolic-ref", "--short", "HEAD") == BRANCH
    assert git(scratch.root, "status", "--porcelain") == ""
    # Only the state commit was made, and it ran the hooks.
    assert (scratch.root.parent / "hook.log").read_text(encoding="utf-8").count("ran") == 1


# ---- CR-2: bound SHAs are published, and a rerun is a no-op ----------------


def test_bound_shas_are_ancestors_of_head_and_a_second_publish_is_a_noop(
    scratch: Scratch, cli: None
) -> None:
    batch_ledger = ledger_path(document_path(scratch.canary), Epoch2Collection.BATCH)
    before = batch_ledger.read_bytes() if batch_ledger.exists() else b""

    code, answer = eawf_publish(scratch.root)

    assert code == 0, answer
    added = batch_ledger.read_bytes()
    assert added.startswith(before)
    bound = {
        sha.decode()
        for sha in SHA_RE.findall(added[len(before) :])
        if subprocess.run(
            ["git", "cat-file", "-e", f"{sha.decode()}^{{commit}}"],
            cwd=scratch.root,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    }
    batch_commits = set(answer["batch_commits"].values())
    assert batch_commits <= bound
    assert batch_commits <= set(scratch.commits)
    for sha in bound:
        assert (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                cwd=scratch.root,
                check=False,
            ).returncode
            == 0
        ), sha
    assert list(staged_dir(document_path(scratch.canary)).glob("*.json")) == []

    snapshot = _snapshot(scratch.root)
    code, again = eawf_publish(scratch.root)

    assert code == 0, again
    assert again["noop"] is True
    assert again["batch_commits"] == answer["batch_commits"]
    assert _snapshot(scratch.root) == snapshot


def test_publish_without_staged_rows_writes_no_state_commit(tmp_path: Path) -> None:
    made = make_repo(tmp_path)

    result = publish(made.root, membership=read_membership(made.root / ".ea"), bind=_no_bind)

    assert result.noop and result.state_commit is None and result.bound == 0
    assert _range(made.root, made.base) == list(made.commits)
    assert result.new_tip == made.tip == git(made.root, "rev-parse", "HEAD")


def test_publish_of_an_empty_range_is_a_noop(tmp_path: Path) -> None:
    made = make_repo(tmp_path, messages=[])

    result = publish(made.root, membership=read_membership(made.root / ".ea"), bind=_no_bind)

    assert result.noop and result.batch_commits == {}
    assert result.new_tip == made.base


def test_state_commits_stay_where_they_were_made(tmp_path: Path) -> None:
    made = make_repo(
        tmp_path,
        messages=[
            (".ea/notes/a.txt", "state: record a note"),
            ("src/alpha.py", "feat: add alpha\n\nTask: EAWF-0042"),
            (".ea/notes/b.txt", "state: record b"),
            (".ea/notes/c.txt", "state: record c"),
        ],
    )

    result = publish(made.root, membership=read_membership(made.root / ".ea"), bind=_no_bind)

    assert _range(made.root, made.base) == list(made.commits)
    assert result.batch_commits == {"BAT-0007": made.commits[1]}
    assert result.noop


# ---- CR-3: refusals change nothing -------------------------------------------


def test_publish_refuses_a_dirty_tree_and_changes_nothing(scratch: Scratch, cli: None) -> None:
    (scratch.root / "src" / "module.py").write_text("x = 99\n", encoding="utf-8")
    snapshot = _snapshot(scratch.root)

    code, answer = eawf_publish(scratch.root)

    assert code != 0
    assert PUBLISH_DIRTY_TREE in orjson.dumps(answer).decode()
    assert _snapshot(scratch.root) == snapshot


def test_publish_refuses_a_staged_tree_no_batch_commit_carries(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    # The tree of a BAT-0008 commit, which no commit of BAT-0007 carries.
    _stage(made, batch=BATCH_URN, task=TASK_URN, at=made.commits[2])
    snapshot = _snapshot(made.root)

    with pytest.raises(PublishRefusedError) as caught:
        publish(made.root, membership=read_membership(made.root / ".ea"), bind=_no_bind)

    assert caught.value.code == STAGED_TREE_UNPUBLISHED
    assert _snapshot(made.root) == snapshot


@pytest.mark.parametrize(
    ("messages", "code"),
    [
        ([("src/a.py", "no trailer at all")], PUBLISH_COMMIT_UNASSIGNED),
        ([("src/a.py", "feat: a\n\nTask: EAWF-9999")], PUBLISH_TASK_UNPLACED),
        ([("src/a.py", "feat: a\n\nTask: EAWF-0042\nTask: EAWF-0043")], PUBLISH_COMMIT_MIXED),
    ],
)
def test_publish_refuses_a_range_it_cannot_place(
    tmp_path: Path, messages: list[tuple[str, str]], code: str
) -> None:
    made = make_repo(tmp_path, messages=messages)
    snapshot = _snapshot(made.root)

    with pytest.raises(PublishRefusedError) as caught:
        publish(made.root, membership=read_membership(made.root / ".ea"), bind=_no_bind)

    assert caught.value.code == code
    assert _snapshot(made.root) == snapshot


def test_publish_refuses_a_detached_head(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    git(made.root, "checkout", "-q", "--detach", made.tip)

    with pytest.raises(PublishRefusedError) as caught:
        publish(made.root, membership=read_membership(made.root / ".ea"), bind=_no_bind)

    assert caught.value.code == PUBLISH_DETACHED_HEAD


def test_publish_refuses_without_a_remote_default_branch(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    git(made.root, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

    with pytest.raises(PublishRefusedError) as caught:
        publish(made.root, membership=read_membership(made.root / ".ea"), bind=_no_bind)

    assert caught.value.code == PUBLISH_NO_REMOTE_DEFAULT


def test_publish_refuses_a_merge_commit(tmp_path: Path) -> None:
    made = make_repo(tmp_path, messages=[("src/a.py", "feat: a\n\nTask: EAWF-0042")])
    git(made.root, "checkout", "-q", "-b", "side", made.base)
    _commit(made.root, "src/side.py", "s\n", "feat: side\n\nTask: EAWF-0042")
    git(made.root, "checkout", "-q", BRANCH)
    git(made.root, "merge", "-q", "--no-ff", "-m", "merge side", "side")

    with pytest.raises(PublishRefusedError) as caught:
        publish(made.root, membership=read_membership(made.root / ".ea"), bind=_no_bind)

    assert caught.value.code == PUBLISH_MERGE_COMMIT


def test_publish_refusal_is_a_value_error() -> None:
    assert issubclass(PublishRefusedError, ValueError)


# ---- the plan and the message, without a repository -------------------------


def _commit_row(index: int, message: str) -> ScratchCommit:
    return ScratchCommit(sha=f"{index:040x}", tree_sha=f"{index + 100:040x}", message=message)


def test_plan_of_no_commits_has_no_batches() -> None:
    plan = plan_publish("a" * 40, "a" * 40, [], {})
    assert plan.batches == {}


def test_plan_reads_a_batch_trailer_without_a_task() -> None:
    plan = plan_publish("a" * 40, "b" * 40, [_commit_row(1, "feat: x\n\nBatch: BAT-0007")], {})
    assert list(plan.batches) == ["BAT-0007"]


# ---- the daemon verb ----------------------------------------------------------


def _bind(scratch_repo: Scratch, tmp_path: Path, candidates: list[dict[str, Any]]) -> Any:
    params = {
        "repo_root": str(scratch_repo.root),
        "branch": BRANCH,
        "candidates": candidates,
    }
    return asyncio.run(
        methods.dispatch(PUBLISH_BIND_STAGED_METHOD, method_context(tmp_path / "rt"), params)
    )


def _candidate(batch: str, sha: str, tree: str) -> dict[str, Any]:
    return {"batch_ref": batch, "commits": [PublishedCommit(sha=sha, tree_sha=tree).model_dump()]}


def test_bind_refuses_empty_candidates(scratch: Scratch, tmp_path: Path) -> None:
    with pytest.raises(methods.DaemonValidationError, match="schema_validation_failed"):
        _bind(scratch, tmp_path, [])


def test_bind_refuses_an_unknown_field(scratch: Scratch, tmp_path: Path) -> None:
    params = {"repo_root": str(scratch.root), "branch": BRANCH, "candidates": [], "actor": ACTOR}
    with pytest.raises(methods.DaemonValidationError, match="actor"):
        asyncio.run(
            methods.dispatch(PUBLISH_BIND_STAGED_METHOD, method_context(tmp_path / "rt"), params)
        )


def test_bind_refuses_a_staged_batch_with_no_commits_and_writes_nothing(
    scratch: Scratch, tmp_path: Path
) -> None:
    tip_tree = git(scratch.root, "rev-parse", f"{scratch.tip}^{{tree}}")
    snapshot = _snapshot(scratch.root)

    with pytest.raises(methods.DaemonValidationError, match="publish_batch_unoffered"):
        _bind(scratch, tmp_path, [_candidate(BATCH_B_URN, scratch.tip, tip_tree)])

    assert _snapshot(scratch.root) == snapshot


def test_bind_refuses_a_tree_no_commit_carries(scratch: Scratch, tmp_path: Path) -> None:
    tip_tree = git(scratch.root, "rev-parse", f"{scratch.tip}^{{tree}}")
    snapshot = _snapshot(scratch.root)

    with pytest.raises(methods.DaemonValidationError, match=STAGED_TREE_UNPUBLISHED):
        _bind(
            scratch,
            tmp_path,
            [
                _candidate(BATCH_URN, scratch.tip, tip_tree),
                _candidate(BATCH_B_URN, scratch.tip, tip_tree),
            ],
        )

    assert _snapshot(scratch.root) == snapshot


def test_bind_on_a_batch_already_standing_there_only_unstages(tmp_path: Path) -> None:
    made = make_repo(tmp_path, messages=[("src/a.py", "feat: a\n\nTask: EAWF-0042")])
    tree = git(made.root, "rev-parse", f"{made.tip}^{{tree}}")
    candidate = [_candidate(BATCH_URN, made.tip, tree)]
    _stage(made, batch=BATCH_URN, task=TASK_URN, at=made.tip)
    first = _bind(made, tmp_path, candidate)
    ledger = ledger_path(document_path(made.canary), Epoch2Collection.BATCH).read_bytes()
    _stage(made, batch=BATCH_URN, task=TASK_URN, at=made.tip)

    again = _bind(made, tmp_path, candidate)

    assert first["bound"][0]["replayed"] is False
    assert again["bound"] == [{**first["bound"][0], "replayed": True}]
    assert ledger_path(document_path(made.canary), Epoch2Collection.BATCH).read_bytes() == ledger
    assert list(staged_dir(document_path(made.canary)).glob("*.json")) == []
