"""What to do with a tree whose cutover stopped part way through.

An interrupted apply leaves the tree at one of four places, and what may
be done about it is decided by which one rather than by what the operator
would prefer.

**Before the select.** Nothing the tree reads from has changed. The
staging directories and any generation the crash published are discarded
and the failure is journalled. Both verbs do the same thing here, because
there is no activation to complete.

**After the select, before the marker.** The tree has a complete new
generation and is still, correctly, epoch 1. ``recover`` reads the
generation back and finishes the activation; if it does not read back,
``recover`` restores the full surface set instead. ``rollback`` restores.

**After the marker, before the first native mutation.** The activation is
complete. ``recover`` closes the write window the crash left open.
``rollback`` can still put the tree back, because nothing has been written
against the new generation yet.

**After the first native mutation.** The marker pins what the published
generation digested to at activation, so a generation whose bytes have
moved has accepted a write natively. Restoring epoch 1 would discard work
recorded nowhere else, which is an incident and an operator decision
rather than an automated repair: ``rollback`` refuses with
``rollback_boundary_crossed`` and writes nothing at all -- not even the
journal row that would say it refused. ``recover`` has nothing to do and
says so.

The boundary is read from the **tree**, never from the journal. The
journal is flushed ahead of each durable act precisely so it never
under-reports, which means it routinely claims one stage more than the
tree has: a crash between the flushed ``generation_selected`` row and the
pointer being replaced leaves a journal that says selected and a tree that
is not. Taking the journal's word there would complete an activation over
a pointer nobody wrote.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import Field

from eawf.kernel.migration.epoch2.apply import authority_locks
from eawf.kernel.migration.epoch2.canary import MAINTENANCE_LOCATOR, DisposableTarget
from eawf.kernel.migration.epoch2.errors import (
    MigrationDualAuthorityError,
    MigrationReadSmokeFailedError,
    MigrationRestorePointMissingError,
    MigrationRollbackBoundaryCrossedError,
)
from eawf.kernel.migration.epoch2.generation import (
    EpochMarker,
    GenerationSelection,
    clear_activation,
    discard_generation,
    discard_staging,
    generation_digest,
    generation_ids,
    read_marker,
    read_selection,
    staging_directories,
    verify_selected_generation,
    write_marker,
)
from eawf.kernel.migration.epoch2.journal import (
    CutoverJournal,
    CutoverStage,
    read_journal,
    require_chain_intact,
)
from eawf.kernel.migration.epoch2.manifest import RollbackBoundary
from eawf.kernel.migration.epoch2.quiescence import quiescence_findings, require_quiescent
from eawf.kernel.migration.epoch2.restore import (
    RestoreManifest,
    read_restore_manifest,
    restore_full_set,
    verify_restore_point,
)
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)


#: The JSON-RPC method both recovery verbs are served under. One method
#: rather than two, because they are one decision table read from opposite
#: ends: the action picks which column, the tree picks which row.
EPOCH2_RECOVER_METHOD: Final = "migration.epoch2.recover"

#: The boundaries in the order a cutover passes through them, so "how far
#: did the journal claim" is a comparison rather than a guess.
BOUNDARY_ORDER: Final[tuple[RollbackBoundary, ...]] = (
    RollbackBoundary.PLAN_ONLY,
    RollbackBoundary.STAGED,
    RollbackBoundary.GENERATION_SELECTED,
    RollbackBoundary.MARKER_WRITTEN,
    RollbackBoundary.CROSSED,
)

#: The boundaries at which nothing the tree reads from has moved yet, so
#: the recovery is a discard rather than a restore.
DISCARDABLE_BOUNDARIES: Final[tuple[RollbackBoundary, ...]] = (
    RollbackBoundary.PLAN_ONLY,
    RollbackBoundary.STAGED,
)


class RecoveryAction(StrEnum):
    """Which direction the operator asked the tree to be taken in."""

    RECOVER = "recover"
    ROLLBACK = "rollback"


class RecoveryOutcome(StrEnum):
    """What the verb actually did."""

    STAGING_DISCARDED = "staging_discarded"
    SURFACES_RESTORED = "surfaces_restored"
    ACTIVATION_COMPLETED = "activation_completed"
    WINDOW_CLOSED = "window_closed"
    NOTHING_TO_RECOVER = "nothing_to_recover"


class Epoch2RecoverRequest(StrictMigrationModel):
    """One request to recover or roll back an interrupted cutover.

    Attributes:
        target_root: The tree to recover. It must still declare itself a
            disposable canary: a recovery writes the same files an apply
            does, so it clears the same fence.
        action: Which direction to take the tree in.
        manifest_path: The restore manifest to write back. Omitted means
            the one the apply left inside the tree, which is the normal
            case; an explicit path is how an operator recovers from a
            restore point they have kept elsewhere.
    """

    target_root: Annotated[str, Field(min_length=1)]
    action: RecoveryAction
    manifest_path: Annotated[str, Field(min_length=1)] | None = None


class TreeAuthority(StrictMigrationModel):
    """The one thing a recovered tree reads from.

    Attributes:
        epoch: ``1`` when the tree reads its own document, ``2`` when it
            reads a generation.
        generation_id: The generation it reads from, or ``None`` at epoch
            1.
        generation_count: How many generations are on disk. At epoch 1 a
            recovered tree carries none: a generation nothing selects is
            the orphan a recovery exists to remove.
    """

    epoch: Literal[1, 2]
    generation_id: str | None
    generation_count: Annotated[int, Field(ge=0)]


class RecoveryAssessment(StrictMigrationModel):
    """Where one interrupted cutover actually got to.

    Attributes:
        boundary: How far the tree went, read from the tree itself.
        journal_boundary: How far the journal claims the cutover went.
            Equal to or one stage ahead of ``boundary``, because the
            journal is flushed before each durable act.
        stage: The last stage the journal committed, or ``None`` when the
            tree carries no journal.
        selected_generation_id: The generation the pointer names.
        marked_generation_id: The generation the marker names.
        staging_directories: Staging directories a crashed build left.
        maintenance_held: Whether the write window is still declared open.
        refusal_code: The code a rollback would refuse with, or ``None``
            when one is still possible.
    """

    boundary: RollbackBoundary
    journal_boundary: RollbackBoundary
    stage: CutoverStage | None
    selected_generation_id: str | None
    marked_generation_id: str | None
    staging_directories: tuple[str, ...]
    maintenance_held: bool
    refusal_code: str | None


class RecoveryResult(StrictMigrationModel):
    """What one recovery did, and what the tree reads from afterwards.

    Attributes:
        action: Which verb ran.
        outcome: What it did.
        boundary: The boundary the interrupted cutover had reached.
        authority: The single authority the tree has afterwards.
        restored_locators: The authority surfaces that were written back
            or removed.
        discarded: The staging directories, generations and markers that
            were removed.
        stage: The journal stage this recovery recorded, or ``None`` when
            it recovered nothing and therefore wrote nothing.
        journal_rows: How many rows it appended.
        manifest_digest: The cutover manifest the restore point belongs
            to, when one was read.
        idempotence_digest: What a re-apply of the same plan must
            reproduce, when a restore point was read. This is the value a
            restore-then-reapply is checked against.
    """

    action: RecoveryAction
    outcome: RecoveryOutcome
    boundary: RollbackBoundary
    authority: TreeAuthority
    restored_locators: tuple[str, ...]
    discarded: tuple[str, ...]
    stage: CutoverStage | None
    journal_rows: Annotated[int, Field(ge=0)]
    manifest_digest: str | None
    idempotence_digest: str | None


def assess_recovery(target: DisposableTarget) -> RecoveryAssessment:
    """Return where one interrupted cutover got to, reading the tree first.

    Args:
        target: The fence-cleared target tree.

    Returns:
        The assessment. ``refusal_code`` is set when the published
        generation's bytes have moved since activation, which is the one
        state no rollback may act on.

    Raises:
        MigrationDualAuthorityError: The pointer and the marker do not
            agree on one generation that is on disk.
        MigrationJournalBrokenError: The journal does not parse or does not
            chain.
        OSError: A file could not be read.
    """
    rows = read_journal(target.journal_path)
    require_chain_intact(rows)
    selection = read_selection(target)
    marker = read_marker(target)
    built = generation_ids(target)
    _require_consistent_pair(selection=selection, marker=marker, built=built)
    boundary, refusal_code = _tree_boundary(target, selection=selection, marker=marker, built=built)
    journal_boundary = max(
        (row.boundary for row in rows), key=BOUNDARY_ORDER.index, default=RollbackBoundary.PLAN_ONLY
    )
    assessment = RecoveryAssessment(
        boundary=boundary,
        journal_boundary=journal_boundary,
        stage=rows[-1].stage if rows else None,
        selected_generation_id=None if selection is None else selection.generation_id,
        marked_generation_id=None if marker is None else marker.generation_id,
        staging_directories=staging_directories(target),
        maintenance_held=target.maintenance_path.exists(),
        refusal_code=refusal_code,
    )
    logger.info(
        f"assess_recovery root={target.root.name} boundary={boundary.value} "
        f"journal_boundary={journal_boundary.value}"
    )
    return assessment


def _tree_boundary(
    target: DisposableTarget,
    *,
    selection: GenerationSelection | None,
    marker: EpochMarker | None,
    built: tuple[str, ...],
) -> tuple[RollbackBoundary, str | None]:
    """Return the boundary the tree is at, and any code that forbids rollback.

    Args:
        target: The fence-cleared target tree.
        selection: The selection pointer, when there is one.
        marker: The epoch marker, when there is one.
        built: Every generation on disk.

    Returns:
        ``(boundary, refusal_code)``.

    Raises:
        MigrationReadSmokeFailedError: The marker names a generation that
            cannot be digested.
        OSError: A generation file could not be read.
    """
    if marker is not None:
        published = generation_digest(target, generation_id=marker.generation_id)
        if published != marker.generation_digest:
            return RollbackBoundary.CROSSED, MigrationRollbackBoundaryCrossedError.code
        return RollbackBoundary.MARKER_WRITTEN, None
    if selection is not None:
        return RollbackBoundary.GENERATION_SELECTED, None
    return (RollbackBoundary.STAGED if built else RollbackBoundary.PLAN_ONLY), None


def _require_consistent_pair(
    *,
    selection: GenerationSelection | None,
    marker: EpochMarker | None,
    built: tuple[str, ...],
) -> None:
    """Refuse a tree whose two select halves do not name one built generation.

    Args:
        selection: The selection pointer, when there is one.
        marker: The epoch marker, when there is one.
        built: Every generation on disk.

    Raises:
        MigrationDualAuthorityError: The marker has no pointer, the two
            disagree, or the generation they name is not on disk. The apply
            writes the pointer before the marker, so none of these can be
            produced by an interruption -- they are a tree somebody else
            edited, and guessing which half is the truth is how a recovery
            destroys the half that was right.
    """
    if marker is not None and selection is None:
        raise MigrationDualAuthorityError(
            f"the tree is marked epoch 2 over {marker.generation_id} but no selection "
            "pointer names a generation, so nothing can say what it reads from"
        )
    if (
        marker is not None
        and selection is not None
        and marker.generation_id != selection.generation_id
    ):
        raise MigrationDualAuthorityError(
            f"the epoch marker names {marker.generation_id} and the selection pointer "
            f"names {selection.generation_id}, so the tree claims two authorities"
        )
    if selection is not None and selection.generation_id not in built:
        raise MigrationDualAuthorityError(
            f"the selection pointer names {selection.generation_id}, which is not a "
            f"generation on disk (found {len(built)})"
        )


def require_single_authority(target: DisposableTarget) -> TreeAuthority:
    """Return the one authority a recovered tree reads from, or refuse.

    Args:
        target: The fence-cleared target tree.

    Returns:
        The authority. Epoch 1 with no generation on disk, or epoch 2 over
        the one generation both halves of the select name.

    Raises:
        MigrationDualAuthorityError: A staging directory survived, an
            orphan generation nothing selects survived, or the two select
            halves disagree. Each one means the recovery left the tree with
            more than one candidate answer, which is the failure this check
            exists to catch.
        OSError: A file could not be read.
    """
    selection = read_selection(target)
    marker = read_marker(target)
    built = generation_ids(target)
    _require_consistent_pair(selection=selection, marker=marker, built=built)
    staging = staging_directories(target)
    if staging:
        raise MigrationDualAuthorityError(
            f"{len(staging)} staging directories survived the recovery, so a later build "
            f"would compare against a tree nobody published: {', '.join(staging)}"
        )
    if marker is None:
        if built:
            raise MigrationDualAuthorityError(
                f"the tree reads epoch 1 but carries {len(built)} generation(s) nothing "
                f"selects, so the next apply would meet an orphan: {', '.join(built)}"
            )
        return TreeAuthority(epoch=1, generation_id=None, generation_count=0)
    return TreeAuthority(epoch=2, generation_id=marker.generation_id, generation_count=len(built))


def recover_cutover(request: Epoch2RecoverRequest, *, recovered_at: datetime) -> RecoveryResult:
    """Take one interrupted cutover forward or back, under the authority locks.

    Args:
        request: The validated recovery request.
        recovered_at: The timestamp every written row records. A parameter
            rather than a clock read, so two recoveries of one tree differ
            in nothing.

    Returns:
        What the recovery did, and the single authority the tree has
        afterwards.

    Raises:
        MigrationTargetNotDisposableError: The tree has not declared itself
            a disposable canary.
        MigrationNotQuiescentError: Something is still holding the tree. A
            recovery rewrites authority surfaces, so it refuses to race a
            live holder exactly as the apply does.
        MigrationRollbackBoundaryCrossedError: A rollback was asked for
            after the published generation accepted a write.
        MigrationRestorePointMissingError: A restore is needed and the tree
            pinned no usable restore point.
        MigrationRestoreIncompleteError: The restore point cannot put the
            whole surface set back, so none of it is written.
        MigrationDualAuthorityError: The tree names two authorities, before
            or after the recovery.
        LockTimeout: An authority surface is held elsewhere.
    """
    target = DisposableTarget.require(Path(request.target_root))
    with authority_locks(target):
        require_quiescent(quiescence_findings(target))
        assessment = assess_recovery(target)
        if (
            request.action is RecoveryAction.ROLLBACK
            and assessment.boundary is RollbackBoundary.CROSSED
        ):
            raise MigrationRollbackBoundaryCrossedError(
                f"{assessment.marked_generation_id} has accepted a mutation since it was "
                "activated, so restoring epoch 1 would discard work recorded nowhere "
                "else; this is an incident and an operator decision, and the rollback "
                "writes nothing"
            )
        journal = CutoverJournal(target.journal_path)
        restore = _restore_point(request, target=target, assessment=assessment)
        before = len(journal.committed_rows())
        outcome, stage, restored, discarded = _run_action(
            target=target,
            assessment=assessment,
            action=request.action,
            restore=restore,
            journal=journal,
            recovered_at=recovered_at,
        )
        written = len(journal.committed_rows()) - before
        authority = require_single_authority(target)
    logger.info(
        f"recover_cutover root={target.root.name} action={request.action.value} "
        f"outcome={outcome.value} epoch={authority.epoch}"
    )
    return RecoveryResult(
        action=request.action,
        outcome=outcome,
        boundary=assessment.boundary,
        authority=authority,
        restored_locators=restored,
        discarded=discarded,
        stage=stage,
        journal_rows=written,
        manifest_digest=None if restore is None else restore.manifest_digest,
        idempotence_digest=None if restore is None else restore.idempotence_digest,
    )


def _restore_point(
    request: Epoch2RecoverRequest,
    *,
    target: DisposableTarget,
    assessment: RecoveryAssessment,
) -> RestoreManifest | None:
    """Return the restore point this recovery will write back, if it needs one.

    A discard needs none: nothing the tree reads from has moved, so there
    is nothing to write back, and a tree that crashed before it sealed its
    restore point is still recoverable that way.

    Args:
        request: The validated recovery request.
        target: The fence-cleared target tree.
        assessment: Where the interrupted cutover got to.

    Returns:
        The manifest, or ``None`` when the boundary needs no restore.

    Raises:
        MigrationRestorePointMissingError: The manifest is absent or
            unusable, or it belongs to a cutover other than the one this
            tree is carrying.
    """
    if assessment.boundary in DISCARDABLE_BOUNDARIES:
        return None
    path = (
        Path(request.manifest_path)
        if request.manifest_path is not None
        else target.restore_manifest_path
    )
    manifest = read_restore_manifest(path)
    carried = assessment.marked_generation_id or assessment.selected_generation_id
    if manifest.generation_id != carried:
        raise MigrationRestorePointMissingError(
            f"{path.name} is the restore point for {manifest.generation_id}, but this tree "
            f"carries {carried}, so it pins no bytes this cutover could be put back to"
        )
    return manifest


def _run_action(
    *,
    target: DisposableTarget,
    assessment: RecoveryAssessment,
    action: RecoveryAction,
    restore: RestoreManifest | None,
    journal: CutoverJournal,
    recovered_at: datetime,
) -> tuple[RecoveryOutcome, CutoverStage | None, tuple[str, ...], tuple[str, ...]]:
    """Run the one arm of the table the action and the boundary select.

    Args:
        target: The fence-cleared target tree.
        assessment: Where the interrupted cutover got to.
        action: Which direction to take the tree in.
        restore: The restore point, when the boundary needs one.
        journal: The journal, flushed by the caller once the arm returns.
        recovered_at: The timestamp every written row records.

    Returns:
        ``(outcome, stage, restored_locators, discarded)``.

    Raises:
        MigrationRestoreIncompleteError: The restore point cannot put the
            whole surface set back.
        OSError: A durable write failed.
    """
    if assessment.boundary in DISCARDABLE_BOUNDARIES:
        return _discard_arm(
            target=target, assessment=assessment, journal=journal, recovered_at=recovered_at
        )
    if action is RecoveryAction.ROLLBACK:
        return _restore_arm(
            target=target,
            assessment=assessment,
            restore=_require_restore(restore),
            journal=journal,
            recovered_at=recovered_at,
        )
    if assessment.boundary is RollbackBoundary.GENERATION_SELECTED:
        return _complete_arm(
            target=target,
            assessment=assessment,
            restore=_require_restore(restore),
            journal=journal,
            recovered_at=recovered_at,
        )
    return _close_arm(
        target=target, assessment=assessment, journal=journal, recovered_at=recovered_at
    )


def _require_restore(restore: RestoreManifest | None) -> RestoreManifest:
    """Return the restore point, refusing the arm that cannot run without one.

    Args:
        restore: The manifest the caller resolved.

    Returns:
        The manifest.

    Raises:
        MigrationRestorePointMissingError: There is none. Reaching this
            means a boundary past the select resolved no restore point,
            which :func:`_restore_point` refuses first -- so it is the
            guard that keeps a later arm from treating ``None`` as "no
            surfaces to put back".
    """
    if restore is None:
        raise MigrationRestorePointMissingError(
            "this recovery needs a restore point and none was resolved, so no surface can "
            "be written back"
        )
    return restore


def _discard_arm(
    *,
    target: DisposableTarget,
    assessment: RecoveryAssessment,
    journal: CutoverJournal,
    recovered_at: datetime,
) -> tuple[RecoveryOutcome, CutoverStage | None, tuple[str, ...], tuple[str, ...]]:
    """Throw away everything an interrupted pre-select cutover built.

    Nothing is selected at this boundary, so every generation on disk is an
    orphan of the interrupted run: a finished earlier cutover would have
    left a pointer and a marker, which would have put the tree at a later
    boundary entirely.

    Args:
        target: The fence-cleared target tree.
        assessment: Where the interrupted cutover got to.
        journal: The journal, flushed by the caller.
        recovered_at: The timestamp every written row records.

    Returns:
        ``(outcome, stage, restored_locators, discarded)``.

    Raises:
        OSError: A directory could not be removed.
    """
    orphans = generation_ids(target)
    planned = (*assessment.staging_directories, *orphans)
    journal.record(
        stage=CutoverStage.ROLLBACK_DISCARDED,
        boundary=RollbackBoundary.PLAN_ONLY,
        recorded_at=recovered_at,
        detail=(
            f"discarding {len(assessment.staging_directories)} staging directories and "
            f"{len(orphans)} unselected generations"
        ),
    )
    journal.flush()
    for name in assessment.staging_directories:
        discard_staging(target, name=name)
    for generation_id in orphans:
        discard_generation(target, generation_id=generation_id)
    discarded = (*planned, *_close_window(target, assessment=assessment))
    return RecoveryOutcome.STAGING_DISCARDED, CutoverStage.ROLLBACK_DISCARDED, (), discarded


def _restore_arm(
    *,
    target: DisposableTarget,
    assessment: RecoveryAssessment,
    restore: RestoreManifest,
    journal: CutoverJournal,
    recovered_at: datetime,
) -> tuple[RecoveryOutcome, CutoverStage | None, tuple[str, ...], tuple[str, ...]]:
    """Put the tree back to the corpus the restore point was taken over.

    The order is de-activate, then restore, then delete: the marker and the
    pointer go first so the tree never names bytes that are gone, and the
    generation is removed last.

    Args:
        target: The fence-cleared target tree.
        assessment: Where the interrupted cutover got to.
        restore: The restore point to write back.
        journal: The journal, flushed by the caller.
        recovered_at: The timestamp every written row records.

    Returns:
        ``(outcome, stage, restored_locators, discarded)``.

    Raises:
        MigrationRestoreIncompleteError: The point cannot put the whole
            surface set back, in which case nothing is written.
        OSError: A durable write failed.
    """
    planned = verify_restore_point(target=target, manifest=restore)
    journal.record(
        stage=CutoverStage.RESTORE_VERIFIED,
        boundary=assessment.boundary,
        recorded_at=recovered_at,
        detail=f"verified {len(restore.restorable_surfaces())} surfaces against the restore point",
    )
    journal.record(
        stage=CutoverStage.SURFACES_RESTORED,
        boundary=RollbackBoundary.PLAN_ONLY,
        recorded_at=recovered_at,
        detail=f"restoring {len(planned)} surfaces and dropping {restore.generation_id}",
    )
    journal.flush()
    discarded = clear_activation(target)
    restored = restore_full_set(target=target, manifest=restore)
    for name in assessment.staging_directories:
        discard_staging(target, name=name)
    discard_generation(target, generation_id=restore.generation_id)
    dropped = (
        *discarded,
        *assessment.staging_directories,
        restore.generation_id,
        *_close_window(target, assessment=assessment),
    )
    return RecoveryOutcome.SURFACES_RESTORED, CutoverStage.SURFACES_RESTORED, restored, dropped


def _complete_arm(
    *,
    target: DisposableTarget,
    assessment: RecoveryAssessment,
    restore: RestoreManifest,
    journal: CutoverJournal,
    recovered_at: datetime,
) -> tuple[RecoveryOutcome, CutoverStage | None, tuple[str, ...], tuple[str, ...]]:
    """Finish the activation the crash interrupted, or restore instead.

    The only write is the marker. The generation is not rebuilt and not
    republished, so a recovery cannot produce a second copy of a tree that
    is already on disk.

    Args:
        target: The fence-cleared target tree.
        assessment: Where the interrupted cutover got to.
        restore: The restore point, used only if the generation does not
            read back.
        journal: The journal, flushed by the caller.
        recovered_at: The timestamp every written row records.

    Returns:
        ``(outcome, stage, restored_locators, discarded)``.

    Raises:
        MigrationRestoreIncompleteError: The generation does not read back
            and the restore point cannot put the surfaces back either.
        OSError: A durable write failed.
    """
    selection = read_selection(target)
    if selection is None:
        raise MigrationDualAuthorityError(
            "the tree is past the select but carries no selection pointer, so there is no "
            "generation to activate"
        )
    generation_id = selection.generation_id
    try:
        records = verify_selected_generation(target, generation_id=generation_id)
    except MigrationReadSmokeFailedError as error:
        logger.warning(f"_complete_arm restoring generation={generation_id} reason={error}")
        return _restore_arm(
            target=target,
            assessment=assessment,
            restore=restore,
            journal=journal,
            recovered_at=recovered_at,
        )
    published_digest = generation_digest(target, generation_id=generation_id)
    journal.record(
        stage=CutoverStage.READ_SMOKE_PASSED,
        boundary=RollbackBoundary.GENERATION_SELECTED,
        recorded_at=recovered_at,
        detail=f"re-read {records} records from the selected generation",
    )
    journal.record(
        stage=CutoverStage.ACTIVATION_COMPLETED,
        boundary=RollbackBoundary.MARKER_WRITTEN,
        recorded_at=recovered_at,
        detail=f"completing the activation of {generation_id}",
    )
    journal.flush()
    write_marker(
        target=target,
        generation_id=generation_id,
        manifest_digest=selection.manifest_digest,
        published_digest=published_digest,
        written_at=recovered_at,
    )
    discarded = _close_window(target, assessment=assessment)
    return RecoveryOutcome.ACTIVATION_COMPLETED, CutoverStage.ACTIVATION_COMPLETED, (), discarded


def _close_arm(
    *,
    target: DisposableTarget,
    assessment: RecoveryAssessment,
    journal: CutoverJournal,
    recovered_at: datetime,
) -> tuple[RecoveryOutcome, CutoverStage | None, tuple[str, ...], tuple[str, ...]]:
    """Close a write window a finished activation left open.

    A cutover that died after the marker is complete; what it may still be
    missing is the closing bookkeeping. When that is already there too, the
    verb writes nothing at all rather than appending a row claiming it
    recovered something.

    Args:
        target: The fence-cleared target tree.
        assessment: Where the interrupted cutover got to.
        journal: The journal, flushed by the caller.
        recovered_at: The timestamp every written row records.

    Returns:
        ``(outcome, stage, restored_locators, discarded)``.

    Raises:
        OSError: A durable write failed.
    """
    settled = (
        assessment.stage is CutoverStage.MAINTENANCE_EXITED
        and not assessment.maintenance_held
        and not assessment.staging_directories
    )
    if settled:
        return RecoveryOutcome.NOTHING_TO_RECOVER, None, (), ()
    journal.record(
        stage=CutoverStage.MAINTENANCE_EXITED,
        boundary=assessment.boundary,
        recorded_at=recovered_at,
        detail="write window closed by the recovery",
    )
    journal.flush()
    for name in assessment.staging_directories:
        discard_staging(target, name=name)
    discarded = (*assessment.staging_directories, *_close_window(target, assessment=assessment))
    return RecoveryOutcome.WINDOW_CLOSED, CutoverStage.MAINTENANCE_EXITED, (), discarded


def _close_window(target: DisposableTarget, *, assessment: RecoveryAssessment) -> tuple[str, ...]:
    """Remove the maintenance marker a crashed write window left behind.

    Args:
        target: The fence-cleared target tree.
        assessment: Where the interrupted cutover got to.

    Returns:
        The locator that was removed, or an empty tuple when the window was
        already closed.

    Raises:
        OSError: The marker could not be removed.
    """
    if not assessment.maintenance_held:
        return ()
    target.maintenance_path.unlink(missing_ok=True)
    return (MAINTENANCE_LOCATOR,)


def recovery_envelope(result: RecoveryResult) -> dict[str, Any]:
    """Return the envelope one recovery result is reported as.

    Args:
        result: What the recovery did.

    Returns:
        The JSON-ready envelope.
    """
    return {"status": result.outcome.value, **result.model_dump(mode="json")}


__all__ = [
    "BOUNDARY_ORDER",
    "DISCARDABLE_BOUNDARIES",
    "EPOCH2_RECOVER_METHOD",
    "Epoch2RecoverRequest",
    "RecoveryAction",
    "RecoveryAssessment",
    "RecoveryOutcome",
    "RecoveryResult",
    "TreeAuthority",
    "assess_recovery",
    "recover_cutover",
    "recovery_envelope",
    "require_single_authority",
]
