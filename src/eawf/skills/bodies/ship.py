"""``/ship`` skill body.

Per ``docs/architecture/envelope.md``:

    /ship body: { commit_groups: [{message, files, evidence_refs}],
                  push: {ref, status}, pr: {action, url, template,
                  gates: {ci, reviews, state_valid}},
                  estimate_vs_actual: {…}, rollback_notes }
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.skills.bodies.user_question import UserQuestion


class ShipCommitGroup(BaseModel):
    """One logical commit group within a ship run."""

    model_config = ConfigDict(extra="forbid")

    message: str
    files: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class ShipPush(BaseModel):
    """Push outcome block."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    status: str


class ShipPrGates(BaseModel):
    """PR gate statuses."""

    model_config = ConfigDict(extra="forbid")

    ci: str
    reviews: str
    state_valid: bool


class ShipPr(BaseModel):
    """PR block for a ship run."""

    model_config = ConfigDict(extra="forbid")

    action: str
    url: str | None = None
    template: str | None = None
    gates: ShipPrGates


class ShipBody(BaseModel):
    """Body for ``/ship``."""

    model_config = ConfigDict(extra="forbid")

    commit_groups: list[ShipCommitGroup] = Field(default_factory=list)
    push: ShipPush | None = None
    pr: ShipPr | None = None
    estimate_vs_actual: dict[str, Any] = Field(default_factory=dict)
    rollback_notes: str | None = None
    user_question: UserQuestion | None = None


__all__ = [
    "ShipBody",
    "ShipCommitGroup",
    "ShipPr",
    "ShipPrGates",
    "ShipPush",
]
