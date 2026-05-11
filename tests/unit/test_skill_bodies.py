"""Unit tests for the ten per-skill body Pydantic models.

For each body model:

- minimal-payload construction (only required fields);
- a representative full-payload round-trip via ``model_dump`` →
  ``model_validate``;
- ``extra='forbid'`` rejection of an unknown top-level key.

These tests pin the W01 freeze: any edit to ``skills/bodies/`` after
this wave that changes the field set should make at least one of these
tests fail loudly so the change goes through review.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from eawf.skills.bodies import (
    AuditBody,
    DifferentiateBody,
    FlowBody,
    InitBody,
    PolishBody,
    PrepBody,
    ResearchBody,
    ReviewBody,
    RoadmapBody,
    ShipBody,
    UserQuestion,
)


def _round_trip(model: BaseModel) -> None:
    """Assert ``model_validate(model.model_dump())`` round-trips."""
    rebuilt = type(model).model_validate(model.model_dump())
    assert rebuilt == model


def test_research_body_minimal_construction_and_round_trip() -> None:
    body = ResearchBody(brief_id="BR-001")
    _round_trip(body)


def test_research_body_full_payload_round_trip() -> None:
    body = ResearchBody.model_validate(
        {
            "brief_id": "BR-001",
            "questions": [
                {"q": "Why?", "answer": "Because.", "confidence": "high", "sources": ["doc-1"]},
            ],
            "options": [
                {
                    "name": "A",
                    "tradeoffs": "fast vs. correct",
                    "complexity": "low",
                    "reversibility": "easy",
                    "risks": ["timeout"],
                }
            ],
            "recommendation": {"choice": "A", "confidence": "medium", "fallback": "B"},
            "peer_review": {
                "reviewer_id": "REV-1",
                "findings": ["nit"],
                "no_flaws_checks": ["lint"],
            },
            "persisted_brief": "urn:eawf:v1:store:QR/research/BR-001",
        }
    )
    _round_trip(body)


def test_research_body_rejects_extra_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ResearchBody.model_validate({"brief_id": "BR-001", "unexpected": True})


def test_prep_body_minimal_construction_and_round_trip() -> None:
    body = PrepBody(iter_id="P00-I01", objective="ship something")
    _round_trip(body)


def test_prep_body_full_payload_round_trip() -> None:
    body = PrepBody.model_validate(
        {
            "iter_id": "P00-I01",
            "objective": "ship something",
            "non_goals": ["docs only"],
            "dag": [
                {
                    "task_id": "T1",
                    "deps": [],
                    "file_scope": ["src/foo.py"],
                    "commands": ["uv run pytest"],
                    "evidence": ["events.jsonl"],
                    "risk": "low",
                }
            ],
            "waves": [
                {
                    "wave_id": "P00-I01-W01",
                    "tasks": ["T1"],
                    "worktree_policy": "isolate",
                    "estimate_eu": 2.0,
                }
            ],
            "acceptance": {"checks": ["pytest"], "baselines": ["green CI"]},
            "approval_required": True,
        }
    )
    _round_trip(body)


def test_prep_body_rejects_extra_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        PrepBody.model_validate(
            {
                "iter_id": "P00-I01",
                "objective": "ship",
                "unexpected": "x",
            }
        )


def test_audit_body_round_trip_with_full_payload() -> None:
    body = AuditBody.model_validate(
        {
            "scope_id": "P00-I01",
            "kind": "ship-gate",
            "checks_run": [
                {
                    "check_id": "C1",
                    "command": "ruff check .",
                    "status": "pass",
                    "output_blob": "no errors",
                }
            ],
            "outcomes_measured": [
                {
                    "outcome_id": "O1",
                    "value": 95.0,
                    "threshold": 90.0,
                    "verdict": "pass",
                }
            ],
            "hypothesis_verdicts": [
                {
                    "hypothesis_id": "H01-01",
                    "verdict": "confirmed",
                    "evidence_commit": "abc123",
                }
            ],
            "findings": [
                {
                    "severity": "minor",
                    "location": "src/foo.py:12",
                    "summary": "long line",
                    "kind": "follow-up",
                }
            ],
            "audit_artifact_urn": "urn:eawf:v1:audit:QR/AU-1",
        }
    )
    _round_trip(body)


def test_audit_body_rejects_unknown_kind_literal() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        AuditBody.model_validate({"scope_id": "X", "kind": "incident"})


def test_audit_body_rejects_unknown_finding_kind() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        AuditBody.model_validate(
            {
                "scope_id": "X",
                "kind": "evaluation",
                "findings": [
                    {
                        "severity": "x",
                        "location": "y",
                        "summary": "z",
                        "kind": "deferred",
                    }
                ],
            }
        )


def test_ship_body_minimal_round_trip() -> None:
    body = ShipBody()
    _round_trip(body)


def test_ship_body_full_payload_round_trip() -> None:
    body = ShipBody.model_validate(
        {
            "commit_groups": [
                {
                    "message": "feat: add thing",
                    "files": ["src/foo.py"],
                    "evidence_refs": ["urn:eawf:v1:audit:QR/AU-1"],
                }
            ],
            "push": {"ref": "main", "status": "ok"},
            "pr": {
                "action": "open",
                "url": "https://github.com/x/y/pull/1",
                "template": "default",
                "gates": {"ci": "green", "reviews": "1 approved", "state_valid": True},
            },
            "estimate_vs_actual": {"estimate_eu": 2.0, "actual_eu": 2.5},
            "rollback_notes": "git revert HEAD",
        }
    )
    _round_trip(body)


def test_ship_body_rejects_extra_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ShipBody.model_validate({"unexpected": 1})


def test_review_body_round_trip_and_recommendation_literal() -> None:
    body = ReviewBody(
        pr_url="https://example.com/pull/1",
        base="main",
        head="feat",
        recommendation="approve",
    )
    _round_trip(body)
    with pytest.raises(ValidationError, match="Input should be"):
        ReviewBody.model_validate(
            {
                "pr_url": "x",
                "base": "main",
                "head": "feat",
                "recommendation": "merge",
            }
        )


def test_polish_body_round_trip_and_item_kind_literal() -> None:
    body = PolishBody.model_validate(
        {
            "groups": [
                {
                    "topic": "stale",
                    "scope": "docs",
                    "risk": "low",
                    "items": [
                        {
                            "kind": "stale_doc",
                            "location": "docs/old.md",
                            "action": "delete",
                            "applied": False,
                        }
                    ],
                }
            ],
            "memory_pass": {"promotions": 1, "prunes": 2, "compactions": 0},
            "report_only": False,
        }
    )
    _round_trip(body)
    # Unknown item kind rejected.
    with pytest.raises(ValidationError, match="Input should be"):
        PolishBody.model_validate(
            {
                "groups": [
                    {
                        "topic": "x",
                        "scope": "y",
                        "risk": "low",
                        "items": [{"kind": "unknown_kind", "location": "x", "action": "y"}],
                    }
                ]
            }
        )


def test_init_body_minimal_round_trip_and_extra_rejected() -> None:
    body = InitBody()
    _round_trip(body)
    with pytest.raises(ValidationError, match="Extra inputs"):
        InitBody.model_validate({"unexpected": 1})


def test_roadmap_body_round_trip() -> None:
    body = RoadmapBody.model_validate(
        {
            "horizon": "Q3",
            "candidates": [
                {
                    "item_id": "R1",
                    "title": "Add X",
                    "rationale": "users want it",
                    "priority": "P1",
                    "estimate_eu": 5.0,
                }
            ],
            "chosen_order": ["R1"],
        }
    )
    _round_trip(body)
    with pytest.raises(ValidationError, match="Extra inputs"):
        RoadmapBody.model_validate({"unexpected": 1})


def test_differentiate_body_round_trip() -> None:
    body = DifferentiateBody.model_validate(
        {
            "target_scope": "PROJECT",
            "axes": [
                {
                    "name": "speed",
                    "current": "fast",
                    "peers": ["slow"],
                    "advantage": "yes",
                }
            ],
            "conclusions": ["we are faster"],
        }
    )
    _round_trip(body)
    with pytest.raises(ValidationError, match="Extra inputs"):
        DifferentiateBody.model_validate({"unexpected": 1})


def test_flow_body_round_trip_with_step_dicts() -> None:
    """``steps`` accepts a list of envelope dicts (back-compat with §15.2)."""
    body = FlowBody.model_validate(
        {
            "topic": "demo",
            "steps": [
                {
                    "header": {
                        "skill": "/research",
                        "scope_id": "urn:eawf:v1:state:QR",
                        "session": "urn:eawf:v1:store:QR/sessions/SES-1",
                        "started_at": "2026-05-09T00:00:00Z",
                        "finished_at": "2026-05-09T00:00:01Z",
                        "status": "ok",
                    },
                    "body": "step ok",
                    "footer": {},
                }
            ],
            "terminal_status": "ok",
        }
    )
    _round_trip(body)


def test_flow_body_rejects_extra_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        FlowBody.model_validate({"unexpected": 1})


def test_user_question_minimum_2_options_required() -> None:
    """The shared :class:`UserQuestion` rejects fewer than 2 options."""
    with pytest.raises(ValidationError, match=r"2-4 entries"):
        UserQuestion(
            question="pick",
            options=[],
        )


def test_user_question_maximum_4_options_required() -> None:
    """Five options should be rejected (the proposal caps at 4)."""
    with pytest.raises(ValidationError, match=r"2-4 entries"):
        UserQuestion(
            question="pick",
            options=[
                {"label": str(i)}
                for i in range(5)  # type: ignore[list-item]
            ],
        )


def test_user_question_round_trip_2_options() -> None:
    payload = {
        "question": "Pick one",
        "options": [
            {"label": "A", "description": "first"},
            {"label": "B"},
        ],
    }
    q = UserQuestion.model_validate(payload)
    assert q.model_dump(exclude_none=True) == payload
