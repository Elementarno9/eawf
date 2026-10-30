"""Tests for the description-floor model validator.

The ``_DescribedEntity`` ``model_validator`` floors a *present*
``description`` on Phase / Iter / Wave / Decision (grandfathering ``None``)
and rejects a description that merely restates the title.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    DecisionStatus,
)
from eawf.kernel.state.models import Decision

_NOW = datetime.now(UTC)


# ---- description floor: grandfather None ------------------------------------


# ---- description floor: too short -------------------------------------------


# ---- description floor: title-prefix duplicate ------------------------------


def test_decision_description_pure_restatement_rejected() -> None:
    # A near-pure restatement (title typed again, only trailing punctuation
    # added) carries no signal beyond the title and is rejected.
    with pytest.raises(ValidationError, match="merely repeats the title"):
        Decision(
            id="D17",
            scope_id="QR",
            title="Use Textual for the TUI",
            description="Use Textual for the TUI.",
            rationale="r",
            status=DecisionStatus.ACTIVE,
            created_at=_NOW,
        )


def test_decision_description_adding_the_why_is_accepted() -> None:
    # Opening with the title and then adding the *why* carries new content and
    # is accepted — the rule targets pure restatement, not any prefix overlap.
    decision = Decision(
        id="D17",
        scope_id="QR",
        title="Use Textual for the TUI",
        description="Use Textual for the TUI because Rich lacks a mode chassis.",
        rationale="r",
        status=DecisionStatus.ACTIVE,
        created_at=_NOW,
    )
    assert decision.description is not None


def test_decision_description_distinct_from_title_accepted() -> None:
    decision = Decision(
        id="D17",
        scope_id="QR",
        title="Use Textual for the TUI",
        description="Rich lacks a mode chassis; the rebuild needs one for live views.",
        rationale="r",
        status=DecisionStatus.ACTIVE,
        created_at=_NOW,
    )
    assert decision.description is not None
