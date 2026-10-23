"""Jury-validation cohort model tests.

The cohort models join a verdict outcome to its ground truth. These tests pin
the models' shapes and error paths: each is closed, so a drifted field fails
validation rather than silently entering the cohort.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
)
from eawf.observability.eval.jury_validation import (
    GoldLabel,
    LabeledVerdict,
    LabelSource,
    ValidationCohort,
)
from eawf.observability.eval.reputation import VerdictOutcome

_T0 = datetime(2026, 5, 1, tzinfo=UTC)


# --- C2: honest-empty path ------------------------------------------------


# --- C1: silver join correctness ------------------------------------------


# --- C1: gold override -----------------------------------------------------


# --- C1: unknown-wave gold label fails fast --------------------------------


# --- model error paths -----------------------------------------------------


def test_gold_label_rejects_extra_field() -> None:
    """An unexpected GoldLabel field fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        GoldLabel(
            wave_id="P01-I01-W01",
            ground_truth=True,
            labeled_at=_T0,
            unexpected="boom",
        )


def test_labeled_verdict_rejects_extra_field() -> None:
    """An unexpected LabeledVerdict field fails ``extra='forbid'``."""
    outcome = VerdictOutcome(
        base_id="P01-I01-W01",
        agent_role=AgentSessionRole.AUDITOR,
        runtime="claude",
        verdict=AgentReportVerdict.PASS,
        confidence=0.9,
        held=True,
        outcome_source="clean",
    )
    with pytest.raises(ValidationError):
        LabeledVerdict(
            outcome=outcome,
            ground_truth=True,
            label_source=LabelSource.SILVER,
            unexpected="boom",
        )


def test_validation_cohort_rejects_extra_field() -> None:
    """An unexpected ValidationCohort field fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        ValidationCohort(silver=[], gold=[], unexpected="boom")
