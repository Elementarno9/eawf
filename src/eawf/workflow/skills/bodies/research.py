"""``/research`` skill body.

Per ``docs/architecture/envelope.md``:

    /research body: { brief_id, questions: [{q, answer, confidence,
                       sources}], peer_review: {reviewer_id,
                       findings: [], no_flaws_checks: []},
                       persisted_brief?: urn }
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eawf.workflow.skills.bodies.user_question import UserQuestion


class ResearchQuestion(BaseModel):
    """One Q&A entry in a research brief."""

    model_config = ConfigDict(extra="forbid")

    q: str
    answer: str
    confidence: str
    sources: list[str] = Field(default_factory=list)


class ResearchPeerReview(BaseModel):
    """Peer-review block; v0.1 uses a single reviewer ID."""

    model_config = ConfigDict(extra="forbid")

    reviewer_id: str
    findings: list[str] = Field(default_factory=list)
    no_flaws_checks: list[str] = Field(default_factory=list)


class ResearchBody(BaseModel):
    """Body for ``/research``."""

    model_config = ConfigDict(extra="forbid")

    brief_id: str
    questions: list[ResearchQuestion] = Field(default_factory=list)
    peer_review: ResearchPeerReview | None = None
    persisted_brief: str | None = None
    user_question: UserQuestion | None = None


__all__ = [
    "ResearchBody",
    "ResearchPeerReview",
    "ResearchQuestion",
]
