"""``/polish`` skill body.

Frozen at Phase 4 W01 per `ea-proposal.md` §15.2:

    /polish body: { groups: [{topic, scope, risk, items: [{kind:
                    stale_doc|duplicate_rule|broken_link|orphan_artifact|
                    stale_memory|naming_drift, location, action,
                    applied: bool}]}], memory_pass: {promotions, prunes,
                    compactions}, report_only: bool }
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.skills.bodies.user_question import UserQuestion

# Frozen literal per §15.2.
PolishItemKind = Literal[
    "stale_doc",
    "duplicate_rule",
    "broken_link",
    "orphan_artifact",
    "stale_memory",
    "naming_drift",
]


class PolishItem(BaseModel):
    """One polish action item."""

    model_config = ConfigDict(extra="forbid")

    kind: PolishItemKind
    location: str
    action: str
    applied: bool = False


class PolishGroup(BaseModel):
    """One polish group bucket."""

    model_config = ConfigDict(extra="forbid")

    topic: str
    scope: str
    risk: str
    items: list[PolishItem] = Field(default_factory=list)


class PolishMemoryPass(BaseModel):
    """Memory-pass summary block."""

    model_config = ConfigDict(extra="forbid")

    promotions: int = 0
    prunes: int = 0
    compactions: int = 0


class PolishBody(BaseModel):
    """Body for ``/polish``."""

    model_config = ConfigDict(extra="forbid")

    groups: list[PolishGroup] = Field(default_factory=list)
    memory_pass: PolishMemoryPass | None = None
    report_only: bool = False
    user_question: UserQuestion | None = None


__all__ = [
    "PolishBody",
    "PolishGroup",
    "PolishItem",
    "PolishItemKind",
    "PolishMemoryPass",
]
