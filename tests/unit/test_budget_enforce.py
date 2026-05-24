"""Unit tests for budget enforcement (P27-I01-W24).

Covers two surfaces:

* :mod:`eawf.runtime.budget.policy` enforcement classification — the ``soft``
  default (warn + continue) vs the ``hard`` opt-in (halt at cap), and the
  ``1.5`` multiplier that scales the base budget into the enforced cap.
* :func:`eawf.runtime.budget.service.terminate_with_grace` — the SIGTERM ->
  grace-window -> SIGKILL ladder, driven by a fake process so SIGKILL
  timing is deterministically asserted (never before the grace window,
  always after it when the process is still alive).
"""

from __future__ import annotations

import signal

import pytest

from eawf.runtime.budget.policy import (
    DEFAULT_ENFORCE,
    DEFAULT_MULTIPLIER,
    BudgetAction,
    classify_enforcement,
    effective_cap,
)
from eawf.runtime.budget.service import (
    DEFAULT_GRACE_SECONDS,
    TerminationResult,
    terminate_with_grace,
)

# --- policy: defaults --------------------------------------------------------


def test_default_enforce_is_soft() -> None:
    """``flow.budget.enforce`` defaults to soft (advisory-only)."""
    assert DEFAULT_ENFORCE == "soft"


def test_default_multiplier_is_1_5() -> None:
    """``flow.budget.multiplier`` defaults to 1.5 (50% headroom)."""
    assert pytest.approx(1.5) == DEFAULT_MULTIPLIER


# --- policy: multiplier-scaled cap -------------------------------------------


def test_effective_cap_scales_by_default_multiplier() -> None:
    """The default 1.5 multiplier scales a 1000-token base to a 1500 cap."""
    assert effective_cap(1000, DEFAULT_MULTIPLIER) == 1500


def test_effective_cap_floors_fractional_tokens() -> None:
    """A fractional scaled cap floors to whole tokens (333 * 1.5 == 499.5)."""
    assert effective_cap(333, 1.5) == 499


def test_effective_cap_none_base_passes_through() -> None:
    assert effective_cap(None, 1.5) is None


def test_effective_cap_rejects_non_positive_multiplier() -> None:
    with pytest.raises(ValueError, match="multiplier must be positive"):
        effective_cap(1000, 0.0)


# --- policy: soft classification (warn + continue) ---------------------------


def test_soft_below_cap_continues() -> None:
    """Soft mode under the scaled cap (1499 < 1500) is CONTINUE."""
    decision = classify_enforcement(1499, 1000, enforce="soft", multiplier=1.5)
    assert decision.action is BudgetAction.CONTINUE
    assert decision.cap == 1500
    assert decision.over_cap is False


def test_soft_at_cap_warns_but_does_not_halt() -> None:
    """Soft mode at the scaled cap warns and keeps running — never halts."""
    decision = classify_enforcement(1500, 1000, enforce="soft", multiplier=1.5)
    assert decision.action is BudgetAction.WARN
    assert decision.action is not BudgetAction.HALT
    assert decision.over_cap is True
    assert decision.cap == 1500


def test_soft_far_over_cap_still_warns() -> None:
    """Soft mode never escalates to HALT no matter how far over the cap."""
    decision = classify_enforcement(10_000, 1000, enforce="soft", multiplier=1.5)
    assert decision.action is BudgetAction.WARN


def test_soft_is_the_default_mode() -> None:
    """Omitting ``enforce`` uses soft — at-cap warns, does not halt."""
    decision = classify_enforcement(1500, 1000, multiplier=1.5)
    assert decision.enforce == "soft"
    assert decision.action is BudgetAction.WARN


# --- policy: hard classification (fail at cap) -------------------------------


def test_hard_below_cap_continues() -> None:
    """Hard mode under the scaled cap still continues."""
    decision = classify_enforcement(1499, 1000, enforce="hard", multiplier=1.5)
    assert decision.action is BudgetAction.CONTINUE
    assert decision.over_cap is False


def test_hard_at_cap_halts() -> None:
    """Hard mode at the scaled cap halts the wave (fails at cap)."""
    decision = classify_enforcement(1500, 1000, enforce="hard", multiplier=1.5)
    assert decision.action is BudgetAction.HALT
    assert decision.over_cap is True
    assert decision.cap == 1500


def test_hard_over_cap_halts() -> None:
    decision = classify_enforcement(2000, 1000, enforce="hard", multiplier=1.5)
    assert decision.action is BudgetAction.HALT


# --- policy: no-budget + error paths -----------------------------------------


def test_no_base_budget_always_continues() -> None:
    """A ``None`` base budget is a no-op cap under either mode."""
    soft = classify_enforcement(10_000, None, enforce="soft")
    hard = classify_enforcement(10_000, None, enforce="hard")
    assert soft.action is BudgetAction.CONTINUE
    assert hard.action is BudgetAction.CONTINUE
    assert soft.cap is None
    assert hard.cap is None


def test_classify_enforcement_rejects_negative_consumed() -> None:
    with pytest.raises(ValueError, match="consumed must be non-negative"):
        classify_enforcement(-1, 1000)


def test_classify_enforcement_rejects_non_positive_multiplier() -> None:
    with pytest.raises(ValueError, match="multiplier must be positive"):
        classify_enforcement(100, 1000, multiplier=-0.5)


# --- service: SIGTERM -> grace -> SIGKILL ladder -----------------------------


class FakeClock:
    """Deterministic monotonic clock advanced manually by the test.

    ``time()`` returns the current value; the ``sleep`` callable advances
    the clock by the requested duration, simulating wall-clock passage
    without any real delay.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeProcess:
    """Fake process for the termination ladder.

    Records every signal it receives in order. ``exit_after`` sets a clock
    value at which the process becomes dead (``poll`` flips from ``None``
    to ``exit_code``); ``None`` means it never exits on its own (so only
    SIGKILL can stop it).
    """

    def __init__(
        self,
        clock: FakeClock,
        *,
        exit_after: float | None = None,
        exit_code: int = 0,
    ) -> None:
        self._clock = clock
        self._exit_after = exit_after
        self._exit_code = exit_code
        self.signals: list[int] = []

    def poll(self) -> int | None:
        if self._exit_after is not None and self._clock.time() >= self._exit_after:
            return self._exit_code
        return None

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        # SIGKILL is unconditionally fatal; mark the process dead now so a
        # subsequent poll reflects the kill.
        if sig == signal.SIGKILL:
            self._exit_after = self._clock.time()
            self._exit_code = -signal.SIGKILL


def test_sigkill_not_sent_before_grace_window_then_sent_after() -> None:
    """A process that ignores SIGTERM gets SIGKILL only after the grace window.

    The fake process never exits on its own, so the ladder must wait out
    the full grace window before escalating. We assert SIGKILL is absent
    until the clock has advanced past ``grace_seconds`` and present after.
    """
    clock = FakeClock()
    proc = FakeProcess(clock, exit_after=None)
    grace = 15.0

    result = terminate_with_grace(
        proc,
        grace_seconds=grace,
        poll_interval=1.0,
        monotonic=clock.time,
        sleep=clock.sleep,
    )

    # SIGTERM fired first; SIGKILL fired exactly once, after SIGTERM.
    assert proc.signals[0] == signal.SIGTERM
    assert signal.SIGKILL in proc.signals
    assert proc.signals.count(signal.SIGKILL) == 1
    # SIGKILL was the escalation rung, so it must come after SIGTERM.
    assert proc.signals.index(signal.SIGKILL) > proc.signals.index(signal.SIGTERM)
    # The ladder did not escalate before the grace window elapsed: the
    # clock advanced to at least the grace window before SIGKILL fired.
    assert clock.now >= grace
    assert result.sigterm_sent is True
    assert result.sigkill_sent is True
    assert result.exited_on_term is False
    assert result.waited_seconds >= grace


def test_sigkill_not_sent_when_process_exits_within_grace() -> None:
    """A process that exits during the grace window is never SIGKILL'd."""
    clock = FakeClock()
    # Process dies 5 s into a 15 s window — SIGTERM was enough.
    proc = FakeProcess(clock, exit_after=5.0)

    result = terminate_with_grace(
        proc,
        grace_seconds=15.0,
        poll_interval=1.0,
        monotonic=clock.time,
        sleep=clock.sleep,
    )

    assert proc.signals == [signal.SIGTERM]
    assert signal.SIGKILL not in proc.signals
    assert result.sigterm_sent is True
    assert result.sigkill_sent is False
    assert result.exited_on_term is True
    # Resolved at the 5 s mark, well before the 15 s window expired.
    assert result.waited_seconds == pytest.approx(5.0)


def test_already_dead_process_sends_no_signals() -> None:
    """A process that is already dead on entry gets no signals at all."""
    clock = FakeClock()
    proc = FakeProcess(clock, exit_after=0.0)  # dead from t=0

    result = terminate_with_grace(
        proc,
        monotonic=clock.time,
        sleep=clock.sleep,
    )

    assert proc.signals == []
    assert result.sigterm_sent is False
    assert result.sigkill_sent is False
    assert result.exited_on_term is True


def test_default_grace_window_is_15_seconds() -> None:
    assert pytest.approx(15.0) == DEFAULT_GRACE_SECONDS


def test_terminate_returns_typed_result() -> None:
    clock = FakeClock()
    proc = FakeProcess(clock, exit_after=1.0)
    result = terminate_with_grace(
        proc, grace_seconds=5.0, poll_interval=1.0, monotonic=clock.time, sleep=clock.sleep
    )
    assert isinstance(result, TerminationResult)


def test_terminate_rejects_negative_grace() -> None:
    clock = FakeClock()
    proc = FakeProcess(clock, exit_after=None)
    with pytest.raises(ValueError, match="grace_seconds must be non-negative"):
        terminate_with_grace(proc, grace_seconds=-1.0, monotonic=clock.time, sleep=clock.sleep)


def test_terminate_rejects_non_positive_poll_interval() -> None:
    clock = FakeClock()
    proc = FakeProcess(clock, exit_after=None)
    with pytest.raises(ValueError, match="poll_interval must be positive"):
        terminate_with_grace(
            proc, grace_seconds=5.0, poll_interval=0.0, monotonic=clock.time, sleep=clock.sleep
        )
