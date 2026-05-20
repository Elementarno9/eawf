"""``/memory`` skill body.

Mirrors the dict body emitted by :class:`eawf.skills.memory.MemorySkill`:
a single ``save|list|forget`` memory operation intent plus the target
tier. A named verb (``save`` / ``forget``) without a ``name`` degrades to
``needs_user`` and carries a ``reason``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

MemoryVerb = Literal["save", "list", "forget"]
MemoryTierName = Literal["working", "archival", "retrieval"]


class MemoryBody(BaseModel):
    """Body for ``/memory`` operations."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["memory_operation"] = "memory_operation"
    verb: MemoryVerb
    name: str | None = None
    tier: MemoryTierName
    reason: str | None = None


__all__ = ["MemoryBody", "MemoryTierName", "MemoryVerb"]
