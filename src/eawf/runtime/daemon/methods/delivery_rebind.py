"""``runtime.delivery.rebind_squashed``: supersede the rows naming a squashed branch's commits.

``eawf batch squash`` writes a branch again as one commit per Batch and answers
which rewritten commit carries each source commit. Once the branch points at
the squashed tip, every ledger row still naming a source commit names history
no branch reaches. This verb takes that map, reads the repository back to
confirm it -- the branch's head is the squashed tip, every rewritten commit is
on the branch, each Batch's commit carries its ``Batch:`` trailer, and each
rebound Task is named by a ``Task:`` trailer on a commit of its Batch -- and
only then appends the superseding rows :func:`~eawf.workflow.integration.rebind.plan_rebind`
plans, each through the ledger-append path.

The rows are planned again under the tree's session, so a second call finds
every line already rebound and appends nothing; a retry under the same
idempotency key replays the first answer.

``runtime.delivery.rebind_rewritten`` takes a history rewrite done in place
instead: every commit got a new hash and kept its tree, on whatever branch it
sat. None of the squash guards fits it, so it confirms only that each new
commit exists and that an old commit still held carries the new one's tree,
then appends the rows ``plan_rebind`` plans commit by commit and records each
applied pair in the committed ``.ea/store/rewrite_map.jsonl``. A live row of
the generation document keeps its old hash: its own verbs rewrite it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.store.compaction import read_document, settle_status_ledgers
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import IdempotencyKey, file_keyed_answer, keyed_answer
from eawf.runtime.daemon.methods.doctor import project_lock_urn
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.workflow.integration.publish import PUBLISHED_REF, read_membership
from eawf.workflow.integration.rebind import (
    RebindDropError,
    RebindRow,
    RewriteMap,
    RewriteMapError,
    RewritePair,
    append_rewrite_pairs,
    document_mentions,
    pairs_to_record,
    plan_rebind,
    read_rewrite_pairs,
    recorded_hashes,
    resolve_rewrite_pairs,
    rewrite_map_path,
    rewritten_commits,
)
from eawf.workflow.integration.squash import SquashMap

logger = logging.getLogger(__name__)

#: The verb that rebinds the ledger rows naming a squashed branch's source commits.
DELIVERY_REBIND_SQUASHED_METHOD: Final = "runtime.delivery.rebind_squashed"

#: Refusal: the branch no longer points at the squashed tip the map names.
REBIND_HEAD_MOVED: Final = "rebind_head_moved"

#: Refusal: a rewritten commit is missing or not on the branch.
REBIND_COMMIT_OFF_BRANCH: Final = "rebind_commit_off_branch"

#: Refusal: a rewritten commit lacks the Batch or Task trailer a row is rebound through.
REBIND_TRAILER_MISSING: Final = "rebind_trailer_missing"

#: Refusal: a dropped commit is the whole value of a binding field.
REBIND_DROP_BINDING: Final = "rebind_drop_binding"

#: Refusal: a live document row still names a source commit.
REBIND_LIVE_ROW: Final = "rebind_live_row"

#: The verb that rebinds the ledger rows naming commits a history rewrite replaced.
DELIVERY_REBIND_REWRITTEN_METHOD: Final = "runtime.delivery.rebind_rewritten"

#: Refusal: a new commit of the rewrite map is not in the repository.
REWRITE_COMMIT_MISSING: Final = "rewrite_commit_missing"

#: Refusal: an old commit still held has another tree than the commit it became.
REWRITE_TREE_MISMATCH: Final = "rewrite_tree_mismatch"

_TRAILER_RE: Final = re.compile(r"^(Batch|Task):\s+(\S+)\s*$", re.MULTILINE)


class RebindSquashedParams(BaseModel):
    """Params of :data:`DELIVERY_REBIND_SQUASHED_METHOD`.

    Attributes:
        actor: Who asked.
        idempotency_key: The client's name for this request.
        squash_map: The map ``eawf batch squash`` wrote; its ``head`` is the
            anchor, the tip the branch must point at.
    """

    model_config = ConfigDict(extra="forbid")

    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    squash_map: SquashMap


class RebindSquashedAnswer(BaseModel):
    """What one rebind answers with.

    Attributes:
        branch: The branch whose source commits were rebound.
        head: The squashed tip the branch points at.
        rows: How many superseding rows were appended.
        ledgers: How many of them each collection's ledger gained.
        rebound: Each source commit a row named, mapped to the commits it was
            rebound to; ``None`` where the reference was dropped.
        replayed: Whether the answer is the stored one of an earlier call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch: str
    head: str
    rows: int
    ledgers: dict[str, int]
    rebound: dict[str, list[str | None]]
    replayed: bool = False


def _refused(code: str, detail: str) -> DaemonValidationError:
    """Return the wire form of one rebind refusal."""
    return DaemonValidationError(f"validation_failed: {code}: {detail}")


def _git(repository: Path, *args: str) -> str | None:
    """Return the trimmed output of one read-only git command, or ``None`` on failure."""
    done = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    return done.stdout.strip() if done.returncode == 0 else None


def _trailers(message: str) -> set[tuple[str, str]]:
    """Return every ``(name, key)`` Batch or Task trailer *message* carries."""
    return {(name, key) for name, key in _TRAILER_RE.findall(message)}


def _read_commits(repository: Path, squash_map: SquashMap) -> dict[str, set[tuple[str, str]]]:
    """Confirm the squashed history is the branch and return each rewritten commit's trailers.

    Raises:
        DaemonValidationError: The branch moved off the squashed tip, a rewritten
            commit is missing or off the branch, or a Batch's commit does not
            name it.
    """
    branch_head = _git(
        repository, "rev-parse", "--verify", "--quiet", f"refs/heads/{squash_map.branch}^{{commit}}"
    )
    if branch_head != squash_map.head:
        raise _refused(
            REBIND_HEAD_MOVED,
            f"{squash_map.branch} is at {branch_head or 'nothing'}, not the squashed tip "
            f"{squash_map.head}; point it at the squashed history first",
        )
    published = _git(repository, "rev-parse", "--verify", "--quiet", f"{PUBLISHED_REF}^{{commit}}")
    sources: dict[str, set[str]] = {}
    for old, new in squash_map.commits.items():
        if new is not None:
            sources.setdefault(new, set()).add(old)
    trailers: dict[str, set[tuple[str, str]]] = {}
    for sha in sorted({*sources, *squash_map.batches.values()}):
        message = _git(repository, "show", "-s", "--format=%B", sha)
        on_branch = _ancestor(repository, sha, squash_map.head)
        # A commit neither the branch nor the published history reaches may be
        # rebound to its published equivalent; every other target is on the branch.
        published_orphan = (
            published is not None
            and sha not in squash_map.batches.values()
            and _ancestor(repository, sha, published)
            and all(_orphaned(repository, old, squash_map.head, published) for old in sources[sha])
        )
        if message is None or not (on_branch or published_orphan):
            raise _refused(
                REBIND_COMMIT_OFF_BRANCH, f"{sha} is not a commit on {squash_map.branch}"
            )
        trailers[sha] = _trailers(message)
    for batch, sha in sorted(squash_map.batches.items()):
        if ("Batch", batch) not in trailers[sha]:
            raise _refused(REBIND_TRAILER_MISSING, f"{sha} carries no 'Batch: {batch}' trailer")
    return trailers


def _ancestor(repository: Path, older: str, newer: str) -> bool:
    """Return whether *older* is an ancestor of, or equal to, *newer*."""
    done = subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", older, newer],
        capture_output=True,
        check=False,
        **no_window_kwargs(),
    )
    return done.returncode == 0


def _orphaned(repository: Path, sha: str, head: str, published: str) -> bool:
    """Return whether neither the branch at *head* nor the published history reaches *sha*."""
    return not _ancestor(repository, sha, head) and not _ancestor(repository, sha, published)


def _source_tasks(repository: Path, squash_map: SquashMap) -> set[str]:
    """Return every Task a ``Task:`` trailer of a source commit names.

    Raises:
        DaemonValidationError: A source commit can no longer be read, so whether
            it named a Task is unknown.
    """
    named: set[str] = set()
    for sha in sorted(squash_map.commits):
        message = _git(repository, "show", "-s", "--format=%B", sha)
        if message is None:
            raise _refused(
                REBIND_COMMIT_OFF_BRANCH,
                f"source commit {sha} cannot be read; keep the pre-squash history reachable",
            )
        named |= {key for name, key in _trailers(message) if name == "Task"}
    return named


def _check_tasks(
    rows: tuple[RebindRow, ...],
    trailers: dict[str, set[tuple[str, str]]],
    source_tasks: Callable[[], set[str]],
) -> None:
    """Refuse a Task row rebound through a Batch no commit names the Task on.

    A Task that landed on evidence alone has no commit in the source range
    either, so it rebinds to its Batch's commit; a Task the source commits
    named but the squashed ones do not was dropped by the squash.

    Raises:
        DaemonValidationError: No rewritten commit carries both the row's Batch
            and Task trailers while a source commit names the Task.
    """
    for row in rows:
        if row.task_key is None or row.batch_key is None:
            continue
        wanted = {("Batch", row.batch_key), ("Task", row.task_key)}
        if any(wanted <= named for named in trailers.values()):
            continue
        if row.task_key in source_tasks():
            raise _refused(
                REBIND_TRAILER_MISSING,
                f"no squashed commit names Task {row.task_key} beside Batch {row.batch_key}",
            )


def rebind_squashed(
    context: Epoch2RootContext, args: RebindSquashedParams, *, now: datetime
) -> RebindSquashedAnswer:
    """Append a superseding row for every ledger line naming a squashed source commit.

    Args:
        context: The native context of the tree whose ledgers are rebound.
        args: The validated request.
        now: The instant every row records.

    Returns:
        The rows appended; none when every line was already rebound.

    Raises:
        DaemonValidationError: The key already answered other parameters, the
            repository does not show the squashed history on the branch, a
            trailer a row is rebound through is missing, or a live document row
            still names a source commit. Nothing was appended.
    """
    params = args.model_dump(mode="json")
    replayed = keyed_answer(
        context, method=DELIVERY_REBIND_SQUASHED_METHOD, key=args.idempotency_key, params=params
    )
    if replayed is not None:
        return RebindSquashedAnswer.model_validate({**replayed, "replayed": True})
    squash_map = args.squash_map
    repository = context.identity.tree_root.parent
    trailers = _read_commits(repository, squash_map)
    trees = {sha: _git(repository, "rev-parse", f"{sha}^{{tree}}") or "" for sha in trailers}
    parents = {sha: _git(repository, "rev-parse", f"{sha}^") or "" for sha in trailers}
    with context.session([project_lock_urn(context)]) as session:
        # The local Run ledger is seeded from the committed one on first touch;
        # planning before it exists would miss every Run row.
        settle_status_ledgers(session.document_path)
        named = document_mentions(read_document(session.document_path), squash_map)
        if named:
            raise _refused(
                REBIND_LIVE_ROW,
                f"the generation document still names {', '.join(named)}; close or "
                "reconcile the rows naming them first",
            )
        task_batches = read_membership(context.identity.tree_root).task_batches
        try:
            rows = plan_rebind(
                session.document_path,
                squash_map,
                task_batches,
                trees=trees,
                parents=parents,
                recorded_at=now,
            )
        except RebindDropError as error:
            raise _refused(REBIND_DROP_BINDING, str(error)) from error
        _check_tasks(rows, trailers, functools.cache(lambda: _source_tasks(repository, squash_map)))
        for row in rows:
            commit_ledger_append(session, row.record)
    rebound: dict[str, set[str | None]] = {}
    for row in rows:
        for old, new in row.rebinds.items():
            rebound.setdefault(old, set()).add(new)
    answer = RebindSquashedAnswer(
        branch=squash_map.branch,
        head=squash_map.head,
        rows=len(rows),
        ledgers=dict(Counter(row.record.collection.value for row in rows)),
        rebound={old: sorted(news, key=str) for old, news in sorted(rebound.items())},
    )
    file_keyed_answer(
        context,
        method=DELIVERY_REBIND_SQUASHED_METHOD,
        key=args.idempotency_key,
        params=params,
        answer=answer.model_dump(mode="json"),
        at=now,
    )
    logger.info(f"rebind_squashed branch={squash_map.branch} rows={len(rows)}")
    return answer


@native_mutator(DELIVERY_REBIND_SQUASHED_METHOD)
async def _rebind_squashed(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Rebind the ledger rows naming a squashed branch's source commits."""
    args = native_params(RebindSquashedParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(rebind_squashed, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


class RebindRewrittenParams(BaseModel):
    """Params of :data:`DELIVERY_REBIND_REWRITTEN_METHOD`.

    Attributes:
        actor: Who asked.
        idempotency_key: The client's name for this request.
        rewrite_map: Which commit each rewritten commit became.
        dry_run: Answer the counts a real run would without writing anything.
    """

    model_config = ConfigDict(extra="forbid")

    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    rewrite_map: RewriteMap
    dry_run: bool = False


class RebindRewrittenAnswer(BaseModel):
    """What one rewrite rebind answers with.

    Attributes:
        rebound: How many superseding rows were appended, or would be on a dry run.
        per_collection: How many of them each collection's ledger gained.
        unmapped: How many distinct hashes the effective ledger lines record
            that the map names neither as old nor new and the repository holds
            as no tree or blob: commits the map left out, kept as they are.
        pairs_recorded: How many pairs were appended to the committed record.
        dry_run: Whether nothing was written.
        replayed: Whether the answer is the stored one of an earlier call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rebound: int
    per_collection: dict[str, int]
    unmapped: int
    pairs_recorded: int
    dry_run: bool = False
    replayed: bool = False


def recorded_rewrites(tree_root: Path) -> dict[str, str]:
    """Return the recorded history-rewrite map of a tree, or refuse a malformed one.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        Each recorded old commit mapped to the commit it finally became;
        empty when nothing is recorded.

    Raises:
        DaemonValidationError: ``rewrite_map_invalid``: a line of the record
            is not a rewrite pair, or its pairs form a cycle.
        OSError: The record exists but cannot be read.
    """
    try:
        return rewritten_commits(tree_root)
    except RewriteMapError as error:
        record = rewrite_map_path(Path(".ea")).as_posix()
        raise DaemonValidationError(f"validation_failed: {error} of {record}") from error


def _objects(repository: Path, revisions: list[str]) -> list[tuple[str, str] | None]:
    """Return the ``(object id, type)`` each revision names, or ``None`` where none.

    One ``git cat-file --batch-check`` reads them all, so a map of hundreds of
    commits costs one process. A repository git cannot read holds nothing.

    Args:
        repository: The repository's working directory.
        revisions: The revisions to look up, each one line of git's input.

    Returns:
        One entry per revision, in order: its object id and type, or ``None``
        where it names no object or is ambiguous. Every entry is ``None``
        when git failed or answered another number of lines.
    """
    done = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        input="".join(f"{revision}\n" for revision in revisions),
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    lines = done.stdout.splitlines() if done.returncode == 0 else []
    if len(lines) != len(revisions):
        return [None] * len(revisions)
    found: list[tuple[str, str] | None] = []
    for line in lines:
        oid, _, kind = line.partition(" ")
        found.append(None if kind in ("missing", "ambiguous") else (oid, kind))
    return found


def _check_rewrite(
    repository: Path, rewrite_map: RewriteMap
) -> tuple[dict[str, str], dict[str, str]]:
    """Confirm every new commit exists and keeps its old commit's tree.

    Args:
        repository: The repository's working directory.
        rewrite_map: Which commit each rewritten commit became.

    Returns:
        Each new commit mapped to its tree, and each new commit that has a
        parent mapped to its first parent.

    Raises:
        DaemonValidationError: A new commit is not in the repository, or an old
            commit still held has another tree than the commit it became.
    """
    news = sorted(set(rewrite_map.commits.values()))
    olds = sorted(old for old, new in rewrite_map.commits.items() if old != new)
    revisions = [
        *(f"{sha}^{{{kind}}}" for sha in news for kind in ("commit", "tree")),
        *(f"{sha}^" for sha in news),
        *(f"{sha}^{{{kind}}}" for sha in olds for kind in ("commit", "tree")),
    ]
    found = dict(zip(revisions, _objects(repository, revisions), strict=True))
    trees: dict[str, str] = {}
    for sha in news:
        commit, tree = found[f"{sha}^{{commit}}"], found[f"{sha}^{{tree}}"]
        if commit is None or tree is None:
            raise _refused(REWRITE_COMMIT_MISSING, f"new commit {sha} is not in the repository")
        trees[sha] = tree[0]
    for old in olds:
        held, tree = found[f"{old}^{{commit}}"], found[f"{old}^{{tree}}"]
        new = rewrite_map.commits[old]
        if held is not None and tree is not None and tree[0] != trees[new]:
            raise _refused(
                REWRITE_TREE_MISMATCH,
                f"old commit {old} has tree {tree[0]}, but {new} has tree {trees[new]}",
            )
    parents = {sha: parent[0] for sha in news if (parent := found[f"{sha}^"]) is not None}
    return trees, parents


def _unmapped(repository: Path, state_path: Path, rewrite_map: RewriteMap) -> int:
    """Count the recorded hashes the map leaves out that are no tree or blob.

    Args:
        repository: The repository's working directory.
        state_path: The generation's ``state.json``; its ledgers are read.
        rewrite_map: Which commit each rewritten commit became.

    Returns:
        How many distinct hashes the effective ledger lines name that the map
        names neither as old nor as new and the repository holds as no tree
        or blob.
    """
    named = set(rewrite_map.commits) | set(rewrite_map.commits.values())
    candidates = sorted(recorded_hashes(state_path) - named)
    return sum(
        1
        for found in _objects(repository, candidates)
        if found is None or found[1] not in ("tree", "blob")
    )


def _plan_rewrite(
    repository: Path,
    state_path: Path,
    record_path: Path,
    args: RebindRewrittenParams,
    *,
    trees: dict[str, str],
    parents: dict[str, str],
    now: datetime,
) -> tuple[tuple[RebindRow, ...], tuple[RewritePair, ...], int]:
    """Plan the rows and pairs one rewrite rebind appends, writing nothing.

    Args:
        repository: The repository's working directory.
        state_path: The generation's ``state.json``; its ledgers are read.
        record_path: The committed record of applied rewrite pairs.
        args: The validated request.
        trees: Each new commit mapped to its tree.
        parents: Each new commit mapped to its first parent.
        now: The instant every row and recorded pair carries.

    Returns:
        The superseding rows, the pairs to record and the unmapped count.

    Raises:
        DaemonValidationError: ``rewrite_map_invalid``: the record is
            malformed, or the recorded pairs and the new ones form a cycle,
            which would leave every later reader of the append-only record
            refused.
    """
    try:
        recorded = read_rewrite_pairs(record_path)
        pairs = pairs_to_record(args.rewrite_map, recorded, at=now)
        resolve_rewrite_pairs((*recorded, *pairs))
    except RewriteMapError as error:
        raise DaemonValidationError(
            f"validation_failed: {error} of the recorded pairs followed by this map's new ones"
        ) from error
    rows = plan_rebind(
        state_path, args.rewrite_map, {}, trees=trees, parents=parents, recorded_at=now
    )
    return rows, pairs, _unmapped(repository, state_path, args.rewrite_map)


def rebind_rewritten(
    context: Epoch2RootContext, args: RebindRewrittenParams, *, now: datetime
) -> RebindRewrittenAnswer:
    """Append a superseding row for every ledger line naming a rewritten commit.

    A dry run plans from a read of the selected generation without opening a
    session, so it writes no lock, seal or fault file, and it reads the
    ledgers a settled tree would hold without settling them.

    Args:
        context: The native context of the tree whose ledgers are rebound.
        args: The validated request.
        now: The instant every row and recorded pair carries.

    Returns:
        The counts; none rebound once every line names a new commit already.

    Raises:
        DaemonValidationError: The key already answered other parameters, a new
            commit is not in the repository, an old commit still held has
            another tree, or the recorded pairs are malformed or would form a
            cycle with the new ones. Nothing was written.
    """
    params = args.model_dump(mode="json")
    if not args.dry_run:
        replayed = keyed_answer(
            context,
            method=DELIVERY_REBIND_REWRITTEN_METHOD,
            key=args.idempotency_key,
            params=params,
        )
        if replayed is not None:
            return RebindRewrittenAnswer.model_validate({**replayed, "replayed": True})
    repository = context.identity.tree_root.parent
    trees, parents = _check_rewrite(repository, args.rewrite_map)
    record_path = context.declared_path(rewrite_map_path(context.identity.tree_root))
    plan = functools.partial(
        _plan_rewrite,
        repository,
        record_path=record_path,
        args=args,
        trees=trees,
        parents=parents,
        now=now,
    )
    if args.dry_run:
        authority = context.require_selected_generation()
        assert authority.target is not None and authority.generation_id is not None
        rows, pairs, unmapped = plan(
            authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
        )
    else:
        with context.session([project_lock_urn(context)]) as session:
            # The local Run ledger is seeded from the committed one on first touch.
            settle_status_ledgers(session.document_path)
            rows, pairs, unmapped = plan(session.document_path)
            for row in rows:
                commit_ledger_append(session, row.record)
            append_rewrite_pairs(record_path, pairs)
    answer = RebindRewrittenAnswer(
        rebound=len(rows),
        per_collection=dict(Counter(row.record.collection.value for row in rows)),
        unmapped=unmapped,
        pairs_recorded=len(pairs),
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        file_keyed_answer(
            context,
            method=DELIVERY_REBIND_REWRITTEN_METHOD,
            key=args.idempotency_key,
            params=params,
            answer=answer.model_dump(mode="json"),
            at=now,
        )
    logger.info(
        f"rebind_rewritten rows={len(rows)} pairs={len(pairs)} unmapped={unmapped} "
        f"dry_run={args.dry_run}"
    )
    return answer


@native_mutator(DELIVERY_REBIND_REWRITTEN_METHOD)
async def _rebind_rewritten(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Rebind the ledger rows naming commits a history rewrite replaced."""
    args = native_params(RebindRewrittenParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(rebind_rewritten, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


__all__ = [
    "DELIVERY_REBIND_REWRITTEN_METHOD",
    "DELIVERY_REBIND_SQUASHED_METHOD",
    "REBIND_COMMIT_OFF_BRANCH",
    "REBIND_DROP_BINDING",
    "REBIND_HEAD_MOVED",
    "REBIND_LIVE_ROW",
    "REBIND_TRAILER_MISSING",
    "REWRITE_COMMIT_MISSING",
    "REWRITE_TREE_MISMATCH",
    "RebindRewrittenAnswer",
    "RebindRewrittenParams",
    "RebindSquashedAnswer",
    "RebindSquashedParams",
    "rebind_rewritten",
    "rebind_squashed",
    "recorded_rewrites",
]
