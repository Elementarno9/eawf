"""``/review`` skill body.

Frozen at Phase 4 W01 per `ea-proposal.md` §15.2:

    /review body: { pr_url, base, head, findings: [{severity, location,
                    comment, suggested_fix}], recommendation:
                    approve|comment|request_changes|fix_locally,
                    posted: bool }
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.skills.bodies.user_question import UserQuestion

# Frozen literal per §15.2.
ReviewRecommendation = Literal["approve", "comment", "request_changes", "fix_locally"]


class ReviewFinding(BaseModel):
    """One review finding."""

    model_config = ConfigDict(extra="forbid")

    severity: str
    location: str
    comment: str
    suggested_fix: str | None = None


class ReviewBody(BaseModel):
    """Body for ``/review``."""

    model_config = ConfigDict(extra="forbid")

    pr_url: str
    base: str
    head: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    recommendation: ReviewRecommendation
    posted: bool = False
    user_question: UserQuestion | None = None


__all__ = ["ReviewBody", "ReviewFinding", "ReviewRecommendation"]
