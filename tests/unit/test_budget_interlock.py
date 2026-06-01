"""Unit tests for the daemon-side token-cap interlock (P29-I03-W05).

Covers :func:`eawf.runtime.daemon.budget_interlock.enforce_token_cap` -- the
join between the pure enforcement classifier
(:func:`eawf.runtime.budget.policy.classify_enforcement`) and the
process-group kill ladder
(:func:`eawf.runtime.runtimes.cancel.cancel_with_grace`).

The canceller is an injected fake that records the pgid it was called with
and returns a canned :class:`~eawf.runtime.budget.service.TerminationResult`,
so a HALT reap is asserted deterministically with no real signal delivered.
The honest dark-dispatch path (HALT computed but ``pgid is None``, so no
signal) is asserted explicitly: the decision is still HALT, but the
canceller is never called.
"""

from __future__ import annotations

import pytest

from eawf.runtime.budget.policy import BudgetAction
from eawf.runtime.budget.service import TerminationResult
from eawf.runtime.daemon.budget_interlock import (
    InterlockOutcome,
    enforce_token_cap,
)


class FakeCanceller:
    """Records every pgid it is asked to reap; returns a canned result.

    Substituted for :func:`cancel_with_grace` so the interlock's HALT path
    is exercised without delivering a real signal. ``calls`` is the ordered
    list of pgids the interlock passed -- empty when no reap fired.
    """

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, pgid: int) -> TerminationResult:
        self.calls.append(pgid)
        return TerminationResult(
            sigterm_sent=True,
            sigkill_sent=False,
            exited_on_term=True,
            waited_seconds=0.0,
        )


# --- HALT with an addressable pgid: the reap fires --------------------------


def test_hard_over_cap_with_pgid_reaps_group_once() -> None:
    """Hard enforce, consumed >= scaled cap, live pgid -> single reap call."""
    cancel = FakeCanceller()
    outcome = enforce_token_cap(
        consumed=1500,
        base_budget=1000,
        enforce="hard",
        multiplier=1.5,
        pgid=4242,
        cancel=cancel,
    )
    assert cancel.calls == [4242]
    assert outcome.terminated is True
    assert outcome.decision.action is BudgetAction.HALT
    assert outcome.termination is not None
    assert outcome.termination.sigterm_sent is True
    assert isinstance(outcome, InterlockOutcome)


def test_hard_far_over_cap_with_pgid_reaps() -> None:
    """A consumption far past the cap under hard enforce still reaps once."""
    cancel = FakeCanceller()
    outcome = enforce_token_cap(
        consumed=10_000,
        base_budget=1000,
        enforce="hard",
        multiplier=1.5,
        pgid=99,
        cancel=cancel,
    )
    assert cancel.calls == [99]
    assert outcome.terminated is True
    assert outcome.decision.action is BudgetAction.HALT


# --- HALT with no addressable pgid: the honest dark-dispatch path -----------


def test_hard_over_cap_no_pgid_computes_halt_but_does_not_reap() -> None:
    """Hard over cap with ``pgid is None`` -> HALT decided, NO signal sent."""
    cancel = FakeCanceller()
    outcome = enforce_token_cap(
        consumed=1500,
        base_budget=1000,
        enforce="hard",
        multiplier=1.5,
        pgid=None,
        cancel=cancel,
    )
    assert cancel.calls == []
    assert outcome.terminated is False
    assert outcome.termination is None
    # The decision is still HALT -- the cap WAS breached; only the reap is
    # withheld because no process group is addressable (the dark dispatch).
    assert outcome.decision.action is BudgetAction.HALT
    assert outcome.decision.over_cap is True


def test_hard_over_cap_non_positive_pgid_does_not_reap() -> None:
    """A non-positive pgid (e.g. the dark dispatch's pid=0) is not addressable."""
    cancel = FakeCanceller()
    outcome = enforce_token_cap(
        consumed=1500,
        base_budget=1000,
        enforce="hard",
        multiplier=1.5,
        pgid=0,
        cancel=cancel,
    )
    assert cancel.calls == []
    assert outcome.terminated is False
    assert outcome.decision.action is BudgetAction.HALT


# --- soft enforce never reaps ----------------------------------------------


def test_soft_over_cap_with_pgid_warns_does_not_reap() -> None:
    """Soft enforce over the cap warns -- never HALT, never a reap."""
    cancel = FakeCanceller()
    outcome = enforce_token_cap(
        consumed=1500,
        base_budget=1000,
        enforce="soft",
        multiplier=1.5,
        pgid=4242,
        cancel=cancel,
    )
    assert cancel.calls == []
    assert outcome.terminated is False
    assert outcome.decision.action is BudgetAction.WARN


# --- under the cap: continue (either mode) ----------------------------------


def test_hard_under_cap_continues_no_reap() -> None:
    """Hard enforce below the scaled cap is CONTINUE -- no reap."""
    cancel = FakeCanceller()
    outcome = enforce_token_cap(
        consumed=1499,
        base_budget=1000,
        enforce="hard",
        multiplier=1.5,
        pgid=4242,
        cancel=cancel,
    )
    assert cancel.calls == []
    assert outcome.terminated is False
    assert outcome.decision.action is BudgetAction.CONTINUE


def test_soft_under_cap_continues_no_reap() -> None:
    """Soft enforce below the scaled cap is CONTINUE -- no reap."""
    cancel = FakeCanceller()
    outcome = enforce_token_cap(
        consumed=1499,
        base_budget=1000,
        enforce="soft",
        multiplier=1.5,
        pgid=4242,
        cancel=cancel,
    )
    assert cancel.calls == []
    assert outcome.terminated is False
    assert outcome.decision.action is BudgetAction.CONTINUE


# --- no budget: continue ----------------------------------------------------


def test_no_base_budget_continues_no_reap() -> None:
    """A ``None`` base budget is a no-op cap -- CONTINUE, never a reap."""
    cancel = FakeCanceller()
    outcome = enforce_token_cap(
        consumed=10_000,
        base_budget=None,
        enforce="hard",
        multiplier=1.5,
        pgid=4242,
        cancel=cancel,
    )
    assert cancel.calls == []
    assert outcome.terminated is False
    assert outcome.decision.action is BudgetAction.CONTINUE
    assert outcome.decision.cap is None


# --- cap boundary: >= is the cap test ---------------------------------------


def test_hard_consumed_exactly_at_cap_reaps() -> None:
    """Hard enforce with consumed == cap (1000 * 1.5 == 1500) HALTs + reaps."""
    cancel = FakeCanceller()
    outcome = enforce_token_cap(
        consumed=1500,
        base_budget=1000,
        enforce="hard",
        multiplier=1.5,
        pgid=4242,
        cancel=cancel,
    )
    assert cancel.calls == [4242]
    assert outcome.terminated is True
    assert outcome.decision.action is BudgetAction.HALT


def test_hard_consumed_one_below_cap_continues() -> None:
    """Hard enforce with consumed == cap - 1 (1499) is CONTINUE -- no reap."""
    cancel = FakeCanceller()
    outcome = enforce_token_cap(
        consumed=1499,
        base_budget=1000,
        enforce="hard",
        multiplier=1.5,
        pgid=4242,
        cancel=cancel,
    )
    assert cancel.calls == []
    assert outcome.terminated is False
    assert outcome.decision.action is BudgetAction.CONTINUE


# --- error path: negative consumed propagates -------------------------------


def test_negative_consumed_propagates_value_error() -> None:
    """A negative consumption propagates ValueError from classify_enforcement."""
    cancel = FakeCanceller()
    with pytest.raises(ValueError, match="consumed must be non-negative"):
        enforce_token_cap(
            consumed=-1,
            base_budget=1000,
            enforce="hard",
            multiplier=1.5,
            pgid=4242,
            cancel=cancel,
        )
    assert cancel.calls == []


def test_default_canceller_is_cancel_with_grace() -> None:
    """The injectable ``cancel`` seam defaults to the real group kill ladder."""
    from eawf.runtime.daemon import budget_interlock
    from eawf.runtime.runtimes.cancel import cancel_with_grace

    defaults = budget_interlock.enforce_token_cap.__defaults__
    # ``cancel`` is the sole keyword-only default with a value.
    assert defaults is None or cancel_with_grace in (defaults or ())
    sig = enforce_token_cap.__kwdefaults__
    assert sig is not None
    assert sig["cancel"] is cancel_with_grace
