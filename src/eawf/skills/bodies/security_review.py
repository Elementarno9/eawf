"""``/security-review`` skill body.

Mirrors the dict body emitted by
:class:`eawf.skills.security_review.SecurityReviewSkill`: the per-check
pass/fail tally from running the audit-check DSL against a closed scope.
A missing/unreadable spec degrades to ``needs_user``; any failing check
flips the terminal status to ``failed`` with repair commands.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SecurityReviewFinding(BaseModel):
    """One audit-DSL check result."""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    details: str | None = None


class SecurityReviewBody(BaseModel):
    """Body for ``/security-review`` audit-DSL runs."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["security_review_report"] = "security_review_report"
    scope_id: str
    spec_path: str | None = None
    checks_run: int = 0
    findings: list[SecurityReviewFinding] = Field(default_factory=list)
    reason: str | None = None


__all__ = ["SecurityReviewBody", "SecurityReviewFinding"]
