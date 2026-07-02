from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.spec.intent import IntentBrief


def make_intent(
    problem: str = "test wave lacks a typed intent",
    desired_outcome: str = "the test wave carries a populated IntentBrief",
) -> IntentBrief:
    """Build a fully-populated :class:`IntentBrief` for plan_wave call sites.

    The authoring guard on :func:`eawf.workflow.lifecycle.wave.plan_wave`
    rejects an intent of ``None``, so every test that stages a wave needs a
    populated brief. This shared factory keeps the 150+ call sites DRY and
    returns a brief that also carries a non-empty ``priority_rationale``,
    one ``planned_steps`` entry, and one ``risks`` entry so the fixture
    survives a future authoring gate that requires non-blank body fields.

    Args:
        problem: The brief's ``problem`` line (1-200 chars).
        desired_outcome: The brief's ``desired_outcome`` line (1-200 chars).

    Returns:
        A populated :class:`IntentBrief`.
    """
    return IntentBrief(
        problem=problem,
        desired_outcome=desired_outcome,
        priority_rationale="exercises the plan_wave authoring path under test",
        planned_steps=["stage the wave with a populated intent"],
        risks=["none material for the test fixture"],
    )


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Skeleton fixture for a throwaway repository directory.

    Phase 1+ tests will populate this with the canonical .ea/ skeleton via
    eawf.platform.install. For now it returns a bare temp directory.
    """
    return tmp_path


def make_floor_waiver():
    """Build a typed criteria-floor waiver for legacy-criterion fixtures.

    The plan-time typed-criteria floor (P30-I23-W26) rejects a wave
    authored with grandfathered legacy rows; fixtures that deliberately
    model migration-era legacy waves attach this waiver so the modelled
    state stays constructible while the floor stays on for real authoring.
    """
    from datetime import UTC, datetime

    from eawf.kernel.state.models import CriteriaFloorWaiver

    return CriteriaFloorWaiver(
        reason="test fixture models a migration-era legacy wave",
        waived_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
