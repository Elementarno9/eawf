"""``/research`` skill body.

Per ``docs/architecture/envelope.md``:

    /research body: { brief_id, questions: [{q, answer, confidence,
                       sources}], options: [{name, tradeoffs, complexity,
                       reversibility, risks}], recommendation: {choice,
                       confidence, fallback}, peer_review: {reviewer_id,
                       findings: [], no_flaws_checks: []},
                       persisted_brief?: urn }
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.workflow.skills.bodies.user_question import UserQuestion


class ResearchQuestion(BaseModel):
    """One Q&A entry in a research brief."""

    model_config = ConfigDict(extra="forbid")

    q: str
    answer: str
    confidence: str
    sources: list[str] = Field(default_factory=list)


class ResearchOption(BaseModel):
    """One candidate option weighed in the recommendation."""

    model_config = ConfigDict(extra="forbid")

    name: str
    tradeoffs: str
    complexity: str
    reversibility: str
    risks: list[str] = Field(default_factory=list)


class ResearchRecommendation(BaseModel):
    """Final recommendation block."""

    model_config = ConfigDict(extra="forbid")

    choice: str
    confidence: str
    fallback: str | None = None


class ResearchPeerReview(BaseModel):
    """Peer-review block; v0.1 uses a single reviewer ID."""

    model_config = ConfigDict(extra="forbid")

    reviewer_id: str
    findings: list[str] = Field(default_factory=list)
    no_flaws_checks: list[str] = Field(default_factory=list)


class ResearchFanoutEnvelope(BaseModel):
    """One read-only agent dispatch envelope in a deep research plan."""

    model_config = ConfigDict(extra="forbid")

    envelope_id: str
    agent_role: str
    question: str
    prompt: str
    expected_output: str
    read_only: bool = True


class ResearchPlan(BaseModel):
    """Typed deep-research fan-out plan."""

    model_config = ConfigDict(extra="forbid")

    section_heading: Literal["## ResearchPlan"] = "## ResearchPlan"
    depth: Literal["deep"] = "deep"
    topic: str
    fanout_envelopes: list[ResearchFanoutEnvelope] = Field(default_factory=list)


class ResearchBody(BaseModel):
    """Body for ``/research``."""

    model_config = ConfigDict(extra="forbid")

    brief_id: str
    questions: list[ResearchQuestion] = Field(default_factory=list)
    options: list[ResearchOption] = Field(default_factory=list)
    recommendation: ResearchRecommendation | None = None
    peer_review: ResearchPeerReview | None = None
    persisted_brief: str | None = None
    research_plan: ResearchPlan | None = None
    user_question: UserQuestion | None = None


__all__ = [
    "ResearchBody",
    "ResearchFanoutEnvelope",
    "ResearchOption",
    "ResearchPeerReview",
    "ResearchPlan",
    "ResearchQuestion",
    "ResearchRecommendation",
]
