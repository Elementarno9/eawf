"""Live (cockpit Level 3+) research-round driver over a staged campaign.

The research-campaign autonomy ladder has three rungs this module names
explicitly (:class:`CockpitLevel`):

* **Level 1 -- plan-only.** :func:`eawf.kernel.spec.research_campaign.stage_campaign`
  expands a topic + a :class:`~eawf.kernel.spec.research_campaign.ResearchProfileBlock`
  into a :class:`~eawf.kernel.spec.research_campaign.StagedCampaign` of
  read-only dispatch envelopes and stops. No subprocess, no round loop.
* **Level 2 -- supervised.** The bounded round loop
  (:func:`eawf.kernel.spec.round_loop.run_round_loop`) sequences rounds but
  pauses for operator review at a tight cadence
  (:attr:`~eawf.kernel.spec.round_loop.CheckpointTier.EVERY_ROUND`).
* **Level 3+ -- live / autonomous.** The same bounded loop runs the rounds
  WITHOUT a per-round operator pause -- a looser checkpoint cadence
  (``EVERY_N`` / ``ON_HALT`` / ``NEVER``) surfaces progress without
  blocking each turn. This module's :func:`run_live_rounds` is that rung:
  it composes the Level-1 plan with the bounded loop and runs the rounds to
  a halt reason, honoring the checkpoint policy.

What "live" does NOT mean here -- the landmine
----------------------------------------------
"Live" is the autonomy level, NOT a license to spawn from this module.
:func:`run_round_loop` is a **pure** driver (it allocates no subprocess,
opens no runtime session, imports no adapter), and this module keeps that
property: the per-round survey work is injected as a ``round_runner``
callback. Production binds a runner that actually convenes the round's
survey through the daemon dispatch path; a test binds a recording stub. So
:func:`run_live_rounds` is unit-testable end to end without a runtime --
the same injected-callback discipline the staged Level-1 runner and the
bounded loop already follow.

The level gate
--------------
:func:`run_live_rounds` REQUIRES a live level (Level 3 or higher). A
caller asking it to drive a Level-1 / Level-2 campaign is a category error
-- Level 1 is the plan-only stager and Level 2 is the supervised loop with
its own per-round pause -- so the driver rejects a sub-live level fail-fast
with :class:`SubLiveLevelError` rather than silently downgrading the
autonomy contract.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import IntEnum

from eawf.kernel.spec.research_campaign import (
    ResearchProfileBlock,
    StagedCampaign,
    stage_campaign,
)
from eawf.kernel.spec.round_loop import (
    DEFAULT_ROUND_BUDGET,
    CheckpointPolicy,
    CheckpointTier,
    RoundLoopResult,
    RoundOutcome,
    run_round_loop,
)

logger = logging.getLogger(__name__)


class CockpitLevel(IntEnum):
    """The research-campaign autonomy ladder rung a driver runs at.

    An :class:`enum.IntEnum` so ``level >= CockpitLevel.LIVE`` reads as the
    natural "live or higher" comparison the level gate makes. The members
    are the named rungs the module docstring describes; higher value = more
    autonomy.

    Members:
        PLAN_ONLY: Level 1 -- the plan-only stager
            (:func:`eawf.kernel.spec.research_campaign.stage_campaign`).
            No round loop runs at this level.
        SUPERVISED: Level 2 -- the bounded round loop with a per-round
            operator pause (the tight
            :attr:`~eawf.kernel.spec.round_loop.CheckpointTier.EVERY_ROUND`
            cadence).
        LIVE: Level 3 -- the live / autonomous round loop: rounds run
            without a per-round pause, surfacing progress at a looser
            checkpoint cadence. The floor :func:`run_live_rounds` requires.
    """

    PLAN_ONLY = 1
    SUPERVISED = 2
    LIVE = 3


class SubLiveLevelError(ValueError):
    """Raised when :func:`run_live_rounds` is asked to drive a sub-live level.

    The live-round driver requires a :attr:`CockpitLevel.LIVE` (or higher)
    level. A Level-1 (plan-only) or Level-2 (supervised) request is a
    category error -- those rungs have their own runners -- so the driver
    refuses it fail-fast rather than silently downgrading to the autonomous
    contract.
    """


class SupervisedCadenceError(ValueError):
    """Raised when a live run is handed the supervised per-round cadence.

    Level 3+ is autonomous: it surfaces progress without pausing every
    round. Pairing it with the
    :attr:`~eawf.kernel.spec.round_loop.CheckpointTier.EVERY_ROUND` cadence
    contradicts the autonomy contract (that cadence is the Level-2
    supervised rung), so the driver rejects the combination rather than
    running an "autonomous" loop that blocks on every turn.
    """


def _default_live_policy() -> CheckpointPolicy:
    """Return the default checkpoint policy for a live run.

    Live rounds default to the
    :attr:`~eawf.kernel.spec.round_loop.CheckpointTier.ON_HALT` cadence:
    surface the terminal halt for review without pausing every round. That
    is the loosest non-silent cadence, the natural default for an
    autonomous run the operator reviews on convergence rather than babysits.
    """
    return CheckpointPolicy(tier=CheckpointTier.ON_HALT)


def run_live_rounds(
    topic: str,
    block: ResearchProfileBlock,
    round_runner: Callable[[int], RoundOutcome],
    *,
    level: CockpitLevel = CockpitLevel.LIVE,
    round_budget: int = DEFAULT_ROUND_BUDGET,
    checkpoint_policy: CheckpointPolicy | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> tuple[StagedCampaign, RoundLoopResult]:
    """Drive a live (Level 3+) research campaign: stage the plan, then run rounds.

    Composes the two existing halves of the campaign surface:

    1. Stage the Level-1 plan via
       :func:`eawf.kernel.spec.research_campaign.stage_campaign` -- expand
       *topic* + *block* into a
       :class:`~eawf.kernel.spec.research_campaign.StagedCampaign` of
       read-only dispatch envelopes (the per-domain fan-out).
    2. Drive the bounded round loop via
       :func:`eawf.kernel.spec.round_loop.run_round_loop` with the injected
       *round_runner*, honoring *round_budget* (the hard ceiling) and
       *checkpoint_policy* (the operator-review cadence). The loop halts on
       the FIRST of saturation or budget and records the single
       :class:`~eawf.kernel.spec.round_loop.RoundHaltReason`.

    The driver itself spawns nothing: *round_runner* is the injected seam
    (production convenes the round's survey, a test records the calls), so
    the whole live path is unit-testable without a runtime -- the same
    purity the staged stager and the bounded loop already hold.

    Args:
        topic: The campaign topic fanned out across the block's domains.
        block: The typed ``research:`` profile block (per-domain depth /
            focus config) the Level-1 plan is staged from.
        round_runner: The per-round survey callback the bounded loop
            invokes once per round with the 1-based round number; returns
            the round's :class:`~eawf.kernel.spec.round_loop.RoundOutcome`.
            Never a real subprocess under test.
        level: The autonomy level to run at. MUST be
            :attr:`CockpitLevel.LIVE` or higher -- a plan-only / supervised
            level is rejected (those rungs have their own runners).
            Defaults to :attr:`CockpitLevel.LIVE`.
        round_budget: Hard ceiling on rounds, forwarded to
            :func:`run_round_loop`. Must be ``>= 1``.
        checkpoint_policy: The operator-review cadence, forwarded to
            :func:`run_round_loop`. ``None`` defaults to the
            :attr:`~eawf.kernel.spec.round_loop.CheckpointTier.ON_HALT`
            cadence (surface only the terminal halt -- the autonomous
            default). A policy whose tier is
            :attr:`~eawf.kernel.spec.round_loop.CheckpointTier.EVERY_ROUND`
            is rejected: that per-round pause is the Level-2 supervised
            cadence, not an autonomous one.
        should_continue: Optional between-rounds cancellation predicate,
            forwarded to :func:`run_round_loop`. ``False`` halts the run with
            :attr:`~eawf.kernel.spec.round_loop.RoundHaltReason.CANCELLED`
            before the next round spawns.

    Returns:
        A ``(staged_campaign, loop_result)`` pair: the Level-1 plan that was
        staged and the bounded-loop result (rounds run, halt reason,
        checkpoints, terminal saturation).

    Raises:
        SubLiveLevelError: When *level* is below :attr:`CockpitLevel.LIVE`.
        SupervisedCadenceError: When *checkpoint_policy* uses the
            per-round
            :attr:`~eawf.kernel.spec.round_loop.CheckpointTier.EVERY_ROUND`
            cadence (the supervised rung, not an autonomous one).
        ValueError: When *topic* is empty (propagated from
            :func:`stage_campaign`) or *round_budget* is below 1
            (propagated from :func:`run_round_loop`).
    """
    if level < CockpitLevel.LIVE:
        raise SubLiveLevelError(
            f"run_live_rounds requires a live level (>= {CockpitLevel.LIVE.value}), "
            f"got level={level.value!r}"
        )

    policy = checkpoint_policy if checkpoint_policy is not None else _default_live_policy()
    if policy.tier is CheckpointTier.EVERY_ROUND:
        raise SupervisedCadenceError(
            "live rounds reject the every_round checkpoint cadence: a per-round "
            "pause is the supervised (Level 2) rung, not an autonomous one"
        )

    staged = stage_campaign(topic, block)
    loop_result = run_round_loop(
        round_runner,
        round_budget=round_budget,
        checkpoint_policy=policy,
        should_continue=should_continue,
    )
    logger.info(
        f"run_live_rounds level={level.value} domains={staged.domain_count} "
        f"rounds={loop_result.rounds_run} halt={loop_result.halt_reason.value} "
        f"checkpoints={len(loop_result.checkpoints)} saturated={loop_result.saturated}"
    )
    return staged, loop_result


__all__ = [
    "CockpitLevel",
    "SubLiveLevelError",
    "SupervisedCadenceError",
    "run_live_rounds",
]
