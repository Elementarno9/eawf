"""Unit tests for the Claim + OpenQuestion research-campaign entities.

The Claim ledger is the prerequisite of the pruning pass + SaturationReport,
and per Decision D-1 :class:`OpenQuestion` is its OWN first-class entity — not
a Claim variant. These tests pin:

* both models round-trip losslessly through ``model_dump`` / ``model_validate``;
* both forbid unknown keys (``ConfigDict(extra="forbid")``, AGENTS rule 2);
* the bounded ``title`` (≤72, no trailing period by convention) rejects an
  over-cap value at ingestion;
* the closed status enums reject an off-ladder token;
* Claim and OpenQuestion are distinct types with distinct field sets (the
  D-1 separation).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state import models
from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus, Urgency


def _now() -> datetime:
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _claim_payload(claim_id: str = "CLM-001", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": claim_id,
        "scope_id": "ABC",
        "title": "Vale wraps cleanly inside eawf hook",
        "description": None,
        "status": "open",
        "evidence_refs": [],
        "source_artifact_id": None,
        "answers_question_id": None,
        "created_at": _now().isoformat(),
        "superseded_by": None,
    }
    payload.update(overrides)
    return payload


def _open_question_payload(qid: str = "OQ-001", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": qid,
        "scope_id": "ABC",
        "title": "Which prose linter covers every artifact type",
        "description": None,
        "status": "open",
        "blocking": False,
        "answered_by_claim_id": None,
        "created_at": _now().isoformat(),
        "resolved_at": None,
    }
    payload.update(overrides)
    return payload


# ---- Claim ------------------------------------------------------------------


def test_claim_round_trips() -> None:
    claim = models.Claim.model_validate(
        _claim_payload(
            description="a longer statement of the claim",
            status="supported",
            evidence_refs=["docs/x.md", "urn:eawf:v1:artifact:NB1"],
            source_artifact_id="NB1",
            answers_question_id="OQ-001",
        )
    )
    dumped = claim.model_dump(mode="json")
    restored = models.Claim.model_validate(dumped)
    assert restored == claim
    assert restored.status is ClaimStatus.SUPPORTED
    assert restored.evidence_refs == ["docs/x.md", "urn:eawf:v1:artifact:NB1"]
    assert restored.answers_question_id == "OQ-001"


def test_claim_minimal_defaults() -> None:
    claim = models.Claim.model_validate(
        {
            "id": "CLM-001",
            "scope_id": "ABC",
            "title": "minimal claim",
            "status": "open",
            "created_at": _now().isoformat(),
        }
    )
    assert claim.description is None
    assert claim.evidence_refs == []
    assert claim.source_artifact_id is None
    assert claim.answers_question_id is None
    assert claim.superseded_by is None


def test_claim_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match=r"(unexpected|extra)"):
        models.Claim.model_validate(_claim_payload(unexpected="oops"))


def test_claim_rejects_over_cap_title() -> None:
    with pytest.raises(ValidationError):
        models.Claim.model_validate(_claim_payload(title="x" * 73))


def test_claim_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        models.Claim.model_validate(_claim_payload(title=""))


def test_claim_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        models.Claim.model_validate(_claim_payload(status="bogus"))


def test_claim_rejects_over_cap_description() -> None:
    with pytest.raises(ValidationError):
        models.Claim.model_validate(_claim_payload(description="d" * 501))


# ---- OpenQuestion -----------------------------------------------------------


def test_open_question_round_trips() -> None:
    question = models.OpenQuestion.model_validate(
        _open_question_payload(
            description="framing of the question",
            status="answered",
            blocking=True,
            answered_by_claim_id="CLM-001",
            resolved_at=_now().isoformat(),
        )
    )
    dumped = question.model_dump(mode="json")
    restored = models.OpenQuestion.model_validate(dumped)
    assert restored == question
    assert restored.status is OpenQuestionStatus.ANSWERED
    assert restored.blocking is True
    assert restored.answered_by_claim_id == "CLM-001"


def test_open_question_minimal_defaults() -> None:
    question = models.OpenQuestion.model_validate(
        {
            "id": "OQ-001",
            "scope_id": "ABC",
            "title": "minimal question",
            "status": "open",
            "created_at": _now().isoformat(),
        }
    )
    assert question.description is None
    assert question.blocking is False
    assert question.answered_by_claim_id is None
    assert question.resolved_at is None


def test_open_question_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match=r"(unexpected|extra)"):
        models.OpenQuestion.model_validate(_open_question_payload(unexpected="oops"))


def test_open_question_rejects_over_cap_title() -> None:
    with pytest.raises(ValidationError):
        models.OpenQuestion.model_validate(_open_question_payload(title="q" * 73))


def test_open_question_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        models.OpenQuestion.model_validate(_open_question_payload(status="bogus"))


# ---- D-1: Claim and OpenQuestion are SEPARATE entities ----------------------


def test_claim_and_open_question_are_distinct_types() -> None:
    """D-1: OpenQuestion is its own entity, not a Claim alias or subclass."""
    assert models.Claim is not models.OpenQuestion
    assert not issubclass(models.OpenQuestion, models.Claim)
    assert not issubclass(models.Claim, models.OpenQuestion)
    # The field sets differ: OpenQuestion carries blocking / answered_by_claim_id /
    # resolved_at; Claim carries evidence_refs / answers_question_id / superseded_by.
    claim_fields = set(models.Claim.model_fields)
    question_fields = set(models.OpenQuestion.model_fields)
    assert {"evidence_refs", "answers_question_id", "superseded_by"} <= claim_fields
    assert {"blocking", "answered_by_claim_id", "resolved_at"} <= question_fields
    assert "evidence_refs" not in question_fields
    assert "blocking" not in claim_fields


# ---- P29-I01-W19: shared Urgency ladder -------------------------------------
#
# One closed ``Urgency`` StrEnum is the single definition shared by the
# needs_user pause, a research-campaign OpenQuestion, and the attention feed.
# These tests pin the ladder shape, the additive default on OpenQuestion, that
# every value round-trips (model + persistence seam), and that an off-ladder
# token is rejected at ingestion.


def test_urgency_ladder_is_the_closed_four_value_set() -> None:
    """The shared Urgency vocabulary is exactly low/normal/high/urgent."""
    assert [u.value for u in Urgency] == ["low", "normal", "high", "urgent"]


def test_open_question_urgency_defaults_to_normal() -> None:
    """Urgency is additive: an omitted value defaults to NORMAL (no schema bump)."""
    question = models.OpenQuestion.model_validate(
        {
            "id": "OQ-001",
            "scope_id": "ABC",
            "title": "minimal question",
            "status": "open",
            "created_at": _now().isoformat(),
        }
    )
    assert question.urgency is Urgency.NORMAL


def test_open_question_uses_the_shared_urgency_enum() -> None:
    """OpenQuestion.urgency references the one shared Urgency type, not a copy."""
    annotation = models.OpenQuestion.model_fields["urgency"].annotation
    assert annotation is Urgency


@pytest.mark.parametrize("value", [u.value for u in Urgency])
def test_open_question_urgency_round_trips_each_value(value: str) -> None:
    """Every urgency value survives a model_dump -> model_validate round-trip."""
    question = models.OpenQuestion.model_validate(_open_question_payload(urgency=value))
    assert question.urgency is Urgency(value)
    restored = models.OpenQuestion.model_validate(question.model_dump(mode="json"))
    assert restored.urgency is Urgency(value)
    assert restored == question


def test_open_question_rejects_unknown_urgency() -> None:
    """An off-ladder urgency token is rejected at ingestion (closed StrEnum)."""
    with pytest.raises(ValidationError):
        models.OpenQuestion.model_validate(_open_question_payload(urgency="emergency"))
