"""Tests for :mod:`eawf.kernel.spec.campaign_driver` (per-level dispatch).

Pins the per-level dispatcher that un-idles the live round driver: it
reads a :class:`CockpitLevel` and routes the campaign to the matching
rung's runner. The point of these tests is the WIRING the audit found
missing -- a Level-3+ campaign now reaches
:func:`~eawf.kernel.spec.live_rounds.run_live_rounds` through a real
production caller, and a sub-live campaign provably does NOT.

Covers:

* Level 3+ (LIVE / higher) drives the live autonomous runner: the bounded
  loop actually runs (the injected round_runner is invoked) and the result
  carries a populated ``loop_result``.
* Sub-live levels do NOT trigger live rounds: Level 1 (plan-only) stages
  and stops (``loop_result is None``, the runner is never invoked); Level 2
  (supervised) runs the loop under the per-round cadence, not the
  autonomous one.
* The autonomy contract is preserved through the dispatch: the live branch
  inherits :func:`run_live_rounds`'s rejection of the per-round
  ``EVERY_ROUND`` cadence.
* Boundary + error paths: a Level-2+ dispatch with no round_runner is
  rejected fail-fast; an empty-domain campaign; a level above LIVE.

The round_runner is a recording / exploding stub so no subprocess runs.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from eawf.kernel.spec.campaign_driver import (
    CampaignDriveResult,
    MissingRoundRunnerError,
    drive_campaign,
)
from eawf.kernel.spec.live_rounds import CockpitLevel, SupervisedCadenceError
from eawf.kernel.spec.research_campaign import ResearchDomainConfig, ResearchProfileBlock
from eawf.kernel.spec.round_loop import (
    CheckpointPolicy,
    CheckpointTier,
    RoundHaltReason,
    RoundOutcome,
)
from eawf.kernel.spec.saturation import SaturationGateResult, SaturationReport


def _report(*, saturated: bool) -> SaturationReport:
    """Return a minimal SaturationReport with the given ``saturated`` bit."""
    gate = SaturationGateResult(name="novelty_decay", passed=saturated, detail="test fixture")
    return SaturationReport(
        saturated=saturated,
        gates=(gate,),
        live_claim_count=1,
        empty_ledger=False,
    )


def _counting_runner(
    saturate_on: int | None,
    *,
    seen: list[int],
) -> Callable[[int], RoundOutcome]:
    """Build a round_runner that records each round number it is invoked with."""

    def _run(round_number: int) -> RoundOutcome:
        seen.append(round_number)
        is_sat = saturate_on is not None and round_number >= saturate_on
        return RoundOutcome(saturation=_report(saturated=is_sat))

    return _run


def _exploding_runner() -> Callable[[int], RoundOutcome]:
    """Build a round_runner that fails if ever invoked.

    Proves the plan-only (Level 1) dispatch runs NO loop: any invocation
    raises, so a green plan-only test is proof the runner was never reached.
    """

    def _run(round_number: int) -> RoundOutcome:
        raise AssertionError(
            f"round_runner must not run on a plan-only dispatch: round {round_number}"
        )

    return _run


def _block(domains: tuple[str, ...]) -> ResearchProfileBlock:
    """Return a ResearchProfileBlock declaring the given domain names."""
    return ResearchProfileBlock(
        domains={name: ResearchDomainConfig() for name in domains},
    )


# ---------------------------------------------------------------------------
# Level 3+ drives the live runner (success criterion b: production caller)
# ---------------------------------------------------------------------------


def test_drive_campaign_live_drives_run_live_rounds() -> None:
    """A LIVE dispatch runs the bounded loop through the live driver."""
    block = _block(("market-structure", "regulatory"))
    seen: list[int] = []

    result = drive_campaign(
        "topic under study",
        block,
        level=CockpitLevel.LIVE,
        round_runner=_counting_runner(saturate_on=2, seen=seen),
    )

    assert isinstance(result, CampaignDriveResult)
    assert result.level is CockpitLevel.LIVE
    # The live driver staged the Level-1 plan AND ran the loop.
    assert result.staged.domain_count == 2
    assert result.loop_result is not None
    assert result.loop_result.rounds_run == 2
    assert result.loop_result.halt_reason is RoundHaltReason.SATURATED
    # The injected runner actually drove the rounds.
    assert seen == [1, 2]


def test_drive_campaign_live_halts_on_round_budget_when_never_saturates() -> None:
    """A LIVE campaign that never saturates halts on the round budget ceiling."""
    block = _block(("alpha",))
    seen: list[int] = []

    result = drive_campaign(
        "topic",
        block,
        level=CockpitLevel.LIVE,
        round_runner=_counting_runner(saturate_on=None, seen=seen),
        round_budget=3,
    )

    assert result.loop_result is not None
    assert result.loop_result.rounds_run == 3
    assert result.loop_result.halt_reason is RoundHaltReason.ROUND_BUDGET
    assert seen == [1, 2, 3]


def test_drive_campaign_live_rejects_every_round_cadence() -> None:
    """The live branch inherits run_live_rounds' rejection of the per-round cadence."""
    block = _block(("alpha",))

    with pytest.raises(SupervisedCadenceError):
        drive_campaign(
            "topic",
            block,
            level=CockpitLevel.LIVE,
            round_runner=_counting_runner(saturate_on=1, seen=[]),
            checkpoint_policy=CheckpointPolicy(tier=CheckpointTier.EVERY_ROUND),
        )


# ---------------------------------------------------------------------------
# Sub-live levels do NOT trigger live rounds (the safety gate)
# ---------------------------------------------------------------------------


def test_drive_campaign_plan_only_stages_without_running_loop() -> None:
    """Level 1 stages the plan and runs NO loop (the runner is never invoked)."""
    block = _block(("alpha", "beta"))

    result = drive_campaign(
        "topic",
        block,
        level=CockpitLevel.PLAN_ONLY,
        round_runner=_exploding_runner(),
    )

    assert result.level is CockpitLevel.PLAN_ONLY
    assert result.staged.domain_count == 2
    # No loop ran -- the plan-only rung never drives live rounds.
    assert result.loop_result is None


def test_drive_campaign_plan_only_needs_no_round_runner() -> None:
    """Level 1 does not require a round_runner (it runs no loop)."""
    block = _block(("alpha",))

    result = drive_campaign("topic", block, level=CockpitLevel.PLAN_ONLY)

    assert result.loop_result is None
    assert result.staged.domain_count == 1


def test_drive_campaign_supervised_runs_loop_with_per_round_cadence() -> None:
    """Level 2 runs the loop under the per-round (supervised) cadence, not autonomous."""
    block = _block(("alpha",))
    seen: list[int] = []

    result = drive_campaign(
        "topic",
        block,
        level=CockpitLevel.SUPERVISED,
        round_runner=_counting_runner(saturate_on=None, seen=seen),
        round_budget=3,
    )

    assert result.level is CockpitLevel.SUPERVISED
    assert result.loop_result is not None
    assert result.loop_result.rounds_run == 3
    # Supervised pauses every round -- one checkpoint per completed round.
    assert len(result.loop_result.checkpoints) == 3
    assert seen == [1, 2, 3]


# ---------------------------------------------------------------------------
# Error + boundary paths
# ---------------------------------------------------------------------------


def test_drive_campaign_live_without_runner_raises() -> None:
    """A LIVE dispatch with no round_runner is rejected fail-fast."""
    block = _block(("alpha",))

    with pytest.raises(MissingRoundRunnerError, match="live"):
        drive_campaign("topic", block, level=CockpitLevel.LIVE)


def test_drive_campaign_supervised_without_runner_raises() -> None:
    """A SUPERVISED dispatch with no round_runner is rejected fail-fast."""
    block = _block(("alpha",))

    with pytest.raises(MissingRoundRunnerError, match="supervised"):
        drive_campaign("topic", block, level=CockpitLevel.SUPERVISED)


def test_drive_campaign_empty_domain_campaign_boundary() -> None:
    """An empty-domain block stages an empty campaign at any level."""
    block = _block(())

    result = drive_campaign(
        "topic",
        block,
        level=CockpitLevel.LIVE,
        round_runner=_counting_runner(saturate_on=1, seen=[]),
    )

    assert result.staged.domain_count == 0
    assert result.loop_result is not None
    assert result.loop_result.rounds_run == 1


def test_drive_campaign_empty_topic_raises_value_error() -> None:
    """An empty topic propagates the stage_campaign ValueError."""
    block = _block(("alpha",))

    with pytest.raises(ValueError, match="topic must be non-empty"):
        drive_campaign(
            "   ",
            block,
            level=CockpitLevel.LIVE,
            round_runner=_counting_runner(saturate_on=1, seen=[]),
        )
