"""Tests for :mod:`eawf.kernel.spec.live_rounds` (live cockpit Level 3+).

Pins the live (autonomous) research-round driver that composes the
Level-1 plan stager with the bounded round loop:

1. The driver stages the Level-1 plan (one dispatch per configured domain)
   AND runs the bounded loop -- a ``(StagedCampaign, RoundLoopResult)`` pair.
2. It runs N rounds through the injected ``round_runner`` and honors the
   checkpoint policy (the recorded checkpoints match the cadence).
3. It honors the loop halt reasons: saturation (the campaign goes dry) and
   the round budget (the campaign never saturates).
4. The level gate: a sub-live level (plan-only / supervised) is rejected;
   a live (or higher) level runs.
5. The autonomy-cadence guard: the per-round ``EVERY_ROUND`` cadence is
   rejected for a live run (that pause is the supervised rung).
6. Boundary cases: a one-round budget, an empty-domain campaign.
7. The driver is pure: the injected runner is the only round work, no
   subprocess is spawned.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from eawf.kernel.spec.live_rounds import (
    CockpitLevel,
    SubLiveLevelError,
    SupervisedCadenceError,
    run_live_rounds,
)
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


def _runner(saturate_on: int | None) -> Callable[[int], RoundOutcome]:
    """Build a round_runner that saturates on round ``saturate_on`` (or never)."""

    def _run(round_number: int) -> RoundOutcome:
        is_sat = saturate_on is not None and round_number >= saturate_on
        return RoundOutcome(saturation=_report(saturated=is_sat))

    return _run


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


def _block(domains: tuple[str, ...]) -> ResearchProfileBlock:
    """Return a ResearchProfileBlock declaring the given domain names."""
    return ResearchProfileBlock(
        domains={name: ResearchDomainConfig() for name in domains},
    )


# ---------------------------------------------------------------------------
# Composition: stage the plan + run the loop (success criterion c)
# ---------------------------------------------------------------------------


def test_run_live_rounds_stages_plan_and_runs_loop() -> None:
    """The driver returns the staged Level-1 plan AND the bounded-loop result."""
    block = _block(("market-structure", "regulatory"))

    staged, result = run_live_rounds(
        "topic under study",
        block,
        _runner(saturate_on=2),
    )

    # Level-1 plan: one dispatch per configured domain, plan-only flag fixed.
    assert staged.spawned is False
    assert staged.domain_count == 2
    assert [d.domain for d in staged.dispatches] == ["market-structure", "regulatory"]
    # Bounded loop ran to saturation on round 2.
    assert result.rounds_run == 2
    assert result.halt_reason is RoundHaltReason.SATURATED
    assert result.saturated is True


def test_run_live_rounds_runs_n_rounds_through_injected_runner() -> None:
    """The driver drives the injected runner once per round, in order."""
    block = _block(("alpha",))
    seen: list[int] = []

    _staged, result = run_live_rounds(
        "topic",
        block,
        _counting_runner(saturate_on=3, seen=seen),
        round_budget=10,
    )

    assert seen == [1, 2, 3]
    assert result.rounds_run == 3


# ---------------------------------------------------------------------------
# Halt reasons honored (success criterion c continued)
# ---------------------------------------------------------------------------


def test_run_live_rounds_halts_on_round_budget_when_never_saturates() -> None:
    """A campaign that never saturates halts on the round budget (the hard ceiling)."""
    block = _block(("alpha",))

    _staged, result = run_live_rounds(
        "topic",
        block,
        _runner(saturate_on=None),
        round_budget=4,
    )

    assert result.rounds_run == 4
    assert result.halt_reason is RoundHaltReason.ROUND_BUDGET
    assert result.saturated is False


def test_run_live_rounds_saturation_wins_halt_precedence() -> None:
    """A round that both saturates and exhausts the budget reports SATURATED."""
    block = _block(("alpha",))

    _staged, result = run_live_rounds(
        "topic",
        block,
        _runner(saturate_on=3),
        round_budget=3,
    )

    assert result.rounds_run == 3
    assert result.halt_reason is RoundHaltReason.SATURATED


# ---------------------------------------------------------------------------
# Checkpoint policy honored (success criterion: checkpoint policy)
# ---------------------------------------------------------------------------


def test_run_live_rounds_honors_every_n_checkpoint_cadence() -> None:
    """An EVERY_N policy records a checkpoint once every interval rounds."""
    block = _block(("alpha",))
    policy = CheckpointPolicy(tier=CheckpointTier.EVERY_N, interval=2)

    _staged, result = run_live_rounds(
        "topic",
        block,
        _runner(saturate_on=None),
        round_budget=5,
        checkpoint_policy=policy,
    )

    # Rounds 2 and 4 fire the EVERY_N checkpoint; round 5 fires too because
    # it is the terminal halt round under the bounded loop's recording.
    fired = [cp.round_number for cp in result.checkpoints]
    assert 2 in fired
    assert 4 in fired


def test_run_live_rounds_default_policy_records_only_terminal_halt() -> None:
    """The default (ON_HALT) cadence records a single terminal checkpoint."""
    block = _block(("alpha",))

    _staged, result = run_live_rounds(
        "topic",
        block,
        _runner(saturate_on=None),
        round_budget=3,
    )

    assert len(result.checkpoints) == 1
    assert result.checkpoints[0].round_number == 3
    assert result.checkpoints[0].terminal is True


def test_run_live_rounds_never_cadence_records_no_checkpoints() -> None:
    """The NEVER cadence runs fully autonomously with no recorded checkpoints."""
    block = _block(("alpha",))
    policy = CheckpointPolicy(tier=CheckpointTier.NEVER)

    _staged, result = run_live_rounds(
        "topic",
        block,
        _runner(saturate_on=2),
        checkpoint_policy=policy,
    )

    assert result.checkpoints == []


# ---------------------------------------------------------------------------
# The level gate (success criterion: cockpit Level 3+ wiring)
# ---------------------------------------------------------------------------


def test_run_live_rounds_rejects_plan_only_level() -> None:
    """A Level-1 (plan-only) request is rejected -- that rung is the stager."""
    block = _block(("alpha",))

    with pytest.raises(SubLiveLevelError, match="requires a live level"):
        run_live_rounds(
            "topic",
            block,
            _runner(saturate_on=1),
            level=CockpitLevel.PLAN_ONLY,
        )


def test_run_live_rounds_rejects_supervised_level() -> None:
    """A Level-2 (supervised) request is rejected -- that rung has its own runner."""
    block = _block(("alpha",))

    with pytest.raises(SubLiveLevelError):
        run_live_rounds(
            "topic",
            block,
            _runner(saturate_on=1),
            level=CockpitLevel.SUPERVISED,
        )


def test_run_live_rounds_accepts_live_level() -> None:
    """An explicit live level runs (the floor the driver requires)."""
    block = _block(("alpha",))

    _staged, result = run_live_rounds(
        "topic",
        block,
        _runner(saturate_on=1),
        level=CockpitLevel.LIVE,
    )

    assert result.rounds_run == 1
    assert result.halt_reason is RoundHaltReason.SATURATED


def test_cockpit_level_ordering_is_ascending_autonomy() -> None:
    """The cockpit ladder orders plan-only < supervised < live by value."""
    assert CockpitLevel.PLAN_ONLY < CockpitLevel.SUPERVISED < CockpitLevel.LIVE


# ---------------------------------------------------------------------------
# Autonomy-cadence guard
# ---------------------------------------------------------------------------


def test_run_live_rounds_rejects_every_round_supervised_cadence() -> None:
    """A live run handed the per-round EVERY_ROUND pause is rejected (that is Level 2)."""
    block = _block(("alpha",))
    policy = CheckpointPolicy(tier=CheckpointTier.EVERY_ROUND)

    with pytest.raises(SupervisedCadenceError, match="every_round"):
        run_live_rounds(
            "topic",
            block,
            _runner(saturate_on=1),
            checkpoint_policy=policy,
        )


# ---------------------------------------------------------------------------
# Boundary cases + error paths
# ---------------------------------------------------------------------------


def test_run_live_rounds_one_round_budget() -> None:
    """A one-round budget (boundary) runs exactly one round."""
    block = _block(("alpha",))

    _staged, result = run_live_rounds(
        "topic",
        block,
        _runner(saturate_on=None),
        round_budget=1,
    )

    assert result.rounds_run == 1
    assert result.halt_reason is RoundHaltReason.ROUND_BUDGET


def test_run_live_rounds_empty_domain_campaign_stages_no_dispatches() -> None:
    """An empty-domain block stages zero dispatches but still runs the loop."""
    block = _block(())

    staged, result = run_live_rounds(
        "topic",
        block,
        _runner(saturate_on=1),
    )

    assert staged.domain_count == 0
    assert result.rounds_run == 1


def test_run_live_rounds_empty_topic_raises() -> None:
    """An empty topic fails fast (propagated from the Level-1 stager)."""
    block = _block(("alpha",))

    with pytest.raises(ValueError, match="campaign topic must be non-empty"):
        run_live_rounds("   ", block, _runner(saturate_on=1))


def test_run_live_rounds_zero_round_budget_raises() -> None:
    """A zero round budget fails fast (propagated from the bounded loop)."""
    block = _block(("alpha",))

    with pytest.raises(ValueError, match="round_budget must be >= 1"):
        run_live_rounds("topic", block, _runner(saturate_on=1), round_budget=0)
