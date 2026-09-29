"""PLAN-044: a Claim carries optional falsifier and implication prose, each at most 300 characters.

The length boundary at 300 and 301, the absent field, and the refusal of a
blank value and of a fourth prose field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import ClaimStatus
from eawf.kernel.state.models import Claim

pytestmark = pytest.mark.unit


def claim(**overrides: Any) -> Claim:
    fields: dict[str, Any] = {
        "id": "CLM-0004",
        "scope_id": "CAM-0001",
        "title": "The normalizer preserves event ordering under replay",
        "description": "Replay any recorded log and every event arrives in order.",
        "status": ClaimStatus.OPEN,
        "created_at": datetime(2026, 9, 18, tzinfo=UTC),
    }
    fields.update(overrides)
    return Claim.model_validate(fields)


@pytest.mark.parametrize("field", ["falsifier", "implication"])
def test_plan_044_prose_at_300_characters_validates(field: str) -> None:
    assert len(getattr(claim(**{field: "x" * 300}), field)) == 300


@pytest.mark.parametrize("field", ["falsifier", "implication"])
def test_plan_044_prose_at_301_characters_fails(field: str) -> None:
    with pytest.raises(ValidationError):
        claim(**{field: "x" * 301})


@pytest.mark.parametrize("field", ["falsifier", "implication"])
def test_plan_044_absent_prose_is_none_not_a_placeholder(field: str) -> None:
    assert getattr(claim(), field) is None


@pytest.mark.parametrize("field", ["falsifier", "implication"])
def test_plan_044_blank_prose_is_refused(field: str) -> None:
    with pytest.raises(ValidationError):
        claim(**{field: ""})


def test_plan_044_both_fields_sit_beside_the_description() -> None:
    c = claim(
        falsifier="One replay puts two events out of order.",
        implication="A replayed run reads exactly like the live one.",
    )
    assert (c.description, c.implication, c.falsifier) == (
        "Replay any recorded log and every event arrives in order.",
        "A replayed run reads exactly like the live one.",
        "One replay puts two events out of order.",
    )


def test_plan_044_no_fourth_prose_field_is_typed() -> None:
    with pytest.raises(ValidationError):
        claim(who_cares="every operator reading a rebuilt timeline")
