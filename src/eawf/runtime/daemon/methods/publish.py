"""Binding staged adoptions to the commits a publish produced.

``runtime.publish.bind_staged``: the daemon half of ``eawf publish``.

An adoption of scratch work is staged in the generation's local tier
until the commit carrying it is published. At publish the caller names, per
Batch, the commits carrying its work; the daemon binds each staged row to
the one whose tree equals the staged tree, reads the change back from the
repository, files the adoption and the generation it selects at the
published commit, exactly as an adoption of already-published work would,
and removes the staged row. A row whose Batch has since closed needs no
binding and is passed over.

Every row is checked before anything is written, so a row that cannot bind
leaves every row staged and every ledger untouched. A row whose Batch
already stands on the published commit is only unstaged, so a retry after
a crash between the append and the removal writes nothing twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.delivery.adoption import ADOPTION_STATUS, LandedAdoption
from eawf.kernel.delivery.integration import IntegrationGeneration
from eawf.kernel.delivery.receipts import RevisionBinding, RevisionRefKind, canonical_digest
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import BranchName
from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.urns import BatchUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.paths import staged_dir
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import GENERATION_STATUS, read_generation_ledger
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.integration.recovery import generation_record_key, integration_generation_id
from eawf.workflow.integration.publish_binding import (
    PublishedBinding,
    PublishedCommit,
    StagedBinding,
    StagedBindingRefusedError,
    bind_staged,
)

logger = logging.getLogger(__name__)

#: The verb that binds the staged rows to the published commits.
PUBLISH_BIND_STAGED_METHOD: Final = "runtime.publish.bind_staged"

#: The integration policy a published generation records.
_PUBLISH_POLICY: Final = canonical_digest({"integration": "publish", "version": 1})


class BatchCandidates(BaseModel):
    """The commits a publish produced for one Batch.

    Attributes:
        batch_ref: The Batch.
        commits: The commits that carry it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: BatchUrn
    commits: tuple[PublishedCommit, ...] = Field(min_length=1)


class BindStagedParams(BaseModel):
    """Params of :data:`PUBLISH_BIND_STAGED_METHOD`.

    The bound adoptions stay attributed to whoever staged them, so the
    request names no actor of its own.

    Attributes:
        branch: The branch the commits were published on.
        candidates: Per Batch, the commits the publish produced.
    """

    model_config = ConfigDict(extra="forbid")

    branch: BranchName
    candidates: tuple[BatchCandidates, ...] = Field(min_length=1)


class BoundRow(BaseModel):
    """One staged row and where it now stands.

    Attributes:
        batch_ref: The Batch.
        head_sha: The published commit it binds.
        generation_id: The generation it selected.
        replayed: Whether the Batch already stood on that commit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: str
    head_sha: str
    generation_id: str
    replayed: bool


class BindStagedAnswer(BaseModel):
    """What one bind answers with.

    Attributes:
        bound: The rows bound, in Batch order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bound: tuple[BoundRow, ...]


@dataclass(frozen=True, slots=True)
class _Planned:
    """One staged row resolved and read back, ready to file."""

    path: Path
    adoption: LandedAdoption | None
    generation: IntegrationGeneration

    @property
    def replayed(self) -> bool:
        """Return whether the Batch already stood on the published commit."""
        return self.adoption is None


def _refused(code: str, detail: str) -> DaemonValidationError:
    """Return the wire form of one bind refusal."""
    return DaemonValidationError(f"validation_failed: {code}: {detail}")


def _git(repository: Path, *args: str) -> str:
    """Return the trimmed output of one read-only git command.

    Raises:
        DaemonValidationError: The command failed.
    """
    done = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    if done.returncode != 0:
        raise _refused("publish_commit_unread", f"git {' '.join(args)} failed")
    return done.stdout.strip()


def _ancestor(repository: Path, older: str, newer: str) -> bool:
    """Return whether *older* is an ancestor of, or equal to, *newer*."""
    done = subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", older, newer],
        capture_output=True,
        check=False,
        **no_window_kwargs(),
    )
    return done.returncode == 0


def _binding(
    batch: DeliveryBatch,
    *,
    ref_kind: RevisionRefKind,
    head_sha: str,
    tree_sha: str,
    parent_sha: str | None,
    generation: int,
    manifest_digest: str,
    affected: tuple[str, ...],
    now: datetime,
) -> RevisionBinding:
    """Return one revision binding of *batch* at *generation*."""
    return RevisionBinding(
        repository_ref=batch.repository_ref,
        ref_kind=ref_kind,
        head_sha=head_sha,
        tree_sha=tree_sha,
        parent_sha=parent_sha,
        batch_ref=batch.urn,
        integration_generation=generation,
        manifest_digest=manifest_digest,
        criteria_digest=canonical_digest(list(affected)),
        policy_digest=_PUBLISH_POLICY,
        environment_digest=None,
        bound_at=now,
    )


def _plan_row(
    repository: Path,
    *,
    path: Path,
    bound: PublishedBinding,
    batch: DeliveryBatch,
    branch: str,
    branch_head: str,
    head: IntegrationGeneration | None,
    affected: tuple[str, ...],
    now: datetime,
) -> _Planned:
    """Read one bound row's change back and build what files it.

    Raises:
        DaemonValidationError: The branch does not carry the commit, the
            commit has no single parent, or the Batch stands on a commit
            the published one does not contain.
    """
    sha = bound.head_sha
    if not _ancestor(repository, sha, branch_head):
        raise _refused(
            "publish_commit_unreachable", f"branch {branch} at {branch_head} does not carry {sha}"
        )
    tree, *parents = _git(repository, "show", "-s", "--format=%T %P", sha).split()
    if len(parents) != 1:
        raise _refused("publish_commit_unread", f"{sha} has {len(parents)} parents, not one")
    base = parents[0]
    if head is not None and not _ancestor(repository, head.integrated_revision.head_sha, sha):
        raise _refused(
            "publish_head_behind",
            f"batch {batch.key} already stands on {head.integrated_revision.head_sha}, which "
            f"{sha} does not contain",
        )
    base_tree, *base_parents = _git(repository, "show", "-s", "--format=%T %P", base).split()
    paths = tuple(_git(repository, "diff", "--name-only", base, sha).splitlines())
    diff = subprocess.run(
        ["git", "-C", str(repository), "diff", "--binary", base, sha],
        capture_output=True,
        check=False,
        **no_window_kwargs(),
    ).stdout
    diff_digest = f"sha256:{hashlib.sha256(diff).hexdigest()}"
    staged = bound.staged
    adoption = LandedAdoption(
        batch_ref=staged.batch_ref,
        task_refs=staged.task_refs,
        base_commit=base,
        head_sha=sha,
        tree_sha=tree,
        parent_sha=base,
        target_branch=branch,
        target_head_sha=branch_head,
        changed_paths=paths,
        report_verdict=staged.report_verdict,
        evidence_refs=staged.evidence_refs,
        adopted_by=staged.staged_by,
        adopted_at=staged.staged_at,
    )
    ordinal = 2 if head is None else head.generation + 1
    manifest = adoption.digest()
    source = _binding(
        batch,
        ref_kind=RevisionRefKind.TARGET_BRANCH,
        head_sha=base,
        tree_sha=base_tree,
        parent_sha=base_parents[0] if base_parents else None,
        generation=1,
        manifest_digest=manifest,
        affected=(),
        now=now,
    )
    generation = IntegrationGeneration(
        id=integration_generation_id(ordinal),
        batch_ref=staged.batch_ref,
        generation=ordinal,
        parent_generation_id=None if head is None else head.id,
        source_candidate_bundle_id=adoption.bundle_key(),
        source_base=source,
        target_base=source if head is None else head.integrated_revision,
        integrated_revision=_binding(
            batch,
            ref_kind=RevisionRefKind.INTEGRATION,
            head_sha=sha,
            tree_sha=tree,
            parent_sha=base,
            generation=ordinal,
            manifest_digest=manifest,
            affected=affected,
            now=now,
        ),
        patch_digest=diff_digest,
        diff_digest=diff_digest,
        tree_digest=canonical_digest(tree),
        changed_paths=paths,
        affected_task_refs=staged.task_refs,
        affected_criterion_ids=affected,
        integration_policy_digest=_PUBLISH_POLICY,
        selected=True,
        created_at=now,
    )
    return _Planned(path=path, adoption=adoption, generation=generation)


def bind_published(
    context: Epoch2RootContext, args: BindStagedParams, *, now: datetime
) -> BindStagedAnswer:
    """Bind every staged row to the published commit carrying its tree.

    Args:
        context: The native context of the tree the Batches live in.
        args: The validated request.
        now: When the generations are filed.

    Returns:
        Each row bound, with the generation it selected.

    Raises:
        DaemonValidationError: A staged row names a Batch the request
            offers no commits for, matches none or several, its commit is
            not on the branch, or its Batch stands on a commit the
            published one does not contain. Nothing is written then.
    """
    repository = context.identity.tree_root.parent
    offered = {str(item.batch_ref): item.commits for item in args.candidates}
    branch_head = _git(repository, "rev-parse", "--verify", f"refs/heads/{args.branch}^{{commit}}")
    with context.session(list(offered)) as session:
        directory = staged_dir(session.document_path)
        files = sorted(directory.glob("*.json"))
        batches = document_rows(session.read_document(), Epoch2Collection.BATCH)
        # A Batch that closed needs no binding, so its staged rows are left where they are.
        rows = [
            (path, row)
            for path, row in (
                (path, StagedBinding.model_validate_json(path.read_bytes())) for path in files
            )
            if row.batch_ref.entity_key in batches
        ]
        stray = sorted({str(row.batch_ref) for _, row in rows} - set(offered))
        if stray:
            raise _refused(
                "publish_batch_unoffered", f"staged rows name batches with no commits: {stray}"
            )
        tasks = document_rows(session.read_document(), Epoch2Collection.TASK)
        ledger = session.ledger_path(Epoch2Collection.BATCH)
        planned: list[_Planned] = []
        for path, row in rows:
            try:
                bound = bind_staged(row, offered[str(row.batch_ref)])
            except StagedBindingRefusedError as error:
                raise _refused(error.code, str(error).split(": ", 1)[1]) from error
            try:
                batch = DeliveryBatch.model_validate(batches.get(row.batch_ref.entity_key))
                carried = [Task.model_validate(tasks.get(ref.entity_key)) for ref in row.task_refs]
            except ValidationError as error:
                raise _refused(
                    "publish_record_absent",
                    f"the tree holds no readable live batch {row.batch_ref.entity_key} with "
                    "the staged tasks",
                ) from error
            head = read_generation_ledger(ledger, row.batch_ref).head
            if head is not None and head.integrated_revision.head_sha == bound.head_sha:
                planned.append(_Planned(path=path, adoption=None, generation=head))
                continue
            affected = tuple(sorted({item.id for task in carried for item in task.criteria}))
            planned.append(
                _plan_row(
                    repository,
                    path=path,
                    bound=bound,
                    batch=batch,
                    branch=args.branch,
                    branch_head=branch_head,
                    head=head,
                    affected=affected,
                    now=now,
                )
            )
        for item in planned:
            if item.adoption is not None:
                commit_ledger_append(
                    session,
                    LedgerRecord(
                        collection=Epoch2Collection.BATCH,
                        record_key=item.adoption.record_key(),
                        status=ADOPTION_STATUS,
                        recorded_at=now,
                        payload=item.adoption.model_dump(mode="json"),
                    ),
                )
                commit_ledger_append(
                    session,
                    LedgerRecord(
                        collection=Epoch2Collection.BATCH,
                        record_key=generation_record_key(item.generation),
                        status=GENERATION_STATUS,
                        recorded_at=now,
                        payload=item.generation.model_dump(mode="json"),
                    ),
                )
            context.declared_path(item.path).unlink()
    logger.info(
        f"bind_staged branch={args.branch} rows={len(planned)} "
        f"replayed={sum(item.replayed for item in planned)}"
    )
    return BindStagedAnswer(
        bound=tuple(
            BoundRow(
                batch_ref=str(item.generation.batch_ref),
                head_sha=item.generation.integrated_revision.head_sha,
                generation_id=item.generation.id,
                replayed=item.replayed,
            )
            for item in planned
        )
    )


@native_mutator(PUBLISH_BIND_STAGED_METHOD)
async def _bind_staged(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Bind the staged rows to the commits a publish produced."""
    args = native_params(BindStagedParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(bind_published, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


__all__ = [
    "PUBLISH_BIND_STAGED_METHOD",
    "BatchCandidates",
    "BindStagedAnswer",
    "BindStagedParams",
    "BoundRow",
    "bind_published",
]
