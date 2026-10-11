"""The feed notice a Batch's proof publishes when what holds the Batch changes.

A proof's progress is no committed transition, so it carries no ordinal a keyed patch
could be ordered by. The daemon announces it instead, as it announces a Run's appends,
and a console holding the Batch reads its routes again to show the note.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.epoch2.urns import BatchUrn


class BatchProofNotice(BaseModel):
    """What one proof notice states.

    Attributes:
        batch_ref: The Batch whose proof moved.
        note: Why the proof now holds the Batch -- ``proving 2 criteria at <sha>`` or
            ``CR-0042-1 unmet at <sha>`` -- or ``None`` once it no longer does.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: BatchUrn
    note: str | None


__all__ = ["BatchProofNotice"]
