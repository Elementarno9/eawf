"""Daemon-side token-cap interlock: wire the HALT verdict to the kill ladder.

This module is the join between the two pre-existing, independently-tested
safety-floor primitives:

* the pure enforcement classifier
  :func:`eawf.runtime.budget.policy.classify_enforcement`, which returns a
  typed :class:`~eawf.runtime.budget.policy.BudgetDecision` whose action is
  :data:`~eawf.runtime.budget.policy.BudgetAction.HALT` only under ``hard``
  enforce at or above the multiplier-scaled cap; and
* the process-group kill ladder
  :func:`eawf.runtime.runtimes.cancel.cancel_with_grace`, the
  SIGTERM -> grace -> SIGKILL escalation over a runaway wave's whole
  process group.

It lives daemon-side on purpose. ``runtime/runtimes/cancel.py`` imports
*from* ``runtime/budget/service.py`` (it composes ``terminate_with_grace``
over the group), so an orchestration that imported ``cancel`` from inside
``runtime/budget/`` would close a ``budget -> runtimes -> budget`` import
cycle. ``runtime/daemon/`` is free to import both, so the interlock is the
daemon's responsibility and ``runtime/budget/`` keeps no ``cancel`` import.

Honest scope (the safety-floor brief's C6). The mutating wave-dispatch
spawn is still dark: ``runtime/daemon/methods/agent.py`` returns ``pid=0``
and there is no live pid/pgid registry for dispatched waves yet (only the
read-only clarify path spawns a live process). So at today's live metering
point the addressable pgid is usually unknown. The interlock encodes that
honestly rather than faking a kill: it takes ``pgid`` as an explicit
argument and, on a HALT with no addressable group (``pgid is None``), it
still computes and loudly logs the over-cap decision but sends no signal --
you cannot reap a process you cannot address. Wave I04-W03 (the live-
dispatch consumer) threads the real pgid in once the mutating spawn
registers a pid; this interlock is the wired, tested seam it feeds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from eawf.kernel.runtime.budget_notice import BudgetNotice
from eawf.kernel.runtime.control import ControlRequestId
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.runtime.budget.policy import (
    BudgetAction,
    BudgetDecision,
    EnforceMode,
    classify_enforcement,
)
from eawf.runtime.budget.service import TerminationResult
from eawf.runtime.runtimes.cancel import cancel_with_grace
from eawf.runtime.runtimes.metering import MeterReading

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InterlockOutcome:
    """Outcome of an :func:`enforce_token_cap` call.

    Attributes:
        decision: The typed :class:`~eawf.runtime.budget.policy.BudgetDecision`
            the post-increment consumption classified to. Always present --
            the classification runs even when no process is addressable.
        terminated: ``True`` only when the decision was
            :data:`~eawf.runtime.budget.policy.BudgetAction.HALT` *and* an
            addressable pgid was supplied *and* the kill ladder was driven.
            ``False`` on a HALT with no addressable pgid (the dark-dispatch
            warning path) and on every CONTINUE / WARN.
        termination: The :class:`~eawf.runtime.budget.service.TerminationResult`
            returned by the kill ladder when :attr:`terminated` is ``True``;
            ``None`` otherwise.
    """

    decision: BudgetDecision
    terminated: bool
    termination: TerminationResult | None


def enforce_token_cap(
    *,
    consumed: int,
    base_budget: int | None,
    enforce: EnforceMode,
    multiplier: float,
    pgid: int | None,
    cancel: Callable[[int], TerminationResult] = cancel_with_grace,
) -> InterlockOutcome:
    """Classify *consumed* against the cap and reap the group on a HALT.

    Runs :func:`eawf.runtime.budget.policy.classify_enforcement` over the
    post-increment consumption, then acts on the verdict:

    * :data:`~eawf.runtime.budget.policy.BudgetAction.HALT` with a positive
      *pgid*: drive *cancel* over the group (the
      SIGTERM -> grace -> SIGKILL ladder) and report ``terminated=True``
      with the captured :class:`~eawf.runtime.budget.service.TerminationResult`.
    * :data:`~eawf.runtime.budget.policy.BudgetAction.HALT` with ``pgid is
      None`` (or a non-positive pgid): the wave is over its hard cap but no
      process group is addressable (the dark mutating dispatch -- C6). The
      decision is still computed and a loud WARNING is logged, but no signal
      is sent (``terminated=False``). I04-W03 supplies the real pgid here.
    * :data:`~eawf.runtime.budget.policy.BudgetAction.CONTINUE` /
      :data:`~eawf.runtime.budget.policy.BudgetAction.WARN`: no-op
      (``terminated=False``). ``soft`` enforce never reaches HALT, so the
      hot metering path stays a no-op under the default mode.

    *cancel* is an injectable seam (defaulting to
    :func:`eawf.runtime.runtimes.cancel.cancel_with_grace`) so a test can
    pass a fake that records the call and returns a canned result without
    delivering a real signal.

    Args:
        consumed: Cumulative tokens spent so far on the wave (>= 0); the
            post-increment total at the live metering point.
        base_budget: The wave's configured token budget, or ``None`` for no
            cap (a no-op -- always CONTINUE, never a kill).
        enforce: Enforce mode -- ``soft`` (warn, keep running) or ``hard``
            (HALT at the cap). Only ``hard`` can trigger a reap.
        multiplier: Cap multiplier scaling *base_budget* into the enforced
            cap (must be strictly positive).
        pgid: Process-group id of the wave's spawn, or ``None`` when no live
            group is addressable (today's dark dispatch). A non-positive
            pgid is treated as not-addressable -- the HALT is logged but no
            signal is sent.
        cancel: Kill-ladder callable invoked with the pgid on a reap.
            Defaults to the real group ladder; tests inject a fake.

    Returns:
        An :class:`InterlockOutcome` carrying the decision, whether a reap
        ran, and the termination result when it did.

    Raises:
        ValueError: Propagated from
            :func:`~eawf.runtime.budget.policy.classify_enforcement` when
            *consumed* is negative or *multiplier* is not strictly positive.
    """
    decision = classify_enforcement(
        consumed,
        base_budget,
        enforce=enforce,
        multiplier=multiplier,
    )
    if decision.action is not BudgetAction.HALT:
        return InterlockOutcome(decision=decision, terminated=False, termination=None)
    if pgid is None or pgid <= 0:
        logger.warning(
            f"enforce_token_cap over_cap=true cap={decision.cap} consumed={consumed} "
            f"pgid=none terminated=false reason=no-addressable-process"
        )
        return InterlockOutcome(decision=decision, terminated=False, termination=None)
    termination = cancel(pgid)
    logger.warning(
        f"enforce_token_cap wave-over-cap cap={decision.cap} consumed={consumed} "
        f"pgid={pgid} terminated=true sigkill={termination.sigkill_sent}"
    )
    return InterlockOutcome(decision=decision, terminated=True, termination=termination)


class BudgetTerminationLedger(Protocol):
    """The two durable steps a budget termination is written in.

    They are two calls rather than one because they record two different
    things. :meth:`open` records what was read and what was asked, and it
    runs while the process group is still alive. :meth:`confirm` records
    what was observed, and it can only run once the kill ladder has
    observed it. Collapsing them would append a confirmed effect for a
    reap that had not happened, and a daemon lost between the two would
    leave that lie standing.
    """

    def open(self, notice: BudgetNotice) -> bool:
        """Append the notice and the control it opens; return whether it leads.

        Returns:
            ``True`` when the opened control took the Run's one control
            lease, so this guard owns the reap. ``False`` when another
            principal's control already holds it.
        """
        ...

    def confirm(self, notice: BudgetNotice, termination: TerminationResult) -> None:
        """Append the confirmed effect of the reap the ladder observed."""
        ...


@dataclass(frozen=True, slots=True)
class InFlightBudgetOutcome:
    """Outcome of one in-flight cap reading on a live Run.

    Attributes:
        decision: The verdict the reading classified to. Always present,
            because the classification runs on every reading.
        notice: The recorded budget notice, or ``None`` when the reading
            stayed under the cap and nothing was written.
        led: Whether the opened control took the Run's control lease. A
            budget termination that lost it records its notice and signals
            nothing: the holder's own control ends the Run.
        terminated: ``True`` only when the kill ladder was driven.
        termination: The ladder's result when :attr:`terminated`, else
            ``None``.
    """

    decision: BudgetDecision
    notice: BudgetNotice | None
    led: bool
    terminated: bool
    termination: TerminationResult | None


def guard_in_flight_budget(
    *,
    reading: MeterReading,
    base_budget: int | None,
    enforce: EnforceMode,
    multiplier: float,
    run_ref: RunUrn,
    control_request_ref: ControlRequestId,
    noticed_at: datetime,
    pgid: int | None,
    ledger: BudgetTerminationLedger,
    cancel: Callable[[int], TerminationResult] = cancel_with_grace,
) -> InFlightBudgetOutcome:
    """Terminate a Run at its cap while its child is still running.

    The shipped :func:`enforce_token_cap` classifies the burn a dispatch
    reports once the dispatch has returned, so the group it signals has
    already exited. This runs on a reading taken mid-turn and writes before
    it signals: the notice and the opened control reach the run ledger
    while the process group is alive, and the confirmed effect is appended
    only after the ladder observed the group die.

    The Run's one control lease still decides who acts. A budget
    termination racing an operator cancel is two answers to the same
    question, and the second one is superseded -- so the loser records its
    notice, which blocks nothing, and sends no signal. The Run reaches the
    same terminal status either way; only the attribution differs.

    Args:
        reading: The metered consumption so far this turn.
        base_budget: The Run's configured token budget, or ``None`` for no
            cap, which never terminates anything.
        enforce: ``soft`` (warn and keep running) or ``hard`` (halt at the
            cap). Only ``hard`` reaches a termination.
        multiplier: Cap multiplier scaling *base_budget* into the enforced
            cap; must be strictly positive.
        run_ref: The Run the reading is of.
        control_request_ref: The control request a termination opens.
        noticed_at: When the crossing was read.
        pgid: Process-group id of the live child, or ``None`` when none is
            addressable -- the notice is still recorded and no signal sent.
        ledger: The durable writer of the notice and the control facts.
        cancel: Kill-ladder callable driven with the pgid on a reap.

    Returns:
        The outcome, carrying the decision, the recorded notice and
        whether the ladder ran.

    Raises:
        ValueError: Propagated from
            :func:`~eawf.runtime.budget.policy.classify_enforcement` when
            the consumption is negative or *multiplier* is not strictly
            positive.
    """
    decision = classify_enforcement(
        reading.observed_tokens,
        base_budget,
        enforce=enforce,
        multiplier=multiplier,
    )
    if decision.action is not BudgetAction.HALT or decision.cap is None:
        return InFlightBudgetOutcome(
            decision=decision, notice=None, led=False, terminated=False, termination=None
        )
    notice = BudgetNotice(
        run_ref=run_ref,
        control_request_ref=control_request_ref,
        cap_tokens=decision.cap,
        observed_tokens=reading.observed_tokens,
        noticed_at=noticed_at,
    )
    led = ledger.open(notice)
    if not led:
        logger.warning(
            f"guard_in_flight_budget run={run_ref.entity_key!r} cap={decision.cap} "
            f"observed={reading.observed_tokens} led=false terminated=false "
            "reason=another-control-holds-the-lease"
        )
        return InFlightBudgetOutcome(
            decision=decision, notice=notice, led=False, terminated=False, termination=None
        )
    if pgid is None or pgid <= 0:
        logger.warning(
            f"guard_in_flight_budget run={run_ref.entity_key!r} cap={decision.cap} "
            f"observed={reading.observed_tokens} pgid=none terminated=false "
            "reason=no-addressable-process"
        )
        return InFlightBudgetOutcome(
            decision=decision, notice=notice, led=True, terminated=False, termination=None
        )
    termination = cancel(pgid)
    ledger.confirm(notice, termination)
    logger.warning(
        f"guard_in_flight_budget run={run_ref.entity_key!r} cap={decision.cap} "
        f"observed={reading.observed_tokens} pgid={pgid} terminated=true "
        f"sigkill={termination.sigkill_sent}"
    )
    return InFlightBudgetOutcome(
        decision=decision, notice=notice, led=True, terminated=True, termination=termination
    )


__all__ = [
    "BudgetTerminationLedger",
    "InFlightBudgetOutcome",
    "InterlockOutcome",
    "enforce_token_cap",
    "guard_in_flight_budget",
]
