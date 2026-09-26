"""Sealing an activation at its first native mutation, and the two windows.

A cutover leaves a tree in epoch 2 with its restore point intact, and for
as long as nothing has been written natively the tree can be put back byte
for byte. The first native mutation ends that: from then on the published
generation holds work recorded nowhere else. This module turns that moment
into a durable fact. When a native session closes and the published
generation no longer digests to the value its marker pinned, the journal
gains one ``activation_completed`` row carrying an
:class:`~eawf.kernel.migration.epoch2.journal.ActivationRecord`, and every
later rollback refuses with ``rollback_boundary_crossed``.

It also answers the question an operator of an opted-in repository keeps
asking: can this still be undone, and how. There are two windows and they
are never merged. The **simple rollback window** is the restore window: it
is open until the first native mutation and closed after it. The **canary
reversible window** belongs only to an opted-in repository; it opens at the
opt-in and closes only once a whole milestone has run on epoch-2 authority
and a migration re-run reproduces the manifest byte for byte. While it is
open the boundary stays queryable, and after the first native mutation the
answer it gives is forward repair rather than restore. The window closes
when its closing event is journaled as a ``canary_window_closed`` row.

A seal that fails does not pass silently: the daemon records the fault
under the tree's machine-local directory, and the boundary report names it
until a later session's seal lands.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field

from eawf.kernel.migration.epoch2.canary import DeclarationKind, DisposableTarget
from eawf.kernel.migration.epoch2.generation import generation_digest, read_marker
from eawf.kernel.migration.epoch2.journal import (
    ActivationRecord,
    CutoverJournal,
    CutoverJournalRow,
    CutoverStage,
    read_journal,
    require_chain_intact,
    sealed_activation,
)
from eawf.kernel.migration.epoch2.manifest import RollbackBoundary
from eawf.kernel.migration.epoch2.recovery import assess_recovery
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)


#: What closes the simple rollback window.
SIMPLE_WINDOW_CLOSES_ON: Final = "the first native mutation against the activated generation"

#: The machine-local record of a seal that failed after its mutation
#: committed. Machine-local because the fault is one daemon's run, not a
#: fact a clone should inherit.
SEAL_FAULT_LOCATOR: Final = "local/epoch2-activation-seal-fault.json"

#: What closes the canary reversible window of an opted-in repository.
CANARY_WINDOW_CLOSES_ON: Final = (
    "one complete milestone executed on epoch-2 authority plus a byte-identical migration re-run"
)


class RollbackRemedy(StrEnum):
    """How a tree would be taken back from where it stands.

    Attributes:
        RESTORE: Write the restore point back; nothing native is lost.
        FORWARD_REPAIR: Repair forward through the daemon, because a
            restore would discard work the generation alone holds.
    """

    RESTORE = "restore"
    FORWARD_REPAIR = "forward_repair"


class WindowState(StrEnum):
    """Whether one window is open, closed, or does not apply to the tree."""

    OPEN = "open"
    CLOSED = "closed"
    NOT_APPLICABLE = "not_applicable"


class SimpleRollbackWindow(StrictMigrationModel):
    """The restore window, open until the first native mutation.

    Attributes:
        state: ``open`` before the first native mutation, ``closed`` after.
        remedy: ``restore`` while open, ``forward_repair`` once closed.
        closes_on: What closes the window.
        closed_at: When the first native mutation was sealed, or ``None``
            while the window is open or when the crossing was seen only in
            the generation's bytes.
    """

    state: WindowState
    remedy: RollbackRemedy
    closes_on: str
    closed_at: datetime | None


class CanaryReversibleWindow(StrictMigrationModel):
    """The opted-in repository's reversible window.

    Attributes:
        state: ``open`` for an opted-in tree until its closing event is
            journaled, ``closed`` after, ``not_applicable`` for a
            disposable canary, which has no such window.
        remedy: How the tree would be taken back today, or ``None`` when
            the window does not apply.
        closes_on: What closes the window.
        closed_at: When the closing event was journaled, or ``None``
            while the window is open or does not apply.
    """

    state: WindowState
    remedy: RollbackRemedy | None
    closes_on: str
    closed_at: datetime | None


class ActivationSealFault(StrictMigrationModel):
    """A seal that failed after the native mutation it would record committed.

    Attributes:
        code: Always ``activation_seal_failed``.
        observed_at: When the failing seal was attempted.
        error: The class name of the error the seal raised.
    """

    code: Literal["activation_seal_failed"] = "activation_seal_failed"
    observed_at: datetime
    error: Annotated[str, Field(min_length=1, max_length=120)]


class RollbackBoundaryReport(StrictMigrationModel):
    """Where one tree stands against both rollback windows.

    Attributes:
        declaration: Which declaration admitted the tree.
        boundary: How far the cutover has gone, read from the tree.
        generation_id: The generation the tree is marked for, if any.
        simple_rollback_window: The restore window.
        canary_reversible_window: The opted-in repository's window,
            reported separately and never folded into the simple one.
        activation: The activation seal, once the first native mutation
            has landed.
        seal_fault: The recorded fault of a seal that failed and has not
            landed since, or ``None``.
    """

    declaration: DeclarationKind
    boundary: RollbackBoundary
    generation_id: Annotated[str, Field(min_length=1, max_length=64)] | None
    simple_rollback_window: SimpleRollbackWindow
    canary_reversible_window: CanaryReversibleWindow
    activation: ActivationRecord | None
    seal_fault: ActivationSealFault | None


def seal_fault_path(target: DisposableTarget) -> Path:
    """Return where *target*'s activation seal fault is recorded.

    Args:
        target: The fence-cleared target tree.

    Returns:
        The machine-local fault record's path.
    """
    return target.root / SEAL_FAULT_LOCATOR


def read_seal_fault(target: DisposableTarget) -> ActivationSealFault | None:
    """Return the seal fault recorded for *target*, if any.

    Args:
        target: The fence-cleared target tree.

    Returns:
        The recorded fault, or ``None`` when no seal has failed since the
        last one landed.

    Raises:
        pydantic.ValidationError: The record does not parse.
        OSError: The record exists but cannot be read.
    """
    path = seal_fault_path(target)
    if not path.is_file():
        return None
    return ActivationSealFault.model_validate_json(path.read_bytes())


def seal_first_native_mutation(
    target: DisposableTarget, *, observed_at: datetime
) -> ActivationRecord | None:
    """Seal the activation when the first native mutation has committed.

    Called as a native session closes, with the session's locks still
    held, so two sessions cannot both find the tree unsealed and both seal
    it.

    Args:
        target: The fence-cleared tree the session wrote through.
        observed_at: When the mutation was observed committed.

    Returns:
        The record this call sealed, or ``None`` when there was nothing to
        seal: the tree carries no cutover journal (a canary born in epoch 2
        was never cut over, so it has no restore window to close), the
        activation is already sealed, the tree is not marked, or the
        generation still digests to its activation value.

    Raises:
        MigrationJournalBrokenError: The journal does not parse or chain.
        MigrationReadSmokeFailedError: The marked generation is not on disk.
        LedgerAppendOnlyError: Another writer appended underneath the seal.
        OSError: A file could not be read or written.
    """
    journal = CutoverJournal(target.journal_path)
    rows = journal.committed_rows()
    if not rows or sealed_activation(rows) is not None:
        return None
    marker = read_marker(target)
    if marker is None:
        return None
    published = generation_digest(target, generation_id=marker.generation_id)
    if published == marker.generation_digest:
        return None
    record = ActivationRecord(
        generation_id=marker.generation_id,
        manifest_digest=marker.manifest_digest,
        authority_marker_at=marker.written_at,
        first_native_mutation_at=observed_at,
        journal_cursor=len(rows),
        effective_epoch=2,
    )
    journal.record(
        stage=CutoverStage.ACTIVATION_COMPLETED,
        boundary=RollbackBoundary.CROSSED,
        recorded_at=observed_at,
        detail=f"first native mutation sealed the activation of {marker.generation_id}",
        activation=record,
    )
    journal.flush()
    logger.info(
        f"seal_first_native_mutation root={target.root.name} generation={marker.generation_id}"
    )
    return record


def rollback_boundary_report(target: DisposableTarget) -> RollbackBoundaryReport:
    """Report where a tree stands against both rollback windows.

    The report writes nothing: it reads the tree and the journal the same
    way a recovery would before it acted.

    Args:
        target: The fence-cleared target tree.

    Returns:
        The report, with the two windows as separate fields.

    Raises:
        MigrationDualAuthorityError: The pointer and the marker disagree.
        MigrationJournalBrokenError: The journal does not parse or chain.
        OSError: A file could not be read.
    """
    assessment = assess_recovery(target)
    rows = read_journal(target.journal_path)
    require_chain_intact(rows)
    activation = sealed_activation(rows)
    crossed = assessment.boundary is RollbackBoundary.CROSSED
    remedy = RollbackRemedy.FORWARD_REPAIR if crossed else RollbackRemedy.RESTORE
    simple = SimpleRollbackWindow(
        state=WindowState.CLOSED if crossed else WindowState.OPEN,
        remedy=remedy,
        closes_on=SIMPLE_WINDOW_CLOSES_ON,
        closed_at=activation.first_native_mutation_at if activation is not None else None,
    )
    canary = _canary_window(target, rows=rows, remedy=remedy)
    return RollbackBoundaryReport(
        declaration=target.kind,
        boundary=assessment.boundary,
        generation_id=assessment.marked_generation_id,
        simple_rollback_window=simple,
        canary_reversible_window=canary,
        activation=activation,
        seal_fault=read_seal_fault(target),
    )


def _canary_window(
    target: DisposableTarget, *, rows: Sequence[CutoverJournalRow], remedy: RollbackRemedy
) -> CanaryReversibleWindow:
    """Return the canary reversible window as the journal leaves it.

    Args:
        target: The fence-cleared target tree.
        rows: The journal's rows, in file order.
        remedy: How the tree would be taken back while the window is open.

    Returns:
        ``not_applicable`` for a disposable canary; ``closed`` once the
        journal carries the closing row, after which only forward repair
        remains; ``open`` otherwise.
    """
    if target.kind is not DeclarationKind.OPT_IN:
        return CanaryReversibleWindow(
            state=WindowState.NOT_APPLICABLE,
            remedy=None,
            closes_on=CANARY_WINDOW_CLOSES_ON,
            closed_at=None,
        )
    closing = next((row for row in rows if row.stage is CutoverStage.CANARY_WINDOW_CLOSED), None)
    if closing is None:
        return CanaryReversibleWindow(
            state=WindowState.OPEN, remedy=remedy, closes_on=CANARY_WINDOW_CLOSES_ON, closed_at=None
        )
    return CanaryReversibleWindow(
        state=WindowState.CLOSED,
        remedy=RollbackRemedy.FORWARD_REPAIR,
        closes_on=CANARY_WINDOW_CLOSES_ON,
        closed_at=closing.recorded_at,
    )


def boundary_envelope(report: RollbackBoundaryReport) -> dict[str, object]:
    """Return the envelope one boundary report is reported as.

    Args:
        report: The report.

    Returns:
        The JSON-ready envelope.
    """
    return {"status": "rollback_boundary", **report.model_dump(mode="json")}


__all__ = [
    "CANARY_WINDOW_CLOSES_ON",
    "SEAL_FAULT_LOCATOR",
    "SIMPLE_WINDOW_CLOSES_ON",
    "ActivationSealFault",
    "CanaryReversibleWindow",
    "RollbackBoundaryReport",
    "RollbackRemedy",
    "SimpleRollbackWindow",
    "WindowState",
    "boundary_envelope",
    "read_seal_fault",
    "rollback_boundary_report",
    "seal_fault_path",
    "seal_first_native_mutation",
]
