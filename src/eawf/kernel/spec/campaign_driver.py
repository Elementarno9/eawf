"""Per-level research-campaign dispatcher: route a campaign by its cockpit level.

The autonomy ladder has three runners already built, one per rung, but
nothing that READS a :class:`~eawf.kernel.spec.live_rounds.CockpitLevel`
and dispatches to the right one. This module is that dispatcher:
:func:`drive_campaign` maps the level onto the matching runner so the
Level-3+ (live) branch actually reaches
:func:`~eawf.kernel.spec.live_rounds.run_live_rounds` instead of leaving
the live driver unreferenced.

The dispatch table -- one runner per rung
-----------------------------------------
* **Level 1 -- plan-only.** Stage the campaign and stop
  (:func:`~eawf.kernel.spec.research_campaign.stage_campaign`). No round
  loop runs; the returned :class:`CampaignDriveResult` carries
  ``loop_result=None``. A ``round_runner`` is neither required nor invoked.
* **Level 2 -- supervised.** Stage the plan, then drive the bounded round
  loop (:func:`~eawf.kernel.spec.round_loop.run_round_loop`) directly with
  the tight per-round
  :attr:`~eawf.kernel.spec.round_loop.CheckpointTier.EVERY_ROUND` cadence
  -- the supervised rung pauses for review every round.
* **Level 3+ -- live / autonomous.** Drive
  :func:`~eawf.kernel.spec.live_rounds.run_live_rounds`, which composes the
  Level-1 stage with the bounded loop under a looser (non-per-round)
  cadence. This is the branch that un-idles the live driver.

The safety gate is preserved end to end
----------------------------------------
:func:`run_live_rounds` REQUIRES a live level and raises
:class:`~eawf.kernel.spec.live_rounds.SubLiveLevelError` for a sub-live
one. This dispatcher never calls it below
:attr:`~eawf.kernel.spec.live_rounds.CockpitLevel.LIVE`: a Level-1 / Level-2
campaign takes the plan-only / supervised branch, so the live path fires
ONLY at Level 3+. A Level-2+ dispatch that lacks the required
``round_runner`` is rejected fail-fast (the supervised / live rungs run a
loop and so need the per-round survey callback).

Purity by injection
--------------------
The dispatcher spawns nothing itself. ``round_runner`` is the injected
per-round survey seam every runner already takes: production binds a
runner that convenes the round's survey through the daemon dispatch path;
a test binds a recording stub. So the whole per-level dispatch is
unit-testable without a runtime.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from eawf.kernel.spec.live_rounds import CockpitLevel, run_live_rounds
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


class MissingRoundRunnerError(ValueError):
    """Raised when a loop-running level is dispatched without a round runner.

    The supervised (Level 2) and live (Level 3+) rungs run a bounded round
    loop, which needs the per-round survey callback. Dispatching either
    without a ``round_runner`` is a category error -- there is no work to
    sequence -- so the dispatcher rejects it fail-fast rather than running
    an empty loop. The plan-only (Level 1) rung needs no runner and is
    unaffected.
    """


@dataclass(frozen=True)
class CampaignDriveResult:
    """Typed outcome of a per-level :func:`drive_campaign` dispatch.

    Carries the staged Level-1 plan (always present -- every level stages
    the plan first) and the bounded-loop result when a loop ran. The
    :attr:`loop_result` distinguishes the plan-only rung from the
    loop-running rungs: ``None`` for Level 1, a populated
    :class:`~eawf.kernel.spec.round_loop.RoundLoopResult` for Level 2+.

    Attributes:
        level: The :class:`~eawf.kernel.spec.live_rounds.CockpitLevel` the
            campaign was dispatched at -- recorded so a caller can confirm
            which rung ran.
        staged: The :class:`~eawf.kernel.spec.research_campaign.StagedCampaign`
            the Level-1 stager produced (the per-domain dispatch fan-out).
        loop_result: The
            :class:`~eawf.kernel.spec.round_loop.RoundLoopResult` from the
            bounded loop on Level 2+, or ``None`` on the plan-only Level 1
            where no loop ran.
    """

    level: CockpitLevel
    staged: StagedCampaign
    loop_result: RoundLoopResult | None = None


def _supervised_policy() -> CheckpointPolicy:
    """Return the supervised (Level 2) per-round checkpoint policy.

    Level 2 pauses for operator review after every round -- the tight
    :attr:`~eawf.kernel.spec.round_loop.CheckpointTier.EVERY_ROUND` cadence
    that distinguishes the supervised rung from the autonomous Level-3+
    one (which surfaces progress at a looser cadence).
    """
    return CheckpointPolicy(tier=CheckpointTier.EVERY_ROUND)


def drive_campaign(
    topic: str,
    block: ResearchProfileBlock,
    *,
    level: CockpitLevel,
    round_runner: Callable[[int], RoundOutcome] | None = None,
    round_budget: int = DEFAULT_ROUND_BUDGET,
    checkpoint_policy: CheckpointPolicy | None = None,
) -> CampaignDriveResult:
    """Dispatch a research campaign to the runner for its cockpit *level*.

    The per-level entry point: it reads *level* and routes the campaign to
    the matching rung's runner -- plan-only stage at Level 1, the bounded
    loop with a per-round pause at Level 2, and the live autonomous driver
    (:func:`~eawf.kernel.spec.live_rounds.run_live_rounds`) at Level 3+. The
    live branch is the one that un-idles the live driver; it fires ONLY at
    Level 3+, so a sub-live dispatch never reaches the autonomous path.

    The dispatcher spawns nothing itself: *round_runner* is the injected
    per-round survey seam (production convenes the round's survey, a test
    records the calls), so the whole dispatch is unit-testable without a
    runtime.

    Args:
        topic: The campaign topic fanned out across the block's domains.
        block: The typed ``research:`` profile block the Level-1 plan is
            staged from.
        level: The :class:`~eawf.kernel.spec.live_rounds.CockpitLevel` to
            dispatch at. Selects the runner: ``PLAN_ONLY`` stages only,
            ``SUPERVISED`` runs the loop with a per-round pause, ``LIVE``
            (or higher) runs the autonomous driver.
        round_runner: The per-round survey callback the bounded loop
            invokes (Level 2+ only). REQUIRED for a loop-running level;
            unused (and may be ``None``) at the plan-only Level 1.
        round_budget: Hard ceiling on rounds for the loop-running rungs.
            Forwarded to the loop / live driver. Must be ``>= 1``.
        checkpoint_policy: The operator-review cadence for the live
            (Level 3+) branch only. ``None`` lets
            :func:`run_live_rounds` apply its autonomous default. Ignored
            on Level 1 (no loop) and Level 2 (the supervised per-round
            cadence is fixed).

    Returns:
        A :class:`CampaignDriveResult` carrying the level, the staged plan,
        and -- for Level 2+ -- the bounded-loop result.

    Raises:
        MissingRoundRunnerError: When *level* is Level 2 or higher and
            *round_runner* is ``None`` (a loop-running rung needs the
            per-round survey callback).
        ValueError: When *topic* is empty (propagated from
            :func:`stage_campaign`) or *round_budget* is below 1
            (propagated from the loop / live driver).
    """
    if level >= CockpitLevel.LIVE:
        if round_runner is None:
            raise MissingRoundRunnerError(
                f"a live (>= {CockpitLevel.LIVE.value}) campaign needs a round_runner "
                f"to drive its rounds, got level={level.value!r}"
            )
        staged, loop_result = run_live_rounds(
            topic,
            block,
            round_runner,
            level=level,
            round_budget=round_budget,
            checkpoint_policy=checkpoint_policy,
        )
        logger.info(
            f"drive_campaign level={level.value} branch=live "
            f"domains={staged.domain_count} rounds={loop_result.rounds_run}"
        )
        return CampaignDriveResult(level=level, staged=staged, loop_result=loop_result)

    if level == CockpitLevel.SUPERVISED:
        if round_runner is None:
            raise MissingRoundRunnerError(
                "a supervised (Level 2) campaign needs a round_runner to drive its "
                "rounds, got level=2"
            )
        staged = stage_campaign(topic, block)
        loop_result = run_round_loop(
            round_runner,
            round_budget=round_budget,
            checkpoint_policy=_supervised_policy(),
        )
        logger.info(
            f"drive_campaign level={level.value} branch=supervised "
            f"domains={staged.domain_count} rounds={loop_result.rounds_run}"
        )
        return CampaignDriveResult(level=level, staged=staged, loop_result=loop_result)

    staged = stage_campaign(topic, block)
    logger.info(
        f"drive_campaign level={level.value} branch=plan_only domains={staged.domain_count}"
    )
    return CampaignDriveResult(level=level, staged=staged, loop_result=None)


__all__ = [
    "CampaignDriveResult",
    "MissingRoundRunnerError",
    "drive_campaign",
]
