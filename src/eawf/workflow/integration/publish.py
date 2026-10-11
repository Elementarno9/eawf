"""Squash the scratch commits a branch carries past its remote into one commit per Batch.

Scratch commits are free locally: any subject, any number per Task. Before a
push they are rewritten into the published shape: one ``<type>: <summary>``
commit per Batch carrying exactly one ``Batch:`` trailer, then one trailing
``state:`` commit. Each Batch commit is built by checking out the scratch
commit that ends the Batch's run, moving ``HEAD`` back with
``git reset --soft`` and running ``git commit``, so the repository's hooks
run on exactly the content being committed; a ``commit-tree`` squash would
skip them.

A Batch commit must carry the very tree its scratch run ended on, or the
staged bindings, which name that tree, would bind content nobody reviewed;
a hook that rewrites content therefore refuses the publish and puts the
branch back. The old tip is kept under ``refs/eawf/archive/publish/`` before
the branch moves. The staged bindings are bound by tree through a caller
supplied binder, which writes the ledger rows the trailing ``state:`` commit
carries. Nothing here pushes.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.store.commit_policy import CommitPolicy, CommitPolicyError, classify_path
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.paths import staged_dir
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.workflow.integration.publish_binding import (
    PublishedCommit,
    StagedBinding,
    StagedBindingRefusedError,
    bind_staged,
)

logger = logging.getLogger(__name__)

#: Where the tip a publish rewrote is kept, one ref per publish.
ARCHIVE_REF_PREFIX: Final = "refs/eawf/archive/publish/"

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

#: Refusal: a Batch's commits are split by another Batch's.
PUBLISH_BATCHES_INTERLEAVED: Final = "publish_batches_interleaved"

#: Refusal: a squashed commit's tree differs from the scratch tree it replaces.
PUBLISH_TREE_MISMATCH: Final = "publish_tree_mismatch"

#: Refusal: a git command the publish needs failed.
PUBLISH_GIT_FAILED: Final = "publish_git_failed"

_STATE_SUBJECT_RE: Final = re.compile(r"^state:\s")
_CONVENTIONAL_SUBJECT_RE: Final = re.compile(
    r"^(?:feat|fix|chore|docs|refactor|test|build|perf|ci|revert):\s+\S"
)
_TASK_TRAILER_RE: Final = re.compile(r"^Task:\s+(?P<key>\S+)\s*$", re.MULTILINE)
_BATCH_TRAILER_RE: Final = re.compile(r"^Batch:\s+(?P<key>\S+)\s*$", re.MULTILINE)

#: Scratch subjects listed in a Batch commit body before the rest are counted.
_BODY_SUBJECTS: Final = 8

#: The binder: given the published branch and each staged Batch's URN mapped to
#: the commits now carrying it, binds the staged rows and answers how many.
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
        parent_sha: Its only parent.
        message: Its full message.
    """

    sha: str
    tree_sha: str
    parent_sha: str
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


@dataclass(slots=True)
class BatchRun:
    """The scratch commits of one Batch, squashed into one commit.

    Attributes:
        batch_key: The Batch.
        commits: Its commits, oldest first, with the ``state:`` commits
            that fell between them.
        task_keys: The Tasks its commits name, in first-seen order.
    """

    batch_key: str
    commits: list[ScratchCommit] = field(default_factory=list)
    task_keys: list[str] = field(default_factory=list)

    @property
    def end(self) -> ScratchCommit:
        """Return the commit whose tree the squashed commit carries."""
        return self.commits[-1]

    def published(self) -> bool:
        """Return whether the run is already one commit in the published shape."""
        only = self.commits[0]
        return len(self.commits) == 1 and only.batch_keys == (self.batch_key,)


@dataclass(frozen=True, slots=True)
class PublishPlan:
    """How the scratch range is squashed.

    Attributes:
        base: The published commit the range starts after.
        tip: The scratch tip.
        runs: One run per Batch, in commit order.
        trailing_state: The ``state:`` commits after the last Batch commit.
    """

    base: str
    tip: str
    runs: tuple[BatchRun, ...]
    trailing_state: tuple[ScratchCommit, ...]


class PublishResult(BaseModel):
    """What one publish did.

    Attributes:
        branch: The branch published.
        old_tip: Its tip before the publish.
        new_tip: Its tip after.
        archive_ref: The ref keeping the old tip, or ``None`` when no
            commit was rewritten.
        batch_commits: Each Batch key with the commit that carries it.
        state_commit: The trailing ``state:`` commit written, or ``None``.
        bound: How many staged bindings were bound.
        noop: Whether the branch was already in the published shape.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch: str
    old_tip: str
    new_tip: str
    archive_ref: str | None
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

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        Every native Task placed in a Batch, and every staged binding.

    Raises:
        NativeAuthorityRequiredError: The tree is not in epoch 2.
        ValidationError: A staged file is not a staged binding.
    """
    authority = require_native_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    placements: dict[str, str] = {}
    for key, row in document_rows(read_document(document), Epoch2Collection.TASK).items():
        try:
            task = Task.model_validate(row)
        except ValidationError:
            # An imported epoch-1 row is not a native Task and sits in no Batch.
            continue
        if task.batch_ref is not None:
            placements[key] = task.batch_ref.entity_key
    staged = tuple(
        StagedBinding.model_validate_json(path.read_bytes())
        for path in sorted(staged_dir(document).glob("*.json"))
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
    """Group the scratch commits into one run per Batch.

    A ``state:`` commit joins the run of the next Batch commit, so its
    content lands in the commit that follows it; the ``state:`` commits
    after the last Batch commit are left for the trailing state commit.

    Args:
        base: The published commit the range starts after.
        tip: The scratch tip.
        commits: The range, oldest first.
        task_batches: Each Task key mapped to its Batch key.

    Returns:
        The plan.

    Raises:
        PublishRefusedError: A commit cannot be placed in one Batch, or a
            Batch's commits are split by another Batch's.
    """
    runs: list[BatchRun] = []
    pending: list[ScratchCommit] = []
    for commit in commits:
        batch = _commit_batch(commit, task_batches)
        if batch is None:
            pending.append(commit)
            continue
        if not runs or runs[-1].batch_key != batch:
            earlier = next((run for run in runs if run.batch_key == batch), None)
            if earlier is not None:
                raise PublishRefusedError(
                    PUBLISH_BATCHES_INTERLEAVED,
                    f"{commit.sha[:12]} returns to Batch {batch} after Batch "
                    f"{runs[-1].batch_key}; reorder the scratch commits so each Batch's "
                    "work is contiguous",
                )
            runs.append(BatchRun(batch_key=batch))
        run = runs[-1]
        run.commits.extend([*pending, commit])
        pending.clear()
        run.task_keys.extend(key for key in commit.task_keys if key not in run.task_keys)
    return PublishPlan(base=base, tip=tip, runs=tuple(runs), trailing_state=tuple(pending))


def batch_message(run: BatchRun) -> str:
    """Return the message of the commit one Batch run is squashed into.

    The subject is the run's first conventional non-state subject, so the
    author's own wording survives; the other subjects are listed in the
    body. The ``Batch:`` trailer comes first, then one ``Task:`` per Task.
    """
    work = [commit for commit in run.commits if not commit.is_state]
    subject = next(
        (c.subject for c in work if _CONVENTIONAL_SUBJECT_RE.match(c.subject)),
        f"feat: deliver Batch {run.batch_key}",
    )
    others = [c.subject for c in work if c.subject != subject]
    lines = [subject, ""]
    if others:
        lines.extend(f"- {item}" for item in others[:_BODY_SUBJECTS])
        if len(others) > _BODY_SUBJECTS:
            lines.append(f"- and {len(others) - _BODY_SUBJECTS} more scratch commits")
        lines.append("")
    lines.append(f"Batch: {run.batch_key}")
    lines.extend(f"Task: {key}" for key in run.task_keys)
    return "\n".join(lines) + "\n"


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
        tree, parent = _git(repo, "show", "-s", "--format=%T %P", sha).split()
        message = _git(repo, "show", "-s", "--format=%B", sha)
        commits.append(ScratchCommit(sha=sha, tree_sha=tree, parent_sha=parent, message=message))
    return commits


def _check_bindable(plan: PublishPlan, staged: Sequence[StagedBinding]) -> None:
    """Refuse a staged binding the squashed runs would leave unbound.

    Each squashed commit carries its run's end tree, so binding against the
    run ends now proves the binding will hold once the commits exist.

    Raises:
        PublishRefusedError: A staged tree is carried by no run end of its
            Batch, or by several.
    """
    for row in staged:
        candidates = [
            PublishedCommit(sha=run.end.sha, tree_sha=run.end.tree_sha)
            for run in plan.runs
            if run.batch_key == row.batch_ref.entity_key
        ]
        try:
            bind_staged(row, candidates)
        except StagedBindingRefusedError as error:
            raise PublishRefusedError(
                error.code,
                f"{error}; adopt the Batch again at the scratch commit that ends its work",
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


def _archive_ref(repo: Path, tip: str, now: datetime) -> str:
    """Keep *tip* under a new archive ref named for *now* and return the ref."""
    ref = f"{ARCHIVE_REF_PREFIX}{now.strftime('%Y%m%dT%H%M%S%fZ')}"
    # The empty old value makes the update create-only, so an archive is never overwritten.
    _git(repo, "update-ref", ref, tip, "")
    return ref


def publish(repo: Path, *, membership: Membership, bind: Binder, now: datetime) -> PublishResult:
    """Squash the branch's scratch commits into one commit per Batch.

    Args:
        repo: The repository's working tree.
        membership: The generation's Task placements and staged bindings.
        bind: Binds the staged rows to the commits that now carry each
            staged Batch and answers how many it bound; called only when a
            staged row is waiting, after the branch has moved, so a binder
            failure leaves the published branch and the archive ref in
            place with the rows still staged.
        now: When the publish runs, naming the archive ref.

    Returns:
        What the publish did; ``noop`` when the branch was already in the
        published shape and nothing was staged.

    Raises:
        PublishRefusedError: The tree is dirty, ``HEAD`` is detached, the
            remote default branch is unknown, a commit cannot be placed, a
            staged row would stay unbound, a git step failed, or a hook
            changed a squashed tree. The branch, the archive refs and the
            state are then as they were.
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
    try:
        head, rewritten, carried = _squash(repo, plan, staged=bool(membership.staged))
    except PublishRefusedError:
        _git(repo, "checkout", "--quiet", "--force", branch)
        raise
    if not rewritten and not membership.staged:
        logger.info(f"publish branch={branch} noop=True runs={len(plan.runs)}")
        return PublishResult(
            branch=branch,
            old_tip=tip,
            new_tip=tip,
            archive_ref=None,
            batch_commits={run.batch_key: run.end.sha for run in plan.runs},
            state_commit=None,
            bound=0,
            noop=True,
        )
    archive = _archive_ref(repo, tip, now) if rewritten else None
    # HEAD already sits on the new head with the trailing state staged, so
    # the branch is moved and re-attached without touching index or tree.
    _git(repo, "update-ref", f"refs/heads/{branch}", head, tip)
    _git(repo, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    targets = {str(row.batch_ref): carried[row.batch_ref.entity_key] for row in membership.staged}
    bound = bind(branch, targets) if targets else 0
    paths = _state_paths(repo)
    if paths:
        _git(repo, "add", "--", *paths)
    state_commit = None
    if _maybe_git(repo, "diff", "--cached", "--quiet") is None:
        subject = (
            f"state: bind {bound} published Batch commits"
            if bound
            else "state: carry the scratch state into the publish"
        )
        _git(repo, "commit", "--quiet", "-F", "-", stdin=f"{subject}\n")
        state_commit = _git(repo, "rev-parse", "HEAD")
    new_tip = state_commit or head
    logger.info(
        f"publish branch={branch} runs={len(plan.runs)} rewritten={rewritten} bound={bound}"
    )
    return PublishResult(
        branch=branch,
        old_tip=tip,
        new_tip=new_tip,
        archive_ref=archive,
        batch_commits={batch: commits[0].sha for batch, commits in carried.items()},
        state_commit=state_commit,
        bound=bound,
        noop=False,
    )


def _squash(
    repo: Path, plan: PublishPlan, *, staged: bool
) -> tuple[str, bool, dict[str, tuple[PublishedCommit, ...]]]:
    """Build the published commits on a detached ``HEAD``.

    A run already in the published shape on an unchanged parent is kept as
    it is, so a publish of a published branch rewrites nothing.

    Args:
        repo: The repository's working tree.
        plan: The runs to squash.
        staged: Whether staged rows will be bound, so the trailing state
            is rebuilt to carry them.

    Returns:
        The new head, whether any commit was rewritten, and each Batch
        mapped to the commit that carries it. When the trailing state is
        rebuilt it is left staged on top of the new head.

    Raises:
        PublishRefusedError: A git step failed, or a hook changed the tree
            a squashed commit carries.
    """
    parent = plan.base
    rewritten = False
    carried: dict[str, tuple[PublishedCommit, ...]] = {}
    for run in plan.runs:
        if not rewritten and run.published() and run.end.parent_sha == parent:
            parent = run.end.sha
            carried[run.batch_key] = (PublishedCommit(sha=parent, tree_sha=run.end.tree_sha),)
            continue
        rewritten = True
        _git(repo, "checkout", "--quiet", "--detach", run.end.sha)
        _git(repo, "reset", "--quiet", "--soft", parent)
        _git(repo, "commit", "--quiet", "-F", "-", stdin=batch_message(run))
        built = _git(repo, "rev-parse", "HEAD")
        tree = _git(repo, "rev-parse", f"{built}^{{tree}}")
        if tree != run.end.tree_sha:
            raise PublishRefusedError(
                PUBLISH_TREE_MISMATCH,
                f"the commit for Batch {run.batch_key} carries tree {tree}, not the scratch "
                f"tree {run.end.tree_sha}; a hook changed the content, so fix the scratch "
                "commits and publish again",
            )
        parent = built
        carried[run.batch_key] = (PublishedCommit(sha=built, tree_sha=tree),)
    trailing = plan.trailing_state
    if trailing and (rewritten or staged or len(trailing) > 1):
        rewritten = True
        _git(repo, "checkout", "--quiet", "--detach", plan.tip)
        _git(repo, "reset", "--quiet", "--soft", parent)
    elif trailing:
        parent = trailing[0].sha
    return parent, rewritten, carried


__all__ = [
    "ARCHIVE_REF_PREFIX",
    "PUBLISHED_REF",
    "PUBLISH_BATCHES_INTERLEAVED",
    "PUBLISH_COMMIT_MIXED",
    "PUBLISH_COMMIT_UNASSIGNED",
    "PUBLISH_DETACHED_HEAD",
    "PUBLISH_DIRTY_TREE",
    "PUBLISH_GIT_FAILED",
    "PUBLISH_MERGE_COMMIT",
    "PUBLISH_NO_REMOTE_DEFAULT",
    "PUBLISH_TASK_UNPLACED",
    "PUBLISH_TREE_MISMATCH",
    "BatchRun",
    "Binder",
    "Membership",
    "PublishPlan",
    "PublishRefusedError",
    "PublishResult",
    "ScratchCommit",
    "batch_message",
    "plan_publish",
    "publish",
    "read_membership",
    "scratch_commits",
]
