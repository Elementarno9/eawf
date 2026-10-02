"""What one daemon start repaired before it accepted a call, and what that cost.

A daemon killed mid-write leaves its native trees mid-stride, and the next start walks
them back before any client reads: it cuts torn ledger tails, carries journalled
intents to durability or abandons them, and finishes interrupted compactions. The
record states which of those paths the start took and how much each moved, measured
when the passes finished, so the console's Recovery frame can say what the daemon did
rather than leave it unknown.

The record is machine-local. It describes one machine's WAL and one process's start;
a clone has no crash of ours to report.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import model_validator

from eawf.kernel.runtime.provider import RuntimeRecord
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt
from eawf.kernel.state.types import UtcDatetime


class RecoveryPass(StrEnum):
    """One way a start can repair a tree, in the order the start runs them."""

    TAIL_REPAIR = "tail_repair"
    WAL_REPLAY = "wal_replay"
    STORE_REPAIR = "store_repair"


class BootRecovery(RuntimeRecord):
    """One daemon start's recovery, as the start measured it.

    Attributes:
        started_at: When the first recovery pass began.
        finished_at: When the last one ended; the difference is what the start cost
            before it could answer.
        truncated_ledgers: Ledgers cut back to their last complete line.
        finished_intents: Journalled mutations whose document had landed, carried the
            rest of the way to durable.
        abandoned_intents: Journalled mutations whose document never moved, so the
            intent was poisoned and never happened.
        document_rows_dropped: Document rows removed because their ledger had already
            committed the record.
    """

    started_at: UtcDatetime
    finished_at: UtcDatetime
    truncated_ledgers: StrictNonNegativeInt
    finished_intents: StrictNonNegativeInt
    abandoned_intents: StrictNonNegativeInt
    document_rows_dropped: StrictNonNegativeInt

    @model_validator(mode="after")
    def _finishes_after_it_starts(self) -> Self:
        """Refuse a recovery that ended before it began.

        Raises:
            ValueError: ``finished_at`` precedes ``started_at``.
        """
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        return self

    def passes(self) -> tuple[RecoveryPass, ...]:
        """Return the passes that repaired something, in run order; empty for a clean start."""
        acted = {
            RecoveryPass.TAIL_REPAIR: self.truncated_ledgers > 0,
            RecoveryPass.WAL_REPLAY: self.finished_intents + self.abandoned_intents > 0,
            RecoveryPass.STORE_REPAIR: self.document_rows_dropped > 0,
        }
        return tuple(step for step in RecoveryPass if acted[step])

    def duration_ms(self) -> int:
        """Return how long the passes took, in whole milliseconds."""
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


__all__ = ["BootRecovery", "RecoveryPass"]
