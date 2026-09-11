"""The append-only record of how far one apply got.

A cutover is a sequence of durable writes with a one-way door near the
end. If the process dies halfway, the only useful question is which
stage completed, and the only answer that can be trusted is one written
before the stage it describes was attempted -- so the journal is flushed
*ahead* of each durable act, never after it. A journal that lags the
tree would report a cutover as less far along than it is, which is the
one error that turns a recoverable crash into a double write.

Rows chain: each digests the row before it. A rewritten, reordered or
dropped row breaks the chain, so a journal that verifies is a journal
nothing has edited. The file itself is written through the same
append-only guard the epoch-2 ledgers use, which refuses any content
that is not an extension of what is already committed.

Rows are buffered until the apply commits to durable work. A cutover
that refuses a precondition, or that finds the generation it would build
is already selected, has changed nothing -- and a journal row claiming
otherwise would be the file's first lie.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field

from eawf.kernel.migration.epoch2.errors import MigrationJournalBrokenError
from eawf.kernel.migration.epoch2.manifest import RollbackBoundary
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel, rule_digest
from eawf.kernel.store.ledger import guarded_ledger_write

logger = logging.getLogger(__name__)


#: The schema version every journal row carries.
JOURNAL_SCHEMA_VERSION: Final[Literal["1"]] = "1"

#: The digest the first row chains from. A fixed seed rather than an
#: empty string so a truncated journal cannot be passed off as a fresh
#: one by deleting every row.
CHAIN_SEED: Final = "epoch2-cutover-journal"


class CutoverStage(StrEnum):
    """Every stage of one apply, in the order the apply runs them."""

    FENCE_CLEARED = "fence_cleared"
    WORKSPACE_RESOLVED = "workspace_resolved"
    AUTHORITY_LOCKED = "authority_locked"
    QUIESCENCE_PROVED = "quiescence_proved"
    RECENSUS_MATCHED = "recensus_matched"
    MAINTENANCE_ENTERED = "maintenance_entered"
    SNAPSHOT_TAKEN = "snapshot_taken"
    GENERATION_BUILT = "generation_built"
    READ_SMOKE_PASSED = "read_smoke_passed"
    GENERATION_SELECTED = "generation_selected"
    MARKER_WRITTEN = "marker_written"
    MAINTENANCE_EXITED = "maintenance_exited"


class CutoverJournalRow(StrictMigrationModel):
    """One stage of one apply, chained to the stage before it.

    Attributes:
        schema_version: Always ``"1"``.
        sequence: The row's 1-based position in the whole journal, across
            every apply the tree has seen.
        stage: Which stage the row records.
        boundary: How far rollback could still go once this stage lands.
        recorded_at: When the stage was reached.
        detail: What the stage found or wrote, in one line.
        previous_digest: The digest of the row before this one, or
            :data:`CHAIN_SEED` for the first row.
        digest: The digest over this row's own content and
            ``previous_digest``.
    """

    schema_version: Literal["1"]
    sequence: Annotated[int, Field(ge=1)]
    stage: CutoverStage
    boundary: RollbackBoundary
    recorded_at: datetime
    detail: Annotated[str, Field(min_length=1, max_length=200)]
    previous_digest: Annotated[str, Field(min_length=1, max_length=64)]
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def row_digest(
    *,
    sequence: int,
    stage: CutoverStage,
    boundary: RollbackBoundary,
    recorded_at: datetime,
    detail: str,
    previous_digest: str,
) -> str:
    """Return the chained digest one journal row is identified by.

    Args:
        sequence: The row's position in the journal.
        stage: Which stage the row records.
        boundary: The rollback boundary the stage leaves behind.
        recorded_at: When the stage was reached.
        detail: The row's one-line detail.
        previous_digest: The preceding row's digest.

    Returns:
        A 64-character lowercase hex digest.
    """
    return rule_digest(
        [
            JOURNAL_SCHEMA_VERSION,
            previous_digest,
            sequence,
            stage.value,
            boundary.value,
            recorded_at.isoformat(),
            detail,
        ]
    )


def read_journal(path: Path) -> tuple[CutoverJournalRow, ...]:
    """Return every row the journal at ``path`` holds.

    Args:
        path: The journal file. A missing file holds no rows, which is
            the state before the first apply.

    Returns:
        The rows, in file order.

    Raises:
        MigrationJournalBrokenError: A line is not the JSON of a journal
            row. A journal that cannot be parsed cannot answer how far a
            previous apply got.
    """
    if not path.exists():
        return ()
    rows: list[CutoverJournalRow] = []
    for number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(CutoverJournalRow.model_validate(json.loads(line)))
        except ValueError as error:
            raise MigrationJournalBrokenError(
                f"{path.name} line {number} is not a cutover journal row: {error}"
            ) from error
    return tuple(rows)


def require_chain_intact(rows: Iterable[CutoverJournalRow]) -> None:
    """Refuse a journal whose rows do not chain.

    Args:
        rows: The rows as read, in file order.

    Raises:
        MigrationJournalBrokenError: A row's sequence, chain link or
            digest does not follow from the row before it. The message
            names the first offending row, because the rows after it
            cannot be assessed until it is explained.
    """
    previous = CHAIN_SEED
    for index, row in enumerate(rows, start=1):
        if row.sequence != index:
            raise MigrationJournalBrokenError(
                f"journal row {index} records sequence {row.sequence}, so a row was "
                "removed, reordered or inserted"
            )
        if row.previous_digest != previous:
            raise MigrationJournalBrokenError(
                f"journal row {index} chains from {row.previous_digest[:12]} but the row "
                f"before it digests to {previous[:12]}"
            )
        expected = row_digest(
            sequence=row.sequence,
            stage=row.stage,
            boundary=row.boundary,
            recorded_at=row.recorded_at,
            detail=row.detail,
            previous_digest=row.previous_digest,
        )
        if row.digest != expected:
            raise MigrationJournalBrokenError(
                f"journal row {index} digests to {row.digest[:12]} but its content "
                f"digests to {expected[:12]}, so the row was edited after it was written"
            )
        previous = row.digest


class CutoverJournal:
    """The journal of one target tree, buffered until the apply commits.

    Attributes:
        path: The journal file.
    """

    def __init__(self, path: Path) -> None:
        """Open the journal at ``path`` without reading or writing it.

        Args:
            path: The journal file. It is neither created nor read here;
                a journal that is never flushed leaves no trace.
        """
        self.path = path
        self._committed: tuple[CutoverJournalRow, ...] = ()
        self._buffered: list[CutoverJournalRow] = []
        self._loaded = False

    def committed_rows(self) -> tuple[CutoverJournalRow, ...]:
        """Return the rows already on disk, verifying the chain once.

        Returns:
            The committed rows, in file order.

        Raises:
            MigrationJournalBrokenError: The journal does not parse, or
                its rows do not chain.
        """
        if not self._loaded:
            rows = read_journal(self.path)
            require_chain_intact(rows)
            self._committed = rows
            self._loaded = True
        return self._committed

    def record(
        self,
        *,
        stage: CutoverStage,
        boundary: RollbackBoundary,
        recorded_at: datetime,
        detail: str,
    ) -> CutoverJournalRow:
        """Buffer one stage, without touching the disk.

        Args:
            stage: Which stage was reached.
            boundary: The rollback boundary it leaves behind.
            recorded_at: When it was reached.
            detail: One line naming what it found or wrote.

        Returns:
            The buffered row.

        Raises:
            MigrationJournalBrokenError: The committed journal does not
                verify, so a new row cannot be chained onto it.
            ValidationError: ``detail`` is empty or over-long.
        """
        tail = self._buffered[-1] if self._buffered else None
        if tail is not None:
            sequence, previous = tail.sequence + 1, tail.digest
        else:
            committed = self.committed_rows()
            sequence = len(committed) + 1
            previous = committed[-1].digest if committed else CHAIN_SEED
        row = CutoverJournalRow(
            schema_version=JOURNAL_SCHEMA_VERSION,
            sequence=sequence,
            stage=stage,
            boundary=boundary,
            recorded_at=recorded_at,
            detail=detail,
            previous_digest=previous,
            digest=row_digest(
                sequence=sequence,
                stage=stage,
                boundary=boundary,
                recorded_at=recorded_at,
                detail=detail,
                previous_digest=previous,
            ),
        )
        self._buffered.append(row)
        return row

    def flush(self) -> int:
        """Append every buffered row, extending the file and nothing else.

        Returns:
            How many rows were written. ``0`` means nothing was buffered,
            which leaves the file untouched -- including never creating
            it.

        Raises:
            LedgerAppendOnlyError: The proposed content does not extend
                what the journal already holds, which would mean another
                writer appended underneath this one.
        """
        if not self._buffered:
            return 0
        current = self.path.read_bytes() if self.path.exists() else b""
        addition = "".join(
            f"{json.dumps(row.model_dump(mode='json'), sort_keys=True)}\n" for row in self._buffered
        ).encode("utf-8")
        guarded_ledger_write(self.path, current + addition)
        written = len(self._buffered)
        self._committed = (*self.committed_rows(), *self._buffered)
        self._buffered = []
        logger.info(f"flush path={self.path.name} rows={written}")
        return written

    def stages(self) -> tuple[CutoverStage, ...]:
        """Return every stage the journal holds, committed then buffered."""
        return tuple(row.stage for row in (*self.committed_rows(), *self._buffered))


__all__ = [
    "CHAIN_SEED",
    "JOURNAL_SCHEMA_VERSION",
    "CutoverJournal",
    "CutoverJournalRow",
    "CutoverStage",
    "read_journal",
    "require_chain_intact",
    "row_digest",
]
