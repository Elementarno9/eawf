"""MemoryPayload — payload model for StoreKind.MEMORY records."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from eawf.state.enums import Confidence


class MemoryPayload(BaseModel):
    """Payload for a memory store record.

    The two ``None``-defaulted fields are policy markers:

    - ``promoted_to_artifact_id`` — set when a memory entry is canonised into a
      durable artifact (a :class:`~eawf.state.models.Decision` row in v0.1).
      The link is stored on the latest JSONL envelope so a cache rebuild from
      the JSONL alone reconstructs the supersession state.
    - ``expired_at`` — set when ``eawf memory prune`` flips the entry to
      :class:`~eawf.state.enums.MemoryStatus.PRUNED`. The original record is
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
