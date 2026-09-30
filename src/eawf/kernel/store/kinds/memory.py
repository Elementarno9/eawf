"""Memory payload models: the epoch-1 store payload and the epoch-2 ledger note."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import Confidence, MemoryStatus, MemoryTier
from eawf.kernel.state.types import UtcDatetime

#: The ``payload_kind`` a native memory ledger line carries, which is what
#: tells it apart from the rows the cutover imported into the same ledger.
MEMORY_NOTE_KIND: Final = "memory_note"


class MemoryPayload(BaseModel):
    """Payload for a memory store record.

    The two ``None``-defaulted fields are policy markers:

    - ``promoted_to_artifact_id`` — set when a memory entry is canonised into a
      durable artifact (a :class:`~eawf.kernel.state.models.Decision` row in v0.1).
      The link is stored on the latest JSONL envelope so a cache rebuild from
      the JSONL alone reconstructs the supersession state.
    - ``expired_at`` — set when ``eawf memory prune`` flips the entry to
      :class:`~eawf.kernel.state.enums.MemoryStatus.PRUNED`. The original record is
      preserved (soft delete); compaction reclaims space later.

    Both fields are additive and ``None``-defaulted so ``extra="forbid"``
    payloads written before W03 still validate.
    """

    model_config = ConfigDict(extra="forbid")

    body: str
    confidence: Confidence
    review_due: datetime | None = None
    promoted_to_artifact_id: str | None = None
    expired_at: datetime | None = None


class MemoryNote(BaseModel):
    """One memory note as a reader sees it, and as an epoch-2 ledger line holds it.

    A note is never edited in place: a revision is a new ledger line that
    supersedes the line it revises, so the ledger keeps every revision and
    reads back only the current one.

    Attributes:
        payload_kind: Always :data:`MEMORY_NOTE_KIND`.
        id: The note's ``MEM-`` id; the ledger line's record key.
        scope_id: The scope the note is anchored to.
        title: The short title it was written under.
        summary: The one-line summary list views show.
        body: The full text; empty for an imported note whose body the
            source never held.
        confidence: How far the note is to be trusted.
        status: Its lifecycle status.
        tier: Its placement in the working, archival or retrieval tier.
        review_due: When it is due for review; the age anchor when set.
        promoted_to_artifact_id: The decision it was promoted into.
        source_ref: The store record it was promoted from.
        expired_at: When a prune retired it.
        created_at: When it was written, or ``None`` when the source never
            recorded it; a note with neither this nor ``review_due`` has no
            age, so no age threshold selects it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload_kind: Literal["memory_note"] = MEMORY_NOTE_KIND
    id: Annotated[str, Field(min_length=1, max_length=128)]
    scope_id: Annotated[str, Field(min_length=1)]
    title: str
    summary: str
    body: str = ""
    confidence: Confidence
    status: MemoryStatus = MemoryStatus.ACTIVE
    tier: MemoryTier = MemoryTier.WORKING
    review_due: UtcDatetime | None = None
    promoted_to_artifact_id: str | None = None
    source_ref: str | None = None
    expired_at: UtcDatetime | None = None
    created_at: UtcDatetime | None = None

    @property
    def age_anchor(self) -> datetime | None:
        """Return the instant a note's age is measured from, if it has one."""
        return self.review_due or self.created_at
