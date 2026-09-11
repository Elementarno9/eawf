"""The apply: preconditions under lock, then one journaled transaction.

The order of the stages here is the safety argument, so it is worth
stating plainly.

The **fence** runs first, before anything else in the function, and it
returns the typed target every later write derives its paths from. A bug
further down cannot reach a write helper without one.

The **workspace check** runs second, before plan mode, because plan mode
mints a URN for every imported record under the workspace key. Finding
out afterwards that the key is unregistered would leave a corpus
addressed at a key nothing resolves.

The **authority locks** come next and are held for everything after them.
They are the enforcement behind "fallback writers disabled": the direct
``portalocker`` write path an operator-facing command falls back to when
the daemon is unavailable takes the same sibling lock on the same file,
so while the cutover holds it, that path cannot proceed. The maintenance
marker written later in the transaction is the legible reason; the lock
is the mechanism.

**Quiescence** then proves nobody else is mid-flight, and the **re-census
under the locks** recomputes plan mode and compares its approval digest
with the one the operator approved. That single comparison covers both
halves of the precondition: a corpus that moved, and an approval that is
stale.

Everything above writes nothing but lock holder records. An apply that
refuses a precondition, or that finds its generation already selected,
leaves the tree byte-identical -- no journal row, no maintenance marker,
no staging directory. The transaction, and the journal with it, begins at
the first act that changes the tree.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import Field

from eawf.kernel.migration.epoch2.canary import DisposableTarget
from eawf.kernel.migration.epoch2.census import SourceCensus
from eawf.kernel.migration.epoch2.errors import (
    MigrationPlanDigestStaleError,
    MigrationPlanNotApplicableError,
    MigrationSourceChangedError,
    MigrationWorkspaceNotRegisteredError,
)
from eawf.kernel.migration.epoch2.generation import (
    GenerationSelection,
    build_generation,
    generation_ids,
    read_marker,
    read_selection,
    read_smoke,
    select_generation,
    write_marker,
)
from eawf.kernel.migration.epoch2.journal import CutoverJournal, CutoverStage
from eawf.kernel.migration.epoch2.manifest import BackupRecord, RollbackBoundary
from eawf.kernel.migration.epoch2.plan_mode import (
    Epoch2PlanRequest,
    MigrationPlan,
    plan_cutover,
)
from eawf.kernel.migration.epoch2.quiescence import quiescence_findings, require_quiescent
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot, digest_bytes
from eawf.platform.registry.models import Registry, RegistryReadError, read_registry
from eawf.platform.registry.workspace import WorkspaceMutationError, get_workspace
from eawf.runtime.lock import portalock

logger = logging.getLogger(__name__)


#: The JSON-RPC method the cutover apply is served under.
EPOCH2_APPLY_METHOD: Final = "migration.epoch2.apply"

#: The locator the restore point records the workspace registry under. A
#: logical name rather than the file's real path, because the registry
#: lives in the operator's home directory and a manifest never records
#: where on a machine anything was.
REGISTRY_LOCATOR: Final = "registry.json"

#: What the restore point records for an authority surface that does not
#: exist yet. Distinct from a digest over empty bytes, which would claim
#: the file was there and empty.
ABSENT_SURFACE: Final = "absent"

#: How long the apply waits for one authority lock. Short on purpose: a
#: surface somebody else is holding is a quiescence failure to report,
#: not a queue to join.
LOCK_TIMEOUT_SECONDS: Final = 5.0


class Epoch2ApplyRequest(StrictMigrationModel):
    """One request to apply an approved cutover plan.

    Attributes:
        plan_request: The plan-mode request, carried whole so the apply
            recomputes exactly the plan that was approved rather than a
            reassembled near-copy of it.
        target_root: The tree the new generation is built in. It must
            declare itself a disposable canary.
        registry_path: The workspace registry the addressing key is
            resolved against.
        plan_digest: The approval digest the operator approved. The apply
            recomputes the plan and refuses unless the two agree.
        accepted_unresolved_rows: The addresses of rows the operator has
            explicitly accepted will not be imported. Empty by default,
            which is the refusing case: a plan with unresolved rows does
            not apply until somebody names every one of them.
    """

    plan_request: Epoch2PlanRequest
    target_root: Annotated[str, Field(min_length=1)]
    registry_path: Annotated[str, Field(min_length=1)]
    plan_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    accepted_unresolved_rows: tuple[Annotated[str, Field(min_length=1, max_length=200)], ...] = ()


class CutoverResult(StrictMigrationModel):
    """What one apply did, or declined to do again.

    Attributes:
        applied: ``True`` when this run built and selected a generation,
            ``False`` when the approved generation was already selected
            and the run wrote nothing.
        generation_id: The selected generation.
        manifest_digest: The manifest that built it.
        approval_digest: The plan digest the operator approved.
        rollback_boundary: How far the cutover has gone.
        journal_rows: How many journal rows this run appended. ``0``
            proves a second apply wrote nothing.
        target_rows: How many records the manifest accounts for.
        generation_count: How many generations the tree now holds. A
        cutover leaves the previous generation in place, so this is the
            number an operator watches: it is the disk the tree is
            carrying until somebody prunes it.
        accepted_unresolved_rows: The unresolved rows the operator
            accepted, carried into the result so the refusal that was
            waived is visible in the answer.
    """

    applied: bool
    generation_id: Annotated[str, Field(min_length=1, max_length=64)]
    manifest_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    approval_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    rollback_boundary: RollbackBoundary
    journal_rows: Annotated[int, Field(ge=0)]
    target_rows: Annotated[int, Field(ge=0)]
    generation_count: Annotated[int, Field(ge=1)]
    accepted_unresolved_rows: tuple[str, ...]


def _require_registered_workspace(request: Epoch2ApplyRequest) -> Registry:
    """Resolve the addressing workspace before any URN is minted.

    Args:
        request: The validated apply request.

    Returns:
        The registry the key resolved in.

    Raises:
        MigrationWorkspaceNotRegisteredError: The registry is missing or
            unreadable, or it holds no such workspace. Both are the same
            answer to the only question asked here -- can this corpus be
            addressed -- so they carry one code.
    """
    key = request.plan_request.workspace_key
    try:
        registry = read_registry(Path(request.registry_path))
    except RegistryReadError as error:
        raise MigrationWorkspaceNotRegisteredError(
            f"the workspace registry cannot be read, so workspace {key!r} cannot be "
            f"confirmed before the corpus is minted under it: {error}"
        ) from error
    try:
        get_workspace(registry, key)
    except WorkspaceMutationError as error:
        raise MigrationWorkspaceNotRegisteredError(
            f"workspace {key!r} is not registered, so every URN the import would mint "
            f"addresses a key nothing resolves: {error}"
        ) from error
    return registry


@contextmanager
def authority_locks(target: DisposableTarget) -> Iterator[tuple[str, ...]]:
    """Hold an exclusive lock on every in-tree authority surface.

    The locks are taken in locator order, so two cutovers racing one tree
    cannot deadlock against each other. They are the reason a fallback
    writer cannot slip a mutation in during the window: that path takes
    the same sibling lock on the same file before it writes.

    Args:
        target: The fence-cleared target tree.

    Yields:
        The locators locked, in the order they were acquired.

    Raises:
        LockTimeout: A surface is held by somebody else.
    """
    surfaces = target.authority_surfaces()
    with ExitStack() as stack:
        for _locator, path in surfaces:
            path.parent.mkdir(parents=True, exist_ok=True)
            stack.enter_context(portalock.acquire(path, timeout=LOCK_TIMEOUT_SECONDS))
        locators = tuple(locator for locator, _path in surfaces)
        logger.info(f"authority_locks root={target.root.name} surfaces={len(locators)}")
        yield locators


@contextmanager
def maintenance_mode(
    target: DisposableTarget, *, entered_at: datetime, holder: str
) -> Iterator[Path]:
    """Declare the tree closed for the duration of the write window.

    The marker is machine-local and transient: it says this machine is
    mid-cutover, which is not a fact a clone should inherit. It is
    removed on the way out whether the window succeeded or failed, so a
    refused apply does not leave a tree that looks closed forever.

    Args:
        target: The fence-cleared target tree.
        entered_at: When the window opened.
        holder: Who opened it.

    Yields:
        The marker's path.

    Raises:
        OSError: The marker could not be written.
    """
    path = target.maintenance_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "entered_at": entered_at.isoformat(),
                "holder": holder,
                "fallback_writers_disabled": True,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def authority_snapshot(
    target: DisposableTarget, *, registry_path: Path, taken_at: datetime
) -> BackupRecord:
    """Pin a digest for every authority surface the cutover could touch.

    Args:
        target: The fence-cleared target tree.
        registry_path: The workspace registry, digested under a logical
            locator so the restore point carries no machine path.
        taken_at: When the surfaces were read.

    Returns:
        The restore point, with one entry per declared surface whether or
        not the file exists.

    Raises:
        OSError: A surface exists but could not be read.
    """
    entries = [*target.authority_surfaces(), (REGISTRY_LOCATOR, registry_path)]
    surfaces = tuple(
        f"{locator}@sha256:{digest_bytes(path.read_bytes())}"
        if path.is_file()
        else f"{locator}@{ABSENT_SURFACE}"
        for locator, path in sorted(entries)
    )
    logger.info(f"authority_snapshot root={target.root.name} surfaces={len(surfaces)}")
    return BackupRecord.of(taken_at=taken_at, surfaces=surfaces)


def _recensus(plan: MigrationPlan, *, snapshot_root: Path) -> SourceCensus:
    """Census the corpus again under the locks and reconcile with the plan.

    Args:
        plan: The plan recomputed under the locks.
        snapshot_root: The staging directory holding the corpus.

    Returns:
        The census taken under the locks.

    Raises:
        MigrationSourceChangedError: The census taken now is not the one
            the plan carries, so the plan describes a corpus other than
            the one on disk.
        MigrationRowValidationError: A source row fails its schema.
    """
    census = SourceCensus.build(SourceSnapshot.read(snapshot_root))
    census.require_importable()
    if census != plan.manifest.source_census:
        raise MigrationSourceChangedError(
            "the census taken under the authority locks does not match the census the "
            f"plan was built from (source digest {plan.manifest.source_digest[:12]})"
        )
    return census


def _require_plan_digest(plan: MigrationPlan, *, expected: str) -> None:
    """Refuse a plan whose approval digest is not the approved one.

    Args:
        plan: The plan recomputed under the locks.
        expected: The digest the operator approved.

    Raises:
        MigrationPlanDigestStaleError: The digests differ.
    """
    if plan.approval_digest != expected:
        raise MigrationPlanDigestStaleError(
            f"the approved plan digest {expected} is not the digest this corpus now "
            f"plans to ({plan.approval_digest}), so the approval is stale"
        )


def require_unresolved_rows_accepted(plan: MigrationPlan, *, accepted: tuple[str, ...]) -> None:
    """Refuse an apply over rows the cutover cannot place, unless named.

    A staged write is a rehearsal into a discardable tree, so it tolerates
    a plan with unresolved rows. An apply does not: a row with no target
    is a row the new generation will not hold, and applying without
    saying so shrinks the corpus silently.

    The escape is not a flag. The operator has to enumerate every
    unresolved address, and the set has to match the plan's exactly --
    a missing address means a row nobody acknowledged, a surplus one
    means an acknowledgement written against a different plan. Either way
    the apply refuses, and the accepted set is recorded in the result and
    the journal so the waiver is part of the answer.

    Args:
        plan: The plan recomputed under the locks.
        accepted: The addresses the operator accepted.

    Raises:
        MigrationPlanNotApplicableError: The plan names unresolved rows
            and ``accepted`` does not enumerate exactly those rows, or
            ``accepted`` names rows a clean plan does not have.
    """
    planned = {row.address for row in plan.manifest.unresolved_rows}
    if not accepted:
        plan.require_applicable()
        return
    surplus = sorted(set(accepted) - planned)
    if surplus:
        raise MigrationPlanNotApplicableError(
            f"the apply accepts {len(surplus)} unresolved rows this plan does not name, "
            f"so the acknowledgement was written against a different plan: "
            f"{', '.join(surplus)}"
        )
    missing = sorted(planned - set(accepted))
    if missing:
        raise MigrationPlanNotApplicableError(
            f"{len(missing)} source rows have no epoch-2 target and the apply does not "
            f"accept them, so the cutover cannot account for them: {', '.join(missing)}"
        )


def _completed_cutover(
    target: DisposableTarget, *, manifest_digest: str
) -> GenerationSelection | None:
    """Return the selection only when this manifest's cutover finished.

    Both halves of the select have to agree, because the window between
    them is exactly the crash a re-run must repair. A tree with the
    pointer in place and no marker is still reading epoch 1, so it has
    work left; treating it as done would strand a selected generation
    nobody reads.

    Args:
        target: The fence-cleared target tree.
        manifest_digest: The manifest the apply is about to build from.

    Returns:
        The selection when the pointer and the marker both name this
        manifest, otherwise ``None``.

    Raises:
        ValidationError: The pointer or the marker is on disk but does
            not satisfy its contract.
    """
    selected = read_selection(target)
    if selected is None or selected.manifest_digest != manifest_digest:
        return None
    marker = read_marker(target)
    if marker is None or marker.manifest_digest != manifest_digest:
        logger.info(
            f"_completed_cutover resuming generation={selected.generation_id} "
            "selected without a marker"
        )
        return None
    return selected


def _commit(
    *,
    target: DisposableTarget,
    request: Epoch2ApplyRequest,
    plan: MigrationPlan,
    journal: CutoverJournal,
    applied_at: datetime,
) -> CutoverResult:
    """Run the durable half of the cutover, journalling ahead of each step.

    Args:
        target: The fence-cleared target tree.
        request: The validated apply request.
        plan: The plan recomputed under the locks.
        journal: The journal holding the precondition rows.
        applied_at: The timestamp every written row records.

    Returns:
        The result of the apply, at the ``marker_written`` boundary.

    Raises:
        MigrationValidationDivergedError: The two staging imports differ.
        MigrationReadSmokeFailedError: The built generation does not read
            back as its manifest describes it.
        OSError: A durable write failed.
    """
    snapshot_root = Path(request.plan_request.snapshot_root)
    allowlist_path = Path(request.plan_request.allowlist_path)
    written = 0
    with maintenance_mode(target, entered_at=applied_at, holder=request.plan_request.sealed_by):
        journal.record(
            stage=CutoverStage.MAINTENANCE_ENTERED,
            boundary=RollbackBoundary.PLAN_ONLY,
            recorded_at=applied_at,
            detail=f"write window opened by {request.plan_request.sealed_by}",
        )
        backup = authority_snapshot(
            target, registry_path=Path(request.registry_path), taken_at=applied_at
        )
        journal.record(
            stage=CutoverStage.SNAPSHOT_TAKEN,
            boundary=RollbackBoundary.PLAN_ONLY,
            recorded_at=applied_at,
            detail=f"pinned {len(backup.surfaces)} authority surfaces",
        )
        written += journal.flush()

        generation_id, manifest = build_generation(
            target=target,
            plan=plan,
            snapshot_root=snapshot_root,
            allowlist_path=allowlist_path,
            recorded_at=applied_at,
        )
        journal.record(
            stage=CutoverStage.GENERATION_BUILT,
            boundary=RollbackBoundary.STAGED,
            recorded_at=applied_at,
            detail=f"{generation_id} built twice and compared byte for byte",
        )
        written += journal.flush()

        records = read_smoke(target=target, generation_id=generation_id, manifest=manifest)
        journal.record(
            stage=CutoverStage.READ_SMOKE_PASSED,
            boundary=RollbackBoundary.STAGED,
            recorded_at=applied_at,
            detail=f"read {records} records back through the public readers",
        )
        journal.record(
            stage=CutoverStage.GENERATION_SELECTED,
            boundary=RollbackBoundary.GENERATION_SELECTED,
            recorded_at=applied_at,
            detail=f"pointing the tree at {generation_id}",
        )
        written += journal.flush()
        select_generation(
            target=target,
            generation_id=generation_id,
            manifest=manifest,
            approval_digest=plan.approval_digest,
            selected_at=applied_at,
        )

        journal.record(
            stage=CutoverStage.MARKER_WRITTEN,
            boundary=RollbackBoundary.MARKER_WRITTEN,
            recorded_at=applied_at,
            detail=f"declaring epoch 2 over {generation_id}",
        )
        written += journal.flush()
        write_marker(
            target=target,
            generation_id=generation_id,
            manifest_digest=manifest.manifest_digest,
            written_at=applied_at,
        )
        final = manifest.advanced(boundary=RollbackBoundary.MARKER_WRITTEN)

    journal.record(
        stage=CutoverStage.MAINTENANCE_EXITED,
        boundary=RollbackBoundary.MARKER_WRITTEN,
        recorded_at=applied_at,
        detail="write window closed",
    )
    written += journal.flush()
    return CutoverResult(
        applied=True,
        generation_id=generation_id,
        manifest_digest=final.manifest_digest,
        approval_digest=plan.approval_digest,
        rollback_boundary=final.rollback_boundary,
        journal_rows=written,
        target_rows=final.target_census.total_rows,
        generation_count=len(generation_ids(target)),
        accepted_unresolved_rows=request.accepted_unresolved_rows,
    )


def apply_cutover(request: Epoch2ApplyRequest, *, applied_at: datetime) -> CutoverResult:
    """Apply one approved cutover plan, or report it is already applied.

    Args:
        request: The validated apply request.
        applied_at: The timestamp every written row records. A parameter
            rather than a clock read, so two applies of one plan differ in
            nothing.

    Returns:
        The result. ``applied=False`` with ``journal_rows=0`` is the
        idempotent case: the approved generation was already selected, so
        the run changed nothing.

    Raises:
        MigrationTargetNotDisposableError: The target has not declared
            itself a disposable canary.
        MigrationWorkspaceNotRegisteredError: The addressing workspace is
            not registered.
        MigrationNotQuiescentError: Something is still holding the tree.
        MigrationPlanDigestStaleError: The approved digest is not the one
            this corpus now plans to.
        MigrationPlanNotApplicableError: The plan names unresolved rows
            the apply does not accept.
        MigrationRuleError: Any other importer rule refused the corpus.
        LockTimeout: An authority surface is held elsewhere.
    """
    target = DisposableTarget.require(Path(request.target_root))
    _require_registered_workspace(request)

    journal = CutoverJournal(target.journal_path)
    journal.record(
        stage=CutoverStage.FENCE_CLEARED,
        boundary=RollbackBoundary.PLAN_ONLY,
        recorded_at=applied_at,
        detail=f"target declared disposable by {target.declaration.declared_by}",
    )
    journal.record(
        stage=CutoverStage.WORKSPACE_RESOLVED,
        boundary=RollbackBoundary.PLAN_ONLY,
        recorded_at=applied_at,
        detail=f"workspace {request.plan_request.workspace_key} is registered",
    )

    with authority_locks(target) as locators:
        journal.record(
            stage=CutoverStage.AUTHORITY_LOCKED,
            boundary=RollbackBoundary.PLAN_ONLY,
            recorded_at=applied_at,
            detail=f"holding {len(locators)} authority surfaces exclusively",
        )
        require_quiescent(quiescence_findings(target))
        journal.record(
            stage=CutoverStage.QUIESCENCE_PROVED,
            boundary=RollbackBoundary.PLAN_ONLY,
            recorded_at=applied_at,
            detail="no session, lease, write-ahead record or worktree is live",
        )
        plan = plan_cutover(request.plan_request, sealed_at=applied_at)
        _require_plan_digest(plan, expected=request.plan_digest)
        census = _recensus(plan, snapshot_root=Path(request.plan_request.snapshot_root))
        journal.record(
            stage=CutoverStage.RECENSUS_MATCHED,
            boundary=RollbackBoundary.PLAN_ONLY,
            recorded_at=applied_at,
            detail=f"re-censused {len(census.collections)} collections under the locks",
        )
        require_unresolved_rows_accepted(plan, accepted=request.accepted_unresolved_rows)

        selected = _completed_cutover(target, manifest_digest=plan.manifest.manifest_digest)
        if selected is not None:
            logger.info(
                f"apply_cutover no-op generation={selected.generation_id} "
                f"manifest_digest={selected.manifest_digest[:12]}"
            )
            return CutoverResult(
                applied=False,
                generation_id=selected.generation_id,
                manifest_digest=selected.manifest_digest,
                approval_digest=selected.approval_digest,
                rollback_boundary=RollbackBoundary.MARKER_WRITTEN,
                journal_rows=0,
                target_rows=plan.manifest.target_census.total_rows,
                generation_count=len(generation_ids(target)),
                accepted_unresolved_rows=request.accepted_unresolved_rows,
            )
        return _commit(
            target=target,
            request=request,
            plan=plan,
            journal=journal,
            applied_at=applied_at,
        )


def apply_envelope(result: CutoverResult) -> dict[str, Any]:
    """Return the envelope one apply result is reported as.

    Args:
        result: What the apply did.

    Returns:
        The JSON-ready envelope.
    """
    return {
        "status": "applied" if result.applied else "already-selected",
        **result.model_dump(mode="json"),
    }


__all__ = [
    "ABSENT_SURFACE",
    "EPOCH2_APPLY_METHOD",
    "LOCK_TIMEOUT_SECONDS",
    "REGISTRY_LOCATOR",
    "CutoverResult",
    "Epoch2ApplyRequest",
    "apply_cutover",
    "apply_envelope",
    "authority_locks",
    "authority_snapshot",
    "maintenance_mode",
    "require_unresolved_rows_accepted",
]
