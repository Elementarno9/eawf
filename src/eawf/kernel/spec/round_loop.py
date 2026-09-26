"""Bounded research-campaign round loop with checkpoint-policy tiers.

A research campaign converges by running survey *rounds*: each round
appends :class:`~eawf.kernel.state.models.Claim` /
:class:`~eawf.kernel.state.models.OpenQuestion` rows, then the four-gate
:class:`~eawf.kernel.spec.saturation.SaturationReport` reducer asks the
single question "is the campaign dry?". This module owns the **driver**
that runs those rounds: a bounded loop that halts on the FIRST of two
conditions — the campaign saturated, or the round budget is spent — and a
:class:`CheckpointPolicy` that gates how often the loop pauses for
operator review.

Live round execution is NOT wired here. The driver is a **pure** loop in
the same spirit as the W14 plan-only stager and the W13 saturation
reducer: it allocates no subprocess, opens no runtime session, and
imports no adapter. The per-round work is injected as a ``round_runner``
callback returning a :class:`RoundOutcome`; a later iter supplies the
callback that actually spawns the survey. Keeping the driver pure means
the loop shape — budget arithmetic, halt precedence, checkpoint cadence —
is unit-testable without a runtime.

Halt precedence
---------------
The loop evaluates three stop conditions per round and records exactly one
:class:`RoundHaltReason` on the result:

(a) **contradiction stop** — the round's
    :class:`~eawf.kernel.spec.saturation.ContradictionStopRule.fired` bit is
    ``True``. A live claim stands refuted; halt collection immediately and
    report :attr:`RoundHaltReason.CONTRADICTION`. Checked first: firing this
    rule never resolves the contradiction, so the loop must not paper over it
    by reporting a saturation or budget halt instead.
(b) **saturation** — the round's :class:`SaturationReport.saturated` bit
    is ``True``. The campaign is dry; stop. Checked second so a round that
    saturates *and* exhausts the budget on the same turn reports
    :attr:`RoundHaltReason.SATURATED` (the campaign converged — the more
    informative reason).
(c) **round budget** — the loop has run ``round_budget`` rounds without
    saturating. The budget is a hard ceiling that guarantees termination
    even if the campaign never converges; stop and report
    :attr:`RoundHaltReason.ROUND_BUDGET`.

Between rounds the caller may also stop the loop before the next round
spawns: a cancelled campaign (:attr:`RoundHaltReason.CANCELLED`), an evidence
budget the next round would exceed (:attr:`RoundHaltReason.EVIDENCE_BUDGET`),
or a blocking operator input (:attr:`RoundHaltReason.PAUSED`).

The contradiction stop rule and the saturation report are evaluated
independently -- the loop reads both bits off the same :class:`RoundOutcome`
but neither is derived from, or mutates, the other (see
:mod:`eawf.kernel.spec.saturation`).

Checkpoint tiers
----------------
:class:`CheckpointPolicy` controls the operator-review cadence — how
often the loop pauses to surface progress for review. The cadence is one
of four closed tiers (:class:`CheckpointTier`): pause after every round,
after every ``n`` rounds, only at the terminal halt, or never. The
driver records each requested checkpoint as a :class:`Checkpoint` row on
the result; it does NOT block — surfacing the checkpoint to the operator
is the live runner's job, mirroring how the staged dispatch is the
hand-off, not the spawn.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.spec.saturation import ContradictionStopRule, SaturationReport

logger = logging.getLogger(__name__)

#: Default hard ceiling on rounds a single campaign loop runs before it
#: halts on budget. A campaign that has not converged in this many rounds
#: stops regardless so the loop always terminates.
DEFAULT_ROUND_BUDGET: int = 12

#: The rule's own unfired verdict, used as the default for a round_runner
#: that does not (yet) wire contradiction detection. The default keeps the
#: field additive: an existing caller that only cares about saturation sees
#: no behaviour change, while the loop still evaluates whatever rule the
#: runner does supply on every round.
_UNFIRED_CONTRADICTION_STOP_RULE = ContradictionStopRule(fired=False, offenders=())

#: Default checkpoint interval for the :attr:`CheckpointTier.EVERY_N`
#: tier — pause for operator review once every this-many rounds.
DEFAULT_CHECKPOINT_INTERVAL: int = 3


class CheckpointTier(StrEnum):
    """Closed ladder of operator-review cadences for the round loop.

    The tier names how often the loop pauses to surface progress for
    operator review. The string values are the on-the-wire / config /
    CLI tokens; the enum is the single source the driver and any future
    config leaf resolve against.

    Members:
        EVERY_ROUND: Pause for review after every round — the tightest
            cadence, for a high-stakes campaign the operator babysits.
        EVERY_N: Pause once every :attr:`CheckpointPolicy.interval`
            rounds — the balanced cadence.
        ON_HALT: Pause only at the terminal halt (saturation or budget) —
            the loosest non-silent cadence, for a campaign the operator
            reviews only once it converges.
        NEVER: Never pause — a fully autonomous loop that surfaces no
            mid-run checkpoint (the terminal result is still returned).
    """

    EVERY_ROUND = "every_round"
    EVERY_N = "every_n"
    ON_HALT = "on_halt"
    NEVER = "never"


class CheckpointPolicy(BaseModel):
    """Operator-review cadence policy for the bounded round loop.

    Gates how often :func:`run_round_loop` records a :class:`Checkpoint`
    for operator review. The :attr:`interval` field is only consulted for
    the :attr:`CheckpointTier.EVERY_N` tier; the other tiers ignore it.

    Attributes:
        tier: The review cadence — one of the four closed
            :class:`CheckpointTier` members. Defaults to
            :attr:`CheckpointTier.ON_HALT` so a policy that declares
            nothing still surfaces the terminal halt for review.
        interval: Rounds between review pauses for the
            :attr:`CheckpointTier.EVERY_N` tier. Must be ``>= 1`` (a
            zero or negative interval would either never fire or divide
            by zero). Ignored by the other three tiers. Defaults to
            :data:`DEFAULT_CHECKPOINT_INTERVAL`.
    """

    model_config = ConfigDict(extra="forbid")

    tier: CheckpointTier = CheckpointTier.ON_HALT
    interval: Annotated[int, Field(ge=1)] = DEFAULT_CHECKPOINT_INTERVAL

    def wants_checkpoint(self, round_number: int, *, halted: bool) -> bool:
        """Return whether the loop should pause for review after this round.

        Pure decision over the tier + round number; performs no I/O and
        records nothing — :func:`run_round_loop` calls it once per round
        and materialises the :class:`Checkpoint` row when it returns
        ``True``.

        Args:
            round_number: The 1-based index of the round just completed.
                The first round is ``1``.
            halted: ``True`` when this round is the terminal round (the
                loop halts after it on saturation or budget). The
                :attr:`CheckpointTier.ON_HALT` tier fires only on the
                terminal round; the other tiers fire irrespective of it.

        Returns:
            ``True`` iff this round's completion warrants an
            operator-review checkpoint under :attr:`tier`.

        Raises:
            ValueError: when *round_number* is not a positive integer
                (the loop is 1-based; round ``0`` never completes).
        """
        if round_number < 1:
            raise ValueError(f"round_number must be >= 1: {round_number!r}")
        match self.tier:
            case CheckpointTier.EVERY_ROUND:
                return True
            case CheckpointTier.EVERY_N:
                return round_number % self.interval == 0
            case CheckpointTier.ON_HALT:
                return halted
            case CheckpointTier.NEVER:
                return False


class RoundHaltReason(StrEnum):
    """Closed vocabulary for why the bounded round loop stopped.

    Exactly one reason is recorded on a :class:`RoundLoopResult`. The
    members are mutually exclusive at the result level even though both
    conditions can hold on the same final round; the loop's halt
    precedence (saturation before budget) picks the single recorded
    reason.

    Members:
        CONTRADICTION: The final round's
            :attr:`~eawf.kernel.spec.saturation.ContradictionStopRule.fired`
            bit was ``True`` — a live claim stands refuted. Distinct from
            :attr:`SATURATED`: firing the stop rule halts collection, it does
            not mean the campaign converged, and it is never inferred from
            (or reported as) a saturation gate failure.
        SATURATED: The campaign reached saturation — the final round's
            :attr:`SaturationReport.saturated` bit was ``True``.
        ROUND_BUDGET: The loop spent its ``round_budget`` without
            saturating — the hard-ceiling termination guarantee.
        CANCELLED: The caller's ``should_continue`` predicate went ``False``
            between rounds — the campaign was cancelled while the run was in
            flight, so the loop stops rather than spawning the next round.
        EVIDENCE_BUDGET: The caller's ``halt_before_round`` hook reported that
            the next round would push the campaign's evidence budget past a
            limit, so the loop stops before paying for it.
        PAUSED: The caller's ``halt_before_round`` hook reported a blocking
            operator input, so the loop yields to the operator between rounds
            instead of spawning the next one.
    """

    CONTRADICTION = "contradiction"
    SATURATED = "saturated"
    ROUND_BUDGET = "round_budget"
    CANCELLED = "cancelled"
    EVIDENCE_BUDGET = "evidence_budget"
    PAUSED = "paused"


class Checkpoint(BaseModel):
    """One recorded operator-review pause point in the round loop.

    The driver appends a :class:`Checkpoint` to
    :attr:`RoundLoopResult.checkpoints` each round
    :meth:`CheckpointPolicy.wants_checkpoint` returns ``True``. It is a
    *record* of where review was warranted, not a blocking call — the
    live runner consumes the checkpoint list to drive the actual operator
    hand-off.

    Attributes:
        round_number: The 1-based round index after which the loop
            requested review.
        saturated: The :attr:`SaturationReport.saturated` bit at this
            checkpoint — lets a reviewer see, per pause, whether the
            campaign had converged yet.
        terminal: ``True`` when this checkpoint coincides with the loop's
            terminal halt round (the last round before the loop stopped).
    """

    model_config = ConfigDict(extra="forbid")

    round_number: Annotated[int, Field(ge=1)]
    saturated: bool
    terminal: bool = False


class RoundOutcome(BaseModel):
    """The per-round result an injected ``round_runner`` returns.

    A round_runner does the (later-wired) survey work for one round and
    reports back through this envelope. The driver reads
    :attr:`saturation` and :attr:`contradiction_stop` to decide whether to
    halt; it never inspects the survey internals. The runner stays free to
    carry its own side state — the driver depends only on this typed surface.

    Attributes:
        saturation: The four-gate :class:`SaturationReport` computed over
            the campaign ledgers AFTER this round's claims were appended.
            The driver halts the loop when
            :attr:`SaturationReport.saturated` is ``True``.
        contradiction_stop: The
            :class:`~eawf.kernel.spec.saturation.ContradictionStopRule`
            computed independently of :attr:`saturation` over the same
            post-round ledger. The driver halts the loop when
            :attr:`~eawf.kernel.spec.saturation.ContradictionStopRule.fired`
            is ``True``, ahead of the saturation / budget checks. Defaults to
            an unfired rule so a runner that does not (yet) wire
            contradiction detection is unaffected.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    saturation: SaturationReport
    contradiction_stop: ContradictionStopRule = Field(
        default_factory=lambda: _UNFIRED_CONTRADICTION_STOP_RULE
    )


class RoundLoopResult(BaseModel):
    """Typed outcome of a bounded :func:`run_round_loop` run.

    Carries the terminal state of the loop: how many rounds ran, why it
    stopped, the recorded operator-review checkpoints, and the final
    saturation report. Produced only by :func:`run_round_loop`.

    Attributes:
        rounds_run: Count of rounds actually executed (``1 <= rounds_run
            <= round_budget``). Always at least one — the loop runs the
            first round before it can check either halt condition.
        halt_reason: The single :class:`RoundHaltReason` the loop stopped
            on, per the saturation-before-budget precedence.
        checkpoints: The operator-review pauses the
            :class:`CheckpointPolicy` requested, in round order. Empty
            when the policy is :attr:`CheckpointTier.NEVER` (or
            :attr:`CheckpointTier.EVERY_N` whose interval never divided a
            completed round).
        final_saturation: The :class:`SaturationReport` from the terminal
            round — ``saturated`` is ``True`` iff
            :attr:`halt_reason` is :attr:`RoundHaltReason.SATURATED`.
        final_contradiction_stop: The
            :class:`~eawf.kernel.spec.saturation.ContradictionStopRule` from
            the terminal round, computed independently of
            :attr:`final_saturation` — ``fired`` is ``True`` iff
            :attr:`halt_reason` is :attr:`RoundHaltReason.CONTRADICTION`.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    rounds_run: Annotated[int, Field(ge=1)]
    halt_reason: RoundHaltReason
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    final_saturation: SaturationReport
    final_contradiction_stop: ContradictionStopRule = Field(
        default_factory=lambda: _UNFIRED_CONTRADICTION_STOP_RULE
    )

    @property
    def saturated(self) -> bool:
        """Whether the loop halted because the campaign converged."""
        return self.halt_reason is RoundHaltReason.SATURATED

    @property
    def contradiction_fired(self) -> bool:
        """Whether the loop halted because a live claim stood refuted."""
        return self.halt_reason is RoundHaltReason.CONTRADICTION


def _between_rounds_halt(
    should_continue: Callable[[], bool] | None,
    halt_before_round: Callable[[], RoundHaltReason | None] | None,
) -> RoundHaltReason | None:
    """Return why the loop must stop before the next round, or ``None`` to go on.

    A cancellation wins over the caller's own halt hook: a cancelled campaign
    stops as cancelled even when its budget is also spent.
    """
    if should_continue is not None and not should_continue():
        return RoundHaltReason.CANCELLED
    if halt_before_round is not None:
        return halt_before_round()
    return None


def run_round_loop(
    round_runner: Callable[[int], RoundOutcome],
    *,
    round_budget: int = DEFAULT_ROUND_BUDGET,
    checkpoint_policy: CheckpointPolicy | None = None,
    should_continue: Callable[[], bool] | None = None,
    halt_before_round: Callable[[], RoundHaltReason | None] | None = None,
) -> RoundLoopResult:
    """Drive a bounded research-campaign round loop until dry or out of budget.

    Runs *round_runner* once per round, starting at round ``1``, and
    halts on the FIRST of three conditions: the round's
    :attr:`~eawf.kernel.spec.saturation.ContradictionStopRule.fired` bit
    (a live claim stands refuted), :attr:`SaturationReport.saturated` (the
    campaign is dry), or the loop has run *round_budget* rounds (the hard
    ceiling). The contradiction stop is checked first, then saturation,
    then budget, so a round that fires more than one of the three records
    the earliest-checked reason -- see the module docstring's "Halt
    precedence" section for why. The stop rule and the saturation report
    are read off the same :class:`RoundOutcome` but computed independently;
    neither is derived from, or mutated by, this precedence check.

    The driver is pure with respect to its own body: it allocates no
    subprocess, opens no runtime session, and imports no adapter. All
    survey work happens inside *round_runner*, which the caller injects;
    the driver only sequences the rounds, applies the halt precedence,
    and records checkpoints per *checkpoint_policy*.

    After each round the driver asks
    :meth:`CheckpointPolicy.wants_checkpoint`; when it returns ``True`` a
    :class:`Checkpoint` is appended to the result. Recording a checkpoint
    does NOT block — surfacing it to the operator is the live runner's
    responsibility.

    Args:
        round_runner: Callback invoked once per round with the 1-based
            round number; returns the round's :class:`RoundOutcome`
            carrying the post-round saturation report and contradiction
            stop rule.
        round_budget: Hard ceiling on rounds. Must be ``>= 1`` (a loop
            must run at least one round). Defaults to
            :data:`DEFAULT_ROUND_BUDGET`.
        checkpoint_policy: The operator-review cadence policy. ``None``
            defaults to a fresh :class:`CheckpointPolicy` (the
            :attr:`CheckpointTier.ON_HALT` tier — surface only the
            terminal halt).
        should_continue: Optional predicate consulted BETWEEN rounds. When it
            returns ``False`` the loop halts with
            :attr:`RoundHaltReason.CANCELLED` instead of spawning the next
            round, so a campaign cancelled in flight stops costing money. It is
            never consulted before the first round, which keeps the
            at-least-one-round guarantee that makes the terminal outcome
            non-``None``. ``None`` means no cancellation check.
        halt_before_round: Optional hook consulted BETWEEN rounds, after
            *should_continue*. A non-``None`` return halts the loop with that
            reason before the next round spawns (an exhausted evidence budget,
            an operator pause). Like *should_continue* it is never consulted
            before the first round. ``None`` means no such check.

    Returns:
        A :class:`RoundLoopResult` with the rounds run, the halt reason,
        the recorded checkpoints, and the terminal saturation report +
        contradiction stop rule.

    Raises:
        ValueError: when *round_budget* is less than 1 (the loop cannot
            run zero rounds).
    """
    if round_budget < 1:
        raise ValueError(f"round_budget must be >= 1: {round_budget!r}")
    policy = checkpoint_policy if checkpoint_policy is not None else CheckpointPolicy()

    checkpoints: list[Checkpoint] = []
    round_number = 0
    outcome: RoundOutcome | None = None
    halt_reason: RoundHaltReason | None = None

    while round_number < round_budget:
        # Consulted only once a round has run, so the terminal outcome is always
        # populated: a cancel cannot arrive before the run itself has started.
        if round_number >= 1:
            halt_reason = _between_rounds_halt(should_continue, halt_before_round)
            if halt_reason is not None:
                break
        round_number += 1
        outcome = round_runner(round_number)
        contradiction_fired = outcome.contradiction_stop.fired
        saturated = outcome.saturation.saturated
        budget_spent = round_number >= round_budget
        # The contradiction stop rule wins the precedence: it halts collection
        # over an unresolved live claim regardless of what the independently
        # computed saturation report or the budget say this same round.
        # Saturation wins over budget: a round that both converges and
        # exhausts the budget reports the campaign as dry, not starved.
        halted = contradiction_fired or saturated or budget_spent
        if contradiction_fired:
            halt_reason = RoundHaltReason.CONTRADICTION
        elif saturated:
            halt_reason = RoundHaltReason.SATURATED
        elif budget_spent:
            halt_reason = RoundHaltReason.ROUND_BUDGET

        if policy.wants_checkpoint(round_number, halted=halted):
            checkpoints.append(
                Checkpoint(
                    round_number=round_number,
                    saturated=saturated,
                    terminal=halted,
                )
            )
        if halted:
            break

    # round_budget >= 1 guarantees the loop body ran at least once, so
    # outcome and halt_reason are always populated here.
    assert outcome is not None
    assert halt_reason is not None
    logger.debug(
        f"run_round_loop rounds={round_number} budget={round_budget} "
        f"halt={halt_reason.value} checkpoints={len(checkpoints)} "
        f"saturated={outcome.saturation.saturated} "
        f"contradiction_fired={outcome.contradiction_stop.fired}"
    )
    return RoundLoopResult(
        rounds_run=round_number,
        halt_reason=halt_reason,
        checkpoints=checkpoints,
        final_saturation=outcome.saturation,
        final_contradiction_stop=outcome.contradiction_stop,
    )


__all__ = [
    "DEFAULT_CHECKPOINT_INTERVAL",
    "DEFAULT_ROUND_BUDGET",
    "Checkpoint",
    "CheckpointPolicy",
    "CheckpointTier",
    "RoundHaltReason",
    "RoundLoopResult",
    "RoundOutcome",
    "run_round_loop",
]
