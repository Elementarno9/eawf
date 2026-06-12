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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import ValidationError

from eawf.kernel.spec.live_rounds import CockpitLevel, run_live_rounds
from eawf.kernel.spec.research_campaign import (
    ResearchProfileBlock,
    StagedCampaign,
    StagedDispatch,
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
from eawf.kernel.spec.saturation import SaturationReport
from eawf.kernel.store.kinds.agent_report import ResearcherReportBody

if TYPE_CHECKING:
    from datetime import datetime

    from eawf.kernel.state.models import Claim, OpenQuestion

logger = logging.getLogger(__name__)

#: Type of the injected per-dispatch spawn seam the production round runner
#: drives. Production binds a closure that spawns a real researcher session via
#: the daemon ``agent.dispatch`` path and returns the spawned agent's decoded
#: ``agent_end`` body; a test binds a recording stub returning a fixture body.
#: The seam is the dispatcher's only contact with the runtime -- the driver
#: itself spawns nothing, so the whole binding is unit-testable behind a stub.
DispatchSpawner = Callable[[StagedDispatch], Mapping[str, object]]


class ResearcherDispatchError(ValueError):
    """Raised when a spawned researcher's ``agent_end`` body fails to parse.

    The production round runner converts each
    :class:`~eawf.kernel.spec.research_campaign.StagedDispatch` into a real
    spawned researcher session and validates the returned ``agent_end`` body
    into a typed :class:`~eawf.kernel.store.kinds.agent_report.ResearcherReportBody`.
    A body that does not validate (a non-researcher role, a missing required
    field, an extra key) is a hard error -- the campaign cannot fold a
    malformed round into findings -- so the runner surfaces this typed error
    rather than silently dropping the dispatch and producing an empty round
    (which would read as a converged campaign). The message names which
    dispatch domain failed.
    """


@dataclass(frozen=True)
class RoundFindings:
    """Typed findings parsed from one round's spawned researcher dispatches.

    The production round runner produces one of these per round: it spawns a
    researcher session per :class:`StagedDispatch`, parses each returned
    ``agent_end`` body into a :class:`ResearcherReportBody`, and folds the
    per-domain bodies into this record. The driver reads :attr:`saturation`
    (via the injected reducer) to decide whether to halt; a downstream pass
    (W02 +) reconciles the findings into the state-resident Claim /
    OpenQuestion ledgers.

    Attributes:
        round_number: The 1-based round index these findings were gathered
            in (matches the ``round_runner`` callback argument).
        bodies: The validated per-domain researcher bodies, in dispatch
            (sorted-domain) order -- one per :class:`StagedDispatch` that was
            spawned this round.
        domains: The domain names spawned this round, parallel to
            :attr:`bodies`.
    """

    round_number: int
    bodies: tuple[ResearcherReportBody, ...] = ()
    domains: tuple[str, ...] = ()

    @property
    def finding_lines(self) -> tuple[str, ...]:
        """Every findings line across the round's bodies, in dispatch order."""
        return tuple(line for body in self.bodies for line in body.findings)


def parse_researcher_findings(domain: str, raw: Mapping[str, object]) -> ResearcherReportBody:
    """Validate a spawned researcher's ``agent_end`` body, or raise typed.

    The seam between the live spawn path and the typed findings ledger: the
    spawned researcher's decoded ``agent_end`` body is forced through
    :class:`~eawf.kernel.store.kinds.agent_report.ResearcherReportBody`
    (the narrow researcher schema, not the whole report union) so a body that
    is not a researcher report -- or omits the required ``question`` /
    ``recommendation`` -- is rejected rather than folded into the round as
    empty findings.

    Args:
        domain: The research domain the dispatch covered (named in the error
            so a malformed round attributes to the failing dispatch).
        raw: The spawned agent's decoded ``agent_end`` body.

    Returns:
        The validated :class:`ResearcherReportBody`.

    Raises:
        ResearcherDispatchError: When *raw* does not validate against the
            researcher report-body schema. A failed parse is a hard error,
            not a silent empty round.
    """
    try:
        return ResearcherReportBody.model_validate(raw)
    except ValidationError as exc:
        raise ResearcherDispatchError(
            f"researcher dispatch for domain {domain!r} returned an unparseable "
            f"agent_end body: {exc.error_count()} error(s)"
        ) from exc


#: Type of the injected per-round saturation reducer the production round
#: runner consults. Production binds a closure that reduces the live Claim /
#: OpenQuestion ledgers (after the round's findings reconcile) into a
#: :class:`~eawf.kernel.spec.saturation.SaturationReport`; a test binds a stub
#: returning a fixed report so the loop's halt arithmetic stays unit-testable.
RoundSaturationReducer = Callable[[RoundFindings], SaturationReport]


@dataclass
class _RoundRecorder:
    """Mutable per-campaign record of the rounds the production runner drove.

    The production :func:`build_round_runner` appends one :class:`RoundFindings`
    per round so a caller (the ``research.run`` RPC) can persist the per-round
    findings after :func:`run_round_loop` returns the terminal result. Kept
    package-private: callers reach the rounds via the
    :func:`build_round_runner` return tuple, not this type directly.
    """

    rounds: list[RoundFindings] = field(default_factory=list)


def build_round_runner(
    staged: StagedCampaign,
    spawn: DispatchSpawner,
    saturation: RoundSaturationReducer,
) -> tuple[Callable[[int], RoundOutcome], list[RoundFindings]]:
    """Build the production round runner that spawns + parses one round.

    Returns the per-round :class:`RoundOutcome` callback the bounded loop
    (:func:`~eawf.kernel.spec.round_loop.run_round_loop`) drives, plus the
    growing list of :class:`RoundFindings` the runner records as it goes. Each
    round the callback:

    1. converts every :class:`StagedDispatch` in *staged* into a real spawned
       researcher session via the injected *spawn* seam (production binds the
       daemon ``agent.dispatch`` path; a test binds a stub), and
    2. parses each returned ``agent_end`` body into a typed
       :class:`ResearcherReportBody` via :func:`parse_researcher_findings` --
       a body that fails to parse raises :class:`ResearcherDispatchError`
       rather than producing a silent empty round, and
    3. folds the parsed bodies into a :class:`RoundFindings` record, appends it
       to the returned list, and reduces it to the round's
       :class:`~eawf.kernel.spec.saturation.SaturationReport` via the injected
       *saturation* reducer.

    The runner spawns nothing itself: *spawn* is the only contact with the
    runtime, so the whole binding is unit-testable behind a recording stub --
    the same injected-callback discipline the staged stager and the bounded
    loop already follow.

    Args:
        staged: The Level-1 staged campaign whose per-domain dispatches the
            runner spawns each round.
        spawn: The per-dispatch spawn seam (production: ``agent.dispatch``; a
            test: a recording stub returning a fixture ``agent_end`` body).
        saturation: The per-round reducer that scores whether the campaign is
            dry after this round's findings.

    Returns:
        A ``(round_runner, rounds)`` pair: the per-round callback for
        :func:`run_round_loop`, and the list of :class:`RoundFindings` the
        runner appends to as the loop drives it.

    Raises:
        ResearcherDispatchError: Propagated from the callback when any of the
            round's spawned researcher bodies fails to parse.
    """
    recorder = _RoundRecorder()

    def _round_runner(round_number: int) -> RoundOutcome:
        bodies: list[ResearcherReportBody] = []
        domains: list[str] = []
        for dispatch in staged.dispatches:
            raw = spawn(dispatch)
            bodies.append(parse_researcher_findings(dispatch.domain, raw))
            domains.append(dispatch.domain)
        findings = RoundFindings(
            round_number=round_number,
            bodies=tuple(bodies),
            domains=tuple(domains),
        )
        recorder.rounds.append(findings)
        report = saturation(findings)
        logger.info(
            f"build_round_runner round={round_number} dispatches={len(bodies)} "
            f"findings={len(findings.finding_lines)} saturated={report.saturated}"
        )
        return RoundOutcome(saturation=report)

    return _round_runner, recorder.rounds


def ledger_saturation_reducer(
    claims_for_round: Callable[[RoundFindings], Sequence[Claim]],
    questions_for_round: Callable[[RoundFindings], Sequence[OpenQuestion]],
    *,
    now_for_round: Callable[[RoundFindings], datetime],
) -> RoundSaturationReducer:
    """Build a saturation reducer over the post-round Claim / question ledgers.

    Composes the four-gate :meth:`SaturationReport.reduce` over the live
    ledgers as they stand after a round's findings reconcile. The callbacks
    inject the ledgers + clock so the reducer stays pure with respect to its
    own body (the round runner owns the reconcile; this only reads the result).

    Args:
        claims_for_round: Returns the Claim ledger to score for a given round.
        questions_for_round: Returns the OpenQuestion ledger to score.
        now_for_round: Returns the reference instant the novelty window is
            measured back from for the round.

    Returns:
        A :class:`RoundSaturationReducer` the production round runner consults.
    """

    def _reduce(findings: RoundFindings) -> SaturationReport:
        return SaturationReport.reduce(
            claims_for_round(findings),
            questions_for_round(findings),
            now=now_for_round(findings),
        )

    return _reduce


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
    "DispatchSpawner",
    "MissingRoundRunnerError",
    "ResearcherDispatchError",
    "RoundFindings",
    "RoundSaturationReducer",
    "build_round_runner",
    "drive_campaign",
    "ledger_saturation_reducer",
    "parse_researcher_findings",
]
