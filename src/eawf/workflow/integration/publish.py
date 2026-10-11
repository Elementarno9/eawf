"""Check the commits a branch carries past its remote, and bind the staged adoptions to them.

Commits are published as they were made: one or more per Task, each naming its Task in
a ``Task:`` trailer, in whatever order the Tasks' work was cherry-picked, with
``state:`` commits between them. Nothing is rewritten. A publish checks that every
non-state commit names exactly one Batch through its trailers, binds each staged
adoption by tree to the commit of its Batch that carries that tree, and commits the
ledger rows the binding writes as one trailing ``state:`` commit. The staged bindings
are bound through a caller supplied binder. Nothing here pushes.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.store.commit_policy import CommitPolicy, CommitPolicyError, classify_path
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path, staged_dir
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.workflow.integration.publish_binding import (
    PublishedCommit,
    StagedBinding,
    StagedBindingRefusedError,
    bind_staged,
)

logger = logging.getLogger(__name__)

#: The remote ref whose commits are already published.
PUBLISHED_REF: Final = "refs/remotes/origin/HEAD"

#: Refusal: tracked files differ from ``HEAD``, which a checkout would clobber.
PUBLISH_DIRTY_TREE: Final = "publish_dirty_tree"

#: Refusal: ``HEAD`` names no branch to move.
PUBLISH_DETACHED_HEAD: Final = "publish_detached_head"

#: Refusal: the remote default branch is unknown, so nothing says what is scratch.
PUBLISH_NO_REMOTE_DEFAULT: Final = "publish_no_remote_default"

#: Refusal: a merge commit has no single parent to squash onto.
PUBLISH_MERGE_COMMIT: Final = "publish_merge_commit"

#: Refusal: a non-state commit names no Task or Batch.
PUBLISH_COMMIT_UNASSIGNED: Final = "publish_commit_unassigned"

#: Refusal: a Task trailer names no Task placed in a Batch.
PUBLISH_TASK_UNPLACED: Final = "publish_task_unplaced"

#: Refusal: one commit carries the work of two Batches.
PUBLISH_COMMIT_MIXED: Final = "publish_commit_mixed_batches"

#: Refusal: a git command the publish needs failed.
PUBLISH_GIT_FAILED: Final = "publish_git_failed"

_STATE_SUBJECT_RE: Final = re.compile(r"^state:\s")
_TASK_TRAILER_RE: Final = re.compile(r"^Task:\s+(?P<key>\S+)\s*$", re.MULTILINE)
_BATCH_TRAILER_RE: Final = re.compile(r"^Batch:\s+(?P<key>\S+)\s*$", re.MULTILINE)

#: The binder: given the published branch and each staged Batch's URN mapped to
#: the commits carrying its work, binds the staged rows and answers how many.
Binder = Callable[[str, Mapping[str, tuple[PublishedCommit, ...]]], int]


class PublishRefusedError(ValueError):
    """The publish cannot run, or could not finish, and left the branch where it was.

    Attributes:
        code: One of the ``PUBLISH_*`` codes, or a staged-binding code.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Membership:
    """What the generation says about the commits being published.

    Attributes:
        task_batches: Each native Task key mapped to the key of its Batch.
        staged: The staged bindings waiting to be bound.
    """

    task_batches: Mapping[str, str]
    staged: tuple[StagedBinding, ...]


@dataclass(frozen=True, slots=True)
class ScratchCommit:
    """One commit between the published base and the tip.

    Attributes:
        sha: The commit.
        tree_sha: Its tree.
        message: Its full message.
    """

    sha: str
    tree_sha: str
    message: str

    @property
    def subject(self) -> str:
        """Return the first line of the message."""
        return self.message.split("\n", 1)[0].strip()

    @property
    def is_state(self) -> bool:
        """Return whether this is a ``state:`` commit."""
        return _STATE_SUBJECT_RE.match(self.subject) is not None

    @property
    def task_keys(self) -> tuple[str, ...]:
        """Return the keys the ``Task:`` trailers name, in order."""
        return tuple(match.group("key") for match in _TASK_TRAILER_RE.finditer(self.message))

    @property
    def batch_keys(self) -> tuple[str, ...]:
        """Return the keys the ``Batch:`` trailers name, in order."""
        return tuple(match.group("key") for match in _BATCH_TRAILER_RE.finditer(self.message))


@dataclass(frozen=True, slots=True)
class PublishPlan:
    """Which Batch each commit of the range carries.

    Attributes:
        base: The published commit the range starts after.
        tip: The branch tip.
        batches: Each Batch key mapped to its commits, oldest first, in the order
            the Batches first appear.
    """

    base: str
    tip: str
    batches: Mapping[str, tuple[ScratchCommit, ...]]


class PublishResult(BaseModel):
    """What one publish did.

    Attributes:
        branch: The branch published.
        old_tip: Its tip before the publish.
        new_tip: Its tip after: the old tip, or the trailing state commit.
        batch_commits: Each Batch key with the newest commit carrying its work.
        state_commit: The trailing ``state:`` commit written, or ``None``.
        bound: How many staged bindings were bound.
        noop: Whether nothing was staged, so the branch was left as it was.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch: str
    old_tip: str
    new_tip: str
    batch_commits: dict[str, str]
    state_commit: str | None
    bound: int
    noop: bool


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    """Run one git command in *repo* and return its stripped stdout.

    Raises:
        PublishRefusedError: The command failed.
    """
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise PublishRefusedError(
            PUBLISH_GIT_FAILED, f"git {args[0]} failed: {detail[-1] if detail else 'no output'}"
        )
    return done.stdout.strip()


def _maybe_git(repo: Path, *args: str) -> str | None:
    """Return the stripped stdout of one git query, or ``None`` when it fails."""
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    return done.stdout.strip() if done.returncode == 0 else None


def read_membership(tree_root: Path) -> Membership:
    """Read the Task placements and the staged bindings of the tree's generation.

    A Task that completed has left the document for its ledger, and its commits are
    still in the range a publish checks, so its placement is read from the ledger.
    A staged row whose Batch has closed needs no binding, so it is left out.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        Every native Task placed in a Batch, and every staged binding of a live Batch.

    Raises:
        NativeAuthorityRequiredError: The tree is not in epoch 2.
        ValidationError: A staged file is not a staged binding.
    """
    authority = require_native_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    live = read_document(document)
    filed = effective_records(read_ledger_records(ledger_path(document, Epoch2Collection.TASK)))
    rows = [item.payload for item in filed]
    rows.extend(document_rows(live, Epoch2Collection.TASK).values())
    placements: dict[str, str] = {}
    for row in rows:
        try:
            task = Task.model_validate(row)
        except ValidationError:
            # An imported epoch-1 row is not a native Task and sits in no Batch, and a
            # release line in the ledger is not a Task at all.
            continue
        if task.batch_ref is not None:
            placements[task.key] = task.batch_ref.entity_key
    batches = document_rows(live, Epoch2Collection.BATCH)
    staged = tuple(
        row
        for row in (
            StagedBinding.model_validate_json(path.read_bytes())
            for path in sorted(staged_dir(document).glob("*.json"))
        )
        if row.batch_ref.entity_key in batches
    )
    return Membership(task_batches=placements, staged=staged)


def _commit_batch(commit: ScratchCommit, task_batches: Mapping[str, str]) -> str | None:
    """Return the Batch *commit* carries, or ``None`` for a ``state:`` commit.

    Raises:
        PublishRefusedError: The commit names no Batch, a Task in no Batch,
            or more than one Batch.
    """
    if commit.is_state:
        return None
    named = set(commit.batch_keys)
    for task in commit.task_keys:
        if task not in task_batches:
            raise PublishRefusedError(
                PUBLISH_TASK_UNPLACED,
                f"{commit.sha[:12]} names Task {task}, which no native Batch holds",
            )
        named.add(task_batches[task])
    if not named:
        raise PublishRefusedError(
            PUBLISH_COMMIT_UNASSIGNED,
            f"{commit.sha[:12]} {commit.subject!r} names no Task or Batch; add a "
            "'Task: <KEY>' trailer or make it a 'state:' commit",
        )
    if len(named) > 1:
        raise PublishRefusedError(
            PUBLISH_COMMIT_MIXED,
            f"{commit.sha[:12]} carries the work of {len(named)} Batches: "
            f"{', '.join(sorted(named))}",
        )
    return named.pop()


def plan_publish(
    base: str, tip: str, commits: Sequence[ScratchCommit], task_batches: Mapping[str, str]
) -> PublishPlan:
    """File each commit of the range under the one Batch it carries.

    The commits of several Batches may interleave, because each Task's work lands
    when it is cherry-picked; a ``state:`` commit carries no Batch.

    Args:
        base: The published commit the range starts after.
        tip: The branch tip.
        commits: The range, oldest first.
        task_batches: Each Task key mapped to its Batch key.

    Returns:
        The plan.

    Raises:
        PublishRefusedError: A commit names no Task or Batch, a Task in no Batch, or
            more than one Batch.
    """
    batches: dict[str, list[ScratchCommit]] = {}
    for commit in commits:
        batch = _commit_batch(commit, task_batches)
        if batch is not None:
            batches.setdefault(batch, []).append(commit)
    return PublishPlan(
        base=base, tip=tip, batches={key: tuple(found) for key, found in batches.items()}
    )


def scratch_commits(repo: Path, base: str, tip: str) -> list[ScratchCommit]:
    """Return the commits of ``base..tip``, oldest first.

    Raises:
        PublishRefusedError: The range holds a merge commit.
    """
    merges = _git(repo, "rev-list", "--min-parents=2", f"{base}..{tip}")
    if merges:
        raise PublishRefusedError(
            PUBLISH_MERGE_COMMIT,
            f"{merges.splitlines()[0][:12]} is a merge; rebase the scratch work onto {base[:12]}",
        )
    commits: list[ScratchCommit] = []
    for sha in _git(repo, "rev-list", "--reverse", f"{base}..{tip}").splitlines():
        tree = _git(repo, "rev-parse", f"{sha}^{{tree}}")
        message = _git(repo, "show", "-s", "--format=%B", sha)
        commits.append(ScratchCommit(sha=sha, tree_sha=tree, message=message))
    return commits


def _candidates(plan: PublishPlan, batch_key: str) -> tuple[PublishedCommit, ...]:
    """Return the commits of the range carrying *batch_key*'s work."""
    return tuple(
        PublishedCommit(sha=commit.sha, tree_sha=commit.tree_sha)
        for commit in plan.batches.get(batch_key, ())
    )


def _check_bindable(plan: PublishPlan, staged: Sequence[StagedBinding]) -> None:
    """Refuse a staged binding no commit of its Batch would take.

    Raises:
        PublishRefusedError: A staged tree is carried by no commit of its Batch,
            or by several.
    """
    for row in staged:
        try:
            bind_staged(row, _candidates(plan, row.batch_ref.entity_key))
        except StagedBindingRefusedError as error:
            raise PublishRefusedError(
                error.code, f"{error}; adopt the Batch again at the commit that ends its work"
            ) from error


def _state_paths(repo: Path) -> list[str]:
    """Return the changed or new paths under ``.ea`` the commit policy commits."""
    listing = _git(repo, "status", "--porcelain", "--untracked-files=all", "--", ".ea")
    paths: list[str] = []
    for line in listing.splitlines():
        path = line[3:].split(" -> ")[-1]
        try:
            committed = classify_path(path).policy is CommitPolicy.COMMITTED
        except CommitPolicyError:
            committed = False
        if committed:
            paths.append(path)
    return paths


def publish(repo: Path, *, membership: Membership, bind: Binder) -> PublishResult:
    """Check the branch's commits past origin and bind the staged adoptions to them.

    Args:
        repo: The repository's working tree.
        membership: The generation's Task placements and staged bindings.
        bind: Binds the staged rows to the commits carrying each staged Batch
            and answers how many it bound; called only when a staged row is
            waiting, so a binder failure leaves the branch as it was with the
            rows still staged.

    Returns:
        What the publish did; ``noop`` when nothing was staged.

    Raises:
        PublishRefusedError: The tree is dirty, ``HEAD`` is detached, the
            remote default branch is unknown, the range holds a merge, a
            commit cannot be placed in one Batch, a staged row would stay
            unbound, or a git step failed. The branch is then as it was.
    """
    branch = _maybe_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        raise PublishRefusedError(PUBLISH_DETACHED_HEAD, "HEAD names no branch to publish")
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise PublishRefusedError(
            PUBLISH_DIRTY_TREE,
            f"{len(dirty.splitlines())} tracked paths differ from HEAD; commit or "
            "discard them first",
        )
    published = _maybe_git(repo, "rev-parse", "--verify", "--quiet", f"{PUBLISHED_REF}^{{commit}}")
    if not published:
        raise PublishRefusedError(
            PUBLISH_NO_REMOTE_DEFAULT,
            f"{PUBLISHED_REF} is not set, so nothing says which commits are scratch; "
            "run 'git remote set-head origin --auto'",
        )
    tip = _git(repo, "rev-parse", "HEAD")
    base = _git(repo, "merge-base", tip, published)
    plan = plan_publish(base, tip, scratch_commits(repo, base, tip), membership.task_batches)
    _check_bindable(plan, membership.staged)
    batch_commits = {key: commits[-1].sha for key, commits in plan.batches.items()}
    if not membership.staged:
        logger.info(f"publish branch={branch} noop=True batches={len(plan.batches)}")
        return PublishResult(
            branch=branch,
            old_tip=tip,
            new_tip=tip,
            batch_commits=batch_commits,
            state_commit=None,
            bound=0,
            noop=True,
        )
    targets = {
        str(row.batch_ref): _candidates(plan, row.batch_ref.entity_key) for row in membership.staged
    }
    bound = bind(branch, targets)
    paths = _state_paths(repo)
    if paths:
        _git(repo, "add", "--", *paths)
    state_commit = None
    if _maybe_git(repo, "diff", "--cached", "--quiet") is None:
        _git(
            repo,
            "commit",
            "--quiet",
            "-F",
            "-",
            stdin=f"state: bind {bound} published Batch commits\n",
        )
        state_commit = _git(repo, "rev-parse", "HEAD")
    logger.info(f"publish branch={branch} batches={len(plan.batches)} bound={bound}")
    return PublishResult(
        branch=branch,
        old_tip=tip,
        new_tip=state_commit or tip,
        batch_commits=batch_commits,
        state_commit=state_commit,
        bound=bound,
        noop=False,
    )


__all__ = [
    "PUBLISHED_REF",
    "PUBLISH_COMMIT_MIXED",
    "PUBLISH_COMMIT_UNASSIGNED",
    "PUBLISH_DETACHED_HEAD",
    "PUBLISH_DIRTY_TREE",
    "PUBLISH_GIT_FAILED",
    "PUBLISH_MERGE_COMMIT",
    "PUBLISH_NO_REMOTE_DEFAULT",
    "PUBLISH_TASK_UNPLACED",
    "Binder",
    "Membership",
    "PublishPlan",
    "PublishRefusedError",
    "PublishResult",
    "ScratchCommit",
    "plan_publish",
    "publish",
    "read_membership",
    "scratch_commits",
]
