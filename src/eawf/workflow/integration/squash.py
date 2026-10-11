"""Squash a branch's scratch commits into one commit per Batch on a scratch branch.

A branch carries one or more commits per Task, interleaved in the order the Tasks'
work was cherry-picked, with ``state:`` commits between them. Before a push it is
rewritten to one commit per Batch, each naming its Batch and every Task it delivers
by trailer, and one trailing ``state:`` commit. The Batch commits are ordered by the
last source commit of each Batch.

The final tree is fixed: it is the source tip's. Each Batch commit's tree is the
previous rewritten tree with that Batch's source commits replayed onto it by a
three-way tree merge, and the trailing ``state:`` commit takes the source tip's tree
outright, so the rewrite ends byte-identical to the source. A replay conflicts only
when a Batch's commit rests on a hunk an earlier commit of a Batch ordered after it
wrote; those two Batches are then merged into one commit and the merge is reported.
A commit naming no placed Task or Batch is folded into the Batch commit before it
and reported.

The rewrite writes commit objects and, unless only planned, one new branch ref; it
never moves an existing ref, so the source branch is untouched. It answers the map
of every source commit to the rewritten commit carrying it, which the daemon's
Batch rebind takes to supersede the ledger rows that name a source commit.
"""

from __future__ import annotations

import functools
import logging
import os
import shlex
import subprocess
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.models import ShaStr
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.workflow.integration.publish import (
    PUBLISH_GIT_FAILED,
    PublishRefusedError,
    ScratchCommit,
    scratch_commits,
)

logger = logging.getLogger(__name__)

#: Refusal: the branch the rewrite would create already exists.
SQUASH_TARGET_EXISTS: Final = "squash_target_exists"

#: Refusal: the range holds no commit to squash.
SQUASH_EMPTY_RANGE: Final = "squash_empty_range"

#: Refusal: no Batch commit precedes or follows a commit that names no Batch.
SQUASH_UNPLACEABLE: Final = "squash_unplaceable"

#: Refusal: a replay conflicts with a ``state:`` commit, which no Batch can absorb.
SQUASH_STATE_CONFLICT: Final = "squash_state_conflict"

#: What a replay conflict does: merge the two Batches into one commit, or carry the
#: conflicted paths forward at their source content and keep the Batches apart.
ConflictStrategy = Literal["merge", "carry"]

#: How many source subjects a squashed commit's body lists before it counts the rest.
_BODY_SUBJECTS: Final = 8

#: The trailers a squashed commit replaces with its own Batch and Task trailers.
_REPLACED_TRAILERS: Final = ("task:", "batch:", "co-authored-by:")

#: The directory whose changes in a state commit only the trailing state commit carries.
_STATE_DIR: Final = ".ea"

#: The conventional types, most significant first, a squashed subject is typed by.
_TYPE_RANK: Final = ("feat", "fix", "perf", "refactor", "test", "docs", "ci", "build", "chore")


class SquashMap(BaseModel):
    """Which rewritten commit carries each source commit of one squash.

    Attributes:
        branch: The branch the squashed history is to replace.
        base: The published commit both histories start after.
        source_head: The tip that was squashed.
        head: The squashed tip.
        commits: Each source commit mapped to the rewritten commit carrying it,
            or to ``None`` when no commit stands for it and a reference to it is
            dropped.
        dropped: Why each commit mapped to ``None`` is dropped.
        batches: Each Batch key mapped to the last rewritten commit carrying it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch: str = Field(min_length=1)
    base: ShaStr
    source_head: ShaStr
    head: ShaStr
    commits: dict[ShaStr, ShaStr | None]
    dropped: dict[ShaStr, Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=dict
    )
    batches: dict[str, ShaStr]

    @model_validator(mode="after")
    def _each_drop_has_a_reason(self) -> Self:
        """Refuse a dropped commit without a reason, or a reason for a kept one.

        Raises:
            ValueError: The commits mapped to ``None`` are not exactly those with
                a reason.
        """
        if {sha for sha, new in self.commits.items() if new is None} != set(self.dropped):
            raise ValueError("every commit mapped to null needs a dropped reason, and only those")
        return self


@dataclass(frozen=True, slots=True)
class SquashGroup:
    """The source commits one rewritten Batch commit carries.

    Attributes:
        batch_keys: The Batches it carries; more than one only when their work
            could not be separated.
        commits: Its source commits, oldest first.
        folded: The commits among them that named no Batch.
    """

    batch_keys: tuple[str, ...]
    commits: tuple[ScratchCommit, ...]
    folded: tuple[ScratchCommit, ...] = ()


@dataclass(frozen=True, slots=True)
class SquashPlan:
    """The Batch commits a squash writes, in order, and the state commits it folds.

    Attributes:
        base: The commit the range starts after.
        tip: The source tip.
        groups: The Batch commits to write, ordered by each one's last source commit.
        state_commits: The ``state:`` commits the trailing state commit replaces.
        order: Each source commit's position in the range, oldest first.
    """

    base: str
    tip: str
    groups: tuple[SquashGroup, ...]
    state_commits: tuple[ScratchCommit, ...]
    order: Mapping[str, int]


class SquashedCommit(BaseModel):
    """One commit a squash wrote.

    Attributes:
        sha: The rewritten commit.
        subject: Its subject.
        batch_keys: The Batches its trailers name; empty for the state commit.
        task_keys: The Tasks its trailers name.
        source_commits: How many source commits it carries.
        folded: The source commits it absorbed that named no Batch, as
            ``<short sha> <subject>``.
        carried: The paths it carries at source content because its Batch's
            change to them did not replay apart from a later Batch's.
        check_failures: Each check command that failed on its tree, with why.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: ShaStr
    subject: str
    batch_keys: tuple[str, ...]
    task_keys: tuple[str, ...]
    source_commits: int
    folded: tuple[str, ...] = ()
    carried: tuple[str, ...] = ()
    check_failures: tuple[str, ...] = ()


class SquashResult(BaseModel):
    """What one squash wrote, and whether it reproduces the source tree.

    Attributes:
        into: The branch created, or ``None`` when the rewrite was only planned.
        commits: The rewritten commits, oldest first, the state commit last.
        merged: Each set of Batches whose work had to share one commit.
        tree_equal: Whether the squashed tip's tree is the source tip's.
        map: The source-to-rewritten commit map.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    into: str | None
    commits: tuple[SquashedCommit, ...]
    merged: tuple[tuple[str, ...], ...]
    tree_equal: bool
    map: SquashMap


def _git(
    repo: Path, *args: str, env: Mapping[str, str] | None = None, stdin: str | None = None
) -> str:
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
        env=None if env is None else {**os.environ, **env},
        **no_window_kwargs(),
    )
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise PublishRefusedError(
            PUBLISH_GIT_FAILED, f"git {args[0]} failed: {detail[-1] if detail else 'no output'}"
        )
    return done.stdout.strip()


def _commit_batches(commit: ScratchCommit, task_batches: Mapping[str, str]) -> set[str]:
    """Return the Batches *commit* names by Batch trailer or through a placed Task."""
    named = set(commit.batch_keys)
    named.update(task_batches[task] for task in commit.task_keys if task in task_batches)
    return named


def _join_batches(
    commits: Sequence[ScratchCommit], task_batches: Mapping[str, str]
) -> tuple[list[list[str] | None], dict[str, frozenset[str]]]:
    """Return each commit's Batches (``None`` for a state commit) and the joined sets.

    A commit naming two Batches joins them for good, so the second mapping sends
    every Batch key to the full set of Batches it was joined with.
    """
    joined: dict[str, set[str]] = {}
    named: list[list[str] | None] = []
    for commit in commits:
        keys = None if commit.is_state else sorted(_commit_batches(commit, task_batches))
        named.append(keys)
        merged = set(keys or [])
        for key in keys or []:
            merged |= joined.get(key, set())
        for key in merged:
            joined[key] = merged
    return named, {key: frozenset(members) for key, members in joined.items()}


def _fold_target(named: Sequence[list[str] | None], index: int) -> str:
    """Return the Batch a commit naming none is folded into: the nearest before, else after."""
    before = [keys[0] for keys in named[:index] if keys]
    if before:
        return before[-1]
    return next(keys[0] for keys in named[index:] if keys)


def plan_squash(
    base: str,
    tip: str,
    commits: Sequence[ScratchCommit],
    task_batches: Mapping[str, str],
    *,
    straddling: frozenset[str] = frozenset(),
) -> SquashPlan:
    """Group the range's commits into one Batch commit each, plus the state commits.

    A commit naming several Batches joins them into one group. A commit naming no
    Batch is folded into the group of the Batch commit before it, or after it when
    none precedes it; so is the part outside ``.ea/`` of a state commit that
    straddles both.

    Args:
        base: The commit the range starts after.
        tip: The source tip.
        commits: The range, oldest first.
        task_batches: Each Task key mapped to its Batch key.
        straddling: The state commits that also change paths outside ``.ea/``.

    Returns:
        The plan, its groups ordered by each one's last source commit.

    Raises:
        PublishRefusedError: The range is empty, or holds a commit naming no Batch
            and no Batch commit to fold it into.
    """
    if not commits:
        raise PublishRefusedError(SQUASH_EMPTY_RANGE, f"{base[:12]}..{tip[:12]} holds no commit")
    named, joined = _join_batches(commits, task_batches)
    folds = [
        index
        for index, (commit, keys) in enumerate(zip(commits, named, strict=True))
        if keys == [] or (keys is None and commit.sha in straddling)
    ]
    if folds and not joined:
        stray = commits[folds[0]]
        raise PublishRefusedError(
            SQUASH_UNPLACEABLE,
            f"{stray.sha[:12]} {stray.subject!r} names no Batch and no Batch commit "
            "carries the range",
        )
    members: dict[frozenset[str], list[int]] = {}
    for index, keys in enumerate(named):
        owner = _fold_target(named, index) if index in folds else (keys[0] if keys else None)
        if owner is not None:
            members.setdefault(joined[owner], []).append(index)
    groups = tuple(
        SquashGroup(
            batch_keys=tuple(sorted(batches)),
            commits=tuple(commits[index] for index in indexes),
            folded=tuple(commits[index] for index in indexes if index in folds),
        )
        for batches, indexes in sorted(members.items(), key=lambda item: item[1][-1])
    )
    return SquashPlan(
        base=base,
        tip=tip,
        groups=groups,
        state_commits=tuple(c for c, keys in zip(commits, named, strict=True) if keys is None),
        order={commit.sha: index for index, commit in enumerate(commits)},
    )


def _merge_groups(plan: SquashPlan, keep: int, absorb: int) -> SquashPlan:
    """Return *plan* with group *absorb* merged into group *keep*, regrouped in order."""
    first, second = plan.groups[keep], plan.groups[absorb]
    joined = SquashGroup(
        batch_keys=tuple(sorted({*first.batch_keys, *second.batch_keys})),
        commits=tuple(
            sorted((*first.commits, *second.commits), key=lambda commit: plan.order[commit.sha])
        ),
        folded=(*first.folded, *second.folded),
    )
    rest = [group for index, group in enumerate(plan.groups) if index not in (keep, absorb)]
    rest.append(joined)
    rest.sort(key=lambda group: plan.order[group.commits[-1].sha])
    return SquashPlan(
        base=plan.base,
        tip=plan.tip,
        groups=tuple(rest),
        state_commits=plan.state_commits,
        order=plan.order,
    )


def _outside_state_tree(repo: Path, commit: ScratchCommit) -> str:
    """Return *commit*'s tree with its ``.ea`` entry put back to its parent's.

    Replayed in place of the commit, it carries only the change a state commit
    makes outside ``.ea/``; the trailing state commit carries the rest.
    """
    entries: list[str] = []
    parent = {
        line.split("\t", 1)[1]: line
        for line in _git(repo, "ls-tree", f"{commit.sha}^").splitlines()
    }
    for line in _git(repo, "ls-tree", commit.sha).splitlines():
        name = line.split("\t", 1)[1]
        if name != _STATE_DIR:
            entries.append(line)
    if _STATE_DIR in parent:
        entries.append(parent[_STATE_DIR])
    return _git(repo, "mktree", stdin="\n".join(entries) + "\n")


def _replay(repo: Path, tree: str, commit: ScratchCommit) -> tuple[str, list[str]]:
    """Replay *commit*'s change onto *tree*; return the tree and any conflicted paths."""
    theirs = _outside_state_tree(repo, commit) if commit.is_state else commit.sha
    done = subprocess.run(
        [
            "git", "-C", str(repo), "merge-tree", "--write-tree", "--name-only",
            "--no-messages", f"--merge-base={commit.sha}^", tree, theirs,
        ],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )  # fmt: skip
    lines = done.stdout.strip().splitlines()
    if done.returncode == 0:
        return lines[0], []
    if done.returncode == 1 and lines:
        return lines[0], lines[1:]
    raise PublishRefusedError(
        PUBLISH_GIT_FAILED, f"git merge-tree failed on {commit.sha[:12]}: {done.stderr.strip()}"
    )


def _changed_paths(repo: Path, commit: ScratchCommit) -> frozenset[str]:
    """Return the paths *commit* changes against its parent."""
    return _paths_of(repo, commit.sha)


@functools.cache
def _paths_of(repo: Path, sha: str) -> frozenset[str]:
    # A commit never changes, so its path list is read once per process.
    listing = _git(repo, "diff-tree", "-r", "--root", "--name-only", "--no-commit-id", sha)
    return frozenset(listing.splitlines())


def _blocking_group(
    repo: Path, plan: SquashPlan, at: int, commit: ScratchCommit, conflicted: list[str]
) -> int:
    """Return the later group whose earlier source commit wrote what *commit* rests on.

    Raises:
        PublishRefusedError: The conflict is with a ``state:`` commit, or no later
            group's commit touches the conflicted paths.
    """
    paths = set(conflicted)
    everything = [c for group in plan.groups for c in group.commits]
    everything.extend(c for c in plan.state_commits if c not in everything)
    earlier_commits = sorted(
        (c for c in everything if plan.order[c.sha] < plan.order[commit.sha]),
        key=lambda c: plan.order[c.sha],
    )
    for earlier in reversed(earlier_commits):
        if not paths & _changed_paths(repo, earlier):
            continue
        if not any(earlier in group.commits for group in plan.groups):
            raise PublishRefusedError(
                SQUASH_STATE_CONFLICT,
                f"{commit.sha[:12]} rests on state commit {earlier.sha[:12]} for "
                f"{', '.join(sorted(paths))}",
            )
        for index in range(at + 1, len(plan.groups)):
            if any(c.sha == earlier.sha for c in plan.groups[index].commits):
                return index
    raise PublishRefusedError(
        PUBLISH_GIT_FAILED,
        f"{commit.sha[:12]} conflicts on {', '.join(sorted(paths))} with no later Batch",
    )


def _build_trees(repo: Path, plan: SquashPlan) -> tuple[SquashPlan, list[str]]:
    """Replay each group onto the previous one's tree, merging groups until none conflicts.

    Returns:
        The plan as finally grouped, and each group's tree in order.
    """
    base_tree = _git(repo, "rev-parse", f"{plan.base}^{{tree}}")
    while True:
        tree = base_tree
        trees: list[str] = []
        blocked: tuple[int, int] | None = None
        for index, group in enumerate(plan.groups):
            for commit in group.commits:
                tree, conflicted = _replay(repo, tree, commit)
                if conflicted:
                    blocked = (index, _blocking_group(repo, plan, index, commit, conflicted))
                    break
            if blocked is not None:
                break
            trees.append(tree)
        if blocked is None:
            return plan, trees
        logger.info(f"_build_trees keep={blocked[1]} absorb={blocked[0]}")
        plan = _merge_groups(plan, blocked[1], blocked[0])


def _with_paths(repo: Path, tree: str, source: str, paths: Sequence[str]) -> str:
    """Return *tree* with each of *paths* as *source* holds it, absent where it holds none."""
    with tempfile.TemporaryDirectory(prefix="eawf-squash-index-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(repo, "read-tree", tree, env=env)
        listing = _git(repo, "ls-tree", "-r", "-z", source, "--", *paths)
        held = {}
        for entry in filter(None, listing.split("\0")):
            meta, path = entry.split("\t", 1)
            mode, _, blob = meta.split()
            held[path] = f"{mode},{blob},{path}"
        for path in paths:
            if path in held:
                _git(repo, "update-index", "--add", "--cacheinfo", held[path], env=env)
            else:
                _git(repo, "update-index", "--force-remove", "--", path, env=env)
        return _git(repo, "write-tree", env=env)


def _carry_pass(
    repo: Path, plan: SquashPlan, pins: Mapping[int, frozenset[str]]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Build each group's tree, carrying a path that will not replay at its source version.

    A conflicted path takes the content the source history held after the
    conflicting commit, or after a later one already carried, so a path only
    moves forward; the commits of later Batches that wrote it then replay as
    no-ops. A path pinned to a group takes the source tip's content once that
    group has replayed.

    Returns:
        Each group's tree, and every ``(source commit, path)`` carried.
    """
    shas = sorted(plan.order, key=plan.order.__getitem__)
    tree = _git(repo, "rev-parse", f"{plan.base}^{{tree}}")
    taken: dict[str, int] = {}
    trees: list[str] = []
    carried: list[tuple[str, str]] = []
    for index, group in enumerate(plan.groups):
        for commit in group.commits:
            tree, conflicted = _replay(repo, tree, commit)
            if not conflicted:
                continue
            at = plan.order[commit.sha]
            by_source: dict[int, list[str]] = {}
            for path in conflicted:
                source = max(at, taken.get(path, at))
                taken[path] = source
                by_source.setdefault(source, []).append(path)
                carried.append((commit.sha, path))
            for source, paths in sorted(by_source.items()):
                tree = _with_paths(repo, tree, shas[source], paths)
        pinned = sorted(pins.get(index, frozenset()))
        if pinned:
            tree = _with_paths(repo, tree, plan.tip, pinned)
            taken.update(dict.fromkeys(pinned, len(shas) - 1))
        trees.append(tree)
    return trees, carried


def _carry_trees(repo: Path, plan: SquashPlan) -> tuple[list[str], list[tuple[str, str]]]:
    """Build every group's tree by carrying, pinning residue until the last tree matches.

    A path whose content after the last group still differs from the tip outside
    what the state commits wrote is pinned to the group of the last source
    commit that wrote it, and the trees are built again.
    """
    tip_tree = _git(repo, "rev-parse", f"{plan.tip}^{{tree}}")
    pins: dict[int, frozenset[str]] = {}
    while True:
        trees, carried = _carry_pass(repo, plan, pins)
        residue = _residual_strays(repo, trees[-1], tip_tree, plan) if trees else []
        if not residue:
            return trees, carried
        added = False
        for path in residue:
            owner = _last_writer_group(repo, plan, path)
            if path not in pins.get(owner, frozenset()):
                pins[owner] = pins.get(owner, frozenset()) | {path}
                added = True
        if not added:
            return trees, carried


def _last_writer_group(repo: Path, plan: SquashPlan, path: str) -> int:
    """Return the group holding the last source commit that wrote *path*, else the last."""
    best = (-1, len(plan.groups) - 1)
    for index, group in enumerate(plan.groups):
        for commit in group.commits:
            if path in _changed_paths(repo, commit) and plan.order[commit.sha] > best[0]:
                best = (plan.order[commit.sha], index)
    return best[1]


def _commit_type(subject: str) -> str:
    """Return the conventional type of *subject*, or ``chore`` when it has none."""
    head, _, _ = subject.partition(":")
    return head if head in _TYPE_RANK else "chore"


def _group_tasks(group: SquashGroup, task_batches: Mapping[str, str]) -> tuple[str, ...]:
    """Return the Tasks of *group*'s Batches its commits name, in first-named order."""
    keys = set(group.batch_keys)
    seen: dict[str, None] = {}
    for commit in group.commits:
        for task in commit.task_keys:
            if task_batches.get(task) in keys:
                seen.setdefault(task, None)
    return tuple(seen)


def _strip_trailers(message: str) -> str:
    """Return *message* without its Task, Batch and co-author trailer lines."""
    kept = [
        line
        for line in message.rstrip().splitlines()
        if not line.strip().lower().startswith(_REPLACED_TRAILERS)
    ]
    return "\n".join(kept).rstrip()


def group_message(group: SquashGroup, tasks: Sequence[str]) -> str:
    """Return the message of *group*'s rewritten commit.

    A group of one commit keeps that commit's message. A larger group takes the
    subject of its first commit of the most significant type and lists every
    source subject in its body. Either way the trailers are replaced by one
    ``Batch:`` line per Batch and one ``Task:`` line per Task.

    Args:
        group: The group.
        tasks: The Tasks its trailers name.

    Returns:
        The message, ending in a newline.
    """
    trailers = [f"Batch: {key}" for key in group.batch_keys]
    trailers.extend(f"Task: {key}" for key in tasks)
    if len(group.commits) == 1 and not group.commits[0].is_state:
        body = _strip_trailers(group.commits[0].message)
        return f"{body}\n\n" + "\n".join(trailers) + "\n"
    work = [commit for commit in group.commits if not commit.is_state] or list(group.commits)
    types = Counter(_commit_type(commit.subject) for commit in work)
    kind = min(types, key=_TYPE_RANK.index)
    lead = next(c for c in work if _commit_type(c.subject) == kind)
    subjects = list(dict.fromkeys(commit.subject for commit in work))
    lines = [f"- {subject}" for subject in subjects[:_BODY_SUBJECTS]]
    if len(subjects) > _BODY_SUBJECTS:
        lines.append(f"- and {len(subjects) - _BODY_SUBJECTS} more")
    return f"{lead.subject}\n\n" + "\n".join(lines) + "\n\n" + "\n".join(trailers) + "\n"


def _identity(repo: Path, sha: str) -> dict[str, str]:
    """Return the author and committer environment that reproduces *sha*'s identity."""
    fields = _git(repo, "show", "-s", "--format=%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd",
                  "--date=raw", sha).split("\0")  # fmt: skip
    names = (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    )  # fmt: skip
    return dict(zip(names, fields, strict=True))


def _commit_tree(repo: Path, tree: str, parent: str, message: str, like: str) -> str:
    """Write one commit of *tree* on *parent* with *like*'s identity and return it."""
    with tempfile.NamedTemporaryFile("w", suffix=".msg", delete=False, encoding="utf-8") as fh:
        fh.write(message)
    try:
        return _git(
            repo, "commit-tree", tree, "-p", parent, "-F", fh.name, env=_identity(repo, like)
        )
    finally:
        Path(fh.name).unlink(missing_ok=True)


def _residual_strays(repo: Path, tree: str, tip_tree: str, plan: SquashPlan) -> list[str]:
    """Return the paths the trailing state commit would carry that no state commit wrote."""
    written: set[str] = set()
    for commit in plan.state_commits:
        written |= {path for path in _changed_paths(repo, commit) if path.startswith(".ea/")}
    listing = _git(repo, "diff-tree", "-r", "--name-only", "--no-commit-id", tree, tip_tree)
    return [path for path in listing.splitlines() if path not in written]


def _check_commit(repo: Path, sha: str, commands: Sequence[str]) -> tuple[str, ...]:
    """Run each check command in a fresh export of *sha*'s tree.

    Args:
        repo: The repository.
        sha: The commit to check.
        commands: Shell-quoted command lines, each run with the export as its
            working directory.

    Returns:
        ``<command>: <last output line>`` for each command that exited non-zero.
    """
    if not commands:
        return ()
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="eawf-squash-") as scratch:
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", sha],
            capture_output=True,
            check=False,
            **no_window_kwargs(),
        )
        subprocess.run(
            ["tar", "-x", "-C", scratch], input=archive.stdout, check=False, **no_window_kwargs()
        )
        for command in commands:
            done = subprocess.run(
                shlex.split(command),
                cwd=scratch,
                capture_output=True,
                text=True,
                check=False,
                **no_window_kwargs(),
            )
            if done.returncode != 0:
                output = (done.stdout + done.stderr).strip().splitlines()
                failures.append(f"{command}: {output[-1] if output else f'exit {done.returncode}'}")
    return tuple(failures)


def _tidy_tree(repo: Path, tree: str, commands: Sequence[str]) -> str:
    """Return *tree* with each tidy command's edits to its tracked files written back.

    The tree is checked out alone into a scratch directory through a private
    index, each command runs there, and only the files the tree already tracks
    are read back, so a cache or byproduct a command leaves behind never enters
    the tree.
    """
    with tempfile.TemporaryDirectory(prefix="eawf-squash-tidy-") as scratch:
        index = Path(scratch) / "index"
        checkout = Path(scratch) / "tree"
        checkout.mkdir()
        env = {"GIT_INDEX_FILE": str(index)}
        _git(repo, "read-tree", tree, env=env)
        _git(repo, "checkout-index", "--all", f"--prefix={checkout}/", env=env)
        for command in commands:
            subprocess.run(
                shlex.split(command),
                cwd=checkout,
                capture_output=True,
                check=False,
                **no_window_kwargs(),
            )
        _git(repo, f"--work-tree={checkout}", "add", "--update", env=env)
        return _git(repo, "write-tree", env=env)


def _group_trees(
    repo: Path, plan: SquashPlan, on_conflict: ConflictStrategy, tidy: Sequence[str]
) -> tuple[SquashPlan, list[str], list[tuple[str, str]]]:
    """Build each Batch commit's tree under *on_conflict*, then tidy all but the last.

    Returns:
        The plan as finally grouped, each group's tree, and every carried path.
    """
    carried: list[tuple[str, str]] = []
    if on_conflict == "merge":
        plan, trees = _build_trees(repo, plan)
    else:
        trees, carried = _carry_trees(repo, plan)
    if tidy and trees:
        # The last Batch tree already holds the source tip's content outside state;
        # tidying it would leave the state commit to carry the revert.
        trees = [*(_tidy_tree(repo, tree, tidy) for tree in trees[:-1]), trees[-1]]
    return plan, trees, carried


def squash(
    repo: Path,
    *,
    base: str,
    source: str,
    branch: str | None,
    task_batches: Mapping[str, str],
    into: str | None,
    on_conflict: ConflictStrategy,
    tidy: Sequence[str] = (),
    checks: Sequence[str] = (),
) -> SquashResult:
    """Rewrite ``base..source`` into one commit per Batch plus a trailing state commit.

    Args:
        repo: The repository's working tree.
        base: The published commit the rewrite starts after.
        source: The tip to squash; its ref is never moved.
        branch: The branch the squashed history is to replace, recorded in the map;
            ``None`` for the branch ``HEAD`` names.
        task_batches: Each Task key mapped to its Batch key.
        into: The new branch to point at the squashed tip, or ``None`` to write
            only commit objects.
        on_conflict: ``merge`` joins two Batches whose work does not replay
            apart into one commit; ``carry`` keeps every Batch its own commit and
            lets the earlier one carry the conflicted paths at their source content.
        tidy: Command lines run on each Batch commit's own tree but the last
            before it is committed, such as an import sorter or formatter; what
            they change in tracked files is kept. Each tree is a snapshot, so a
            tidied tree changes no later one, and the trailing state commit still
            takes the source tip's tree.
        checks: Command lines run on each rewritten commit's tree.

    Returns:
        The rewritten commits, the Batches merged, the tree comparison and the map.

    Raises:
        PublishRefusedError: *into* already exists, *branch* is ``None`` while
            ``HEAD`` is detached, the range is empty or holds a merge, a commit
            cannot be placed, a replay conflicts with a state commit, or a git
            step failed.
    """
    if into is not None and _maybe_ref(repo, f"refs/heads/{into}"):
        raise PublishRefusedError(SQUASH_TARGET_EXISTS, f"branch {into} already exists")
    if branch is None:
        branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    base_sha = _git(repo, "rev-parse", f"{base}^{{commit}}")
    tip = _git(repo, "rev-parse", f"{source}^{{commit}}")
    fork = _git(repo, "merge-base", base_sha, tip)
    commits = scratch_commits(repo, fork, tip)
    straddling = frozenset(
        commit.sha
        for commit in commits
        if commit.is_state
        and any(not path.startswith(".ea/") for path in _changed_paths(repo, commit))
    )
    plan = plan_squash(fork, tip, commits, task_batches, straddling=straddling)
    plan, trees, carried = _group_trees(repo, plan, on_conflict, tidy)
    written: list[SquashedCommit] = []
    commit_map: dict[str, str | None] = {}
    batch_map: dict[str, str] = {}
    head = fork
    for group, tree in zip(plan.groups, trees, strict=True):
        tasks = _group_tasks(group, task_batches)
        message = group_message(group, tasks)
        head = _commit_tree(repo, tree, head, message, group.commits[-1].sha)
        for commit in group.commits:
            commit_map[commit.sha] = head
        for key in group.batch_keys:
            batch_map[key] = head
        written.append(
            SquashedCommit(
                sha=head,
                subject=message.split("\n", 1)[0],
                batch_keys=group.batch_keys,
                task_keys=tasks,
                source_commits=len(group.commits),
                folded=tuple(f"{c.sha[:12]} {c.subject}" for c in group.folded),
                carried=tuple(
                    sorted({path for sha, path in carried if sha in {c.sha for c in group.commits}})
                ),
                check_failures=_check_commit(repo, head, checks),
            )
        )
    tip_tree = _git(repo, "rev-parse", f"{tip}^{{tree}}")
    last_tree = trees[-1] if trees else _git(repo, "rev-parse", f"{fork}^{{tree}}")
    strays = _residual_strays(repo, last_tree, tip_tree, plan)
    if last_tree != tip_tree:
        state_source = plan.state_commits[-1] if plan.state_commits else None
        message = (
            _strip_trailers(state_source.message) + "\n"
            if state_source is not None
            else "state: record the squashed Batches\n"
        )
        head = _commit_tree(
            repo, tip_tree, head, message, state_source.sha if state_source else tip
        )
        written.append(
            SquashedCommit(
                sha=head,
                subject=message.split("\n", 1)[0],
                batch_keys=(),
                task_keys=(),
                source_commits=len(plan.state_commits),
                check_failures=(
                    *(f"non-state residue: {path}" for path in strays),
                    *_check_commit(repo, head, checks),
                ),
            )
        )
    for commit in plan.state_commits:
        commit_map[commit.sha] = head
    if into is not None:
        _git(repo, "update-ref", f"refs/heads/{into}", head, "")
    merged = tuple(group.batch_keys for group in plan.groups if len(group.batch_keys) > 1)
    logger.info(f"squash source={tip[:12]} commits={len(written)} merged={len(merged)} into={into}")
    return SquashResult(
        into=into,
        commits=tuple(written),
        merged=merged,
        tree_equal=_git(repo, "rev-parse", f"{head}^{{tree}}") == tip_tree,
        map=SquashMap(
            branch=branch,
            base=fork,
            source_head=tip,
            head=head,
            commits=commit_map,
            batches=batch_map,
        ),
    )


def _maybe_ref(repo: Path, ref: str) -> str | None:
    """Return the commit *ref* names, or ``None`` when it does not exist."""
    done = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    return done.stdout.strip() if done.returncode == 0 else None


__all__ = [
    "SQUASH_EMPTY_RANGE",
    "SQUASH_STATE_CONFLICT",
    "SQUASH_TARGET_EXISTS",
    "SQUASH_UNPLACEABLE",
    "SquashGroup",
    "SquashMap",
    "SquashPlan",
    "SquashResult",
    "SquashedCommit",
    "group_message",
    "plan_squash",
    "squash",
]
