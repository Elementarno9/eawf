"""Unit tests for the process-group cancel primitive (P29-I01-W25).

Pins the pgid-kill primitive the safety-floor HALT (FLOOR-6) wires to:

* :func:`~eawf.runtime.runtimes.cancel.cancel_process_group` — the
  one-shot group signal (``soft`` = SIGTERM the group, ``hard`` = SIGKILL
  the group).
* :func:`~eawf.runtime.runtimes.cancel.cancel_with_grace` — the soft ->
  grace-window -> hard escalation, which composes the existing
  :func:`~eawf.runtime.budget.service.terminate_with_grace` ladder over a
  process group.

``os.killpg`` and ``os.getpgid`` are ALWAYS mocked here — these tests
never deliver a real signal to a real process group (no real spawn, no
real kill). The group fan-out is observed through a fake ``killpg`` that
records ``(pgid, sig)`` calls; group liveness is driven by a fake
``getpgid`` whose deadness is toggled by the SIGKILL fan-out or by a fake
clock.
"""

from __future__ import annotations

import signal

import pytest

from eawf.runtime.budget.service import TerminationResult
from eawf.runtime.runtimes.cancel import (
    CancelResult,
    cancel_process_group,
    cancel_with_grace,
)

# ---------------------------------------------------------------------------
# Fakes: never a real signal / real process group
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic monotonic clock advanced manually by the test.

    ``time()`` returns the current value; ``sleep`` advances it without
    any real wall-clock delay, so the grace ladder resolves instantly.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeGroup:
    """Fake process group standing in for ``os.killpg`` / ``os.getpgid``.

    Records every ``(pgid, sig)`` the cancel path fans out. Liveness is
    modelled two ways so both cancel surfaces are testable without a real
    process:

    * ``alive`` — a hard SIGKILL fan-out flips it ``False`` immediately
      (SIGKILL is unconditionally fatal), so the grace ladder's next poll
      observes the group gone.
    * ``dead_after`` — an optional clock value at which the group leader
      becomes unreachable on its own (``getpgid`` raises
      ``ProcessLookupError``), modelling a group that exits during the
      grace window in response to SIGTERM.
    """

    def __init__(
        self,
        *,
        clock: FakeClock | None = None,
        alive: bool = True,
        dead_after: float | None = None,
    ) -> None:
        self._clock = clock
        self._alive = alive
        self._dead_after = dead_after
        self.signals: list[tuple[int, int]] = []

    def _is_dead(self) -> bool:
        if not self._alive:
            return True
        if self._dead_after is not None and self._clock is not None:
            return self._clock.time() >= self._dead_after
        return False

    def killpg(self, pgid: int, sig: int) -> None:
        if self._is_dead():
            raise ProcessLookupError(f"no such process group: {pgid}")
        self.signals.append((pgid, sig))
        # SIGKILL is unconditionally fatal — the group is dead from here.
        if sig == signal.SIGKILL:
            self._alive = False

    def getpgid(self, pgid: int) -> int:
        if self._is_dead():
            raise ProcessLookupError(f"no such process group: {pgid}")
        return pgid


# ---------------------------------------------------------------------------
# cancel_process_group — soft path (SIGTERM the group)
# ---------------------------------------------------------------------------


def test_cancel_process_group_soft_sends_sigterm_to_group() -> None:
    """A soft cancel delivers SIGTERM to the whole group via killpg."""
    group = FakeGroup()
    result = cancel_process_group(4321, killpg=group.killpg)

    assert group.signals == [(4321, signal.SIGTERM)]
    assert isinstance(result, CancelResult)
    assert result.pgid == 4321
    assert result.signal_sent == signal.SIGTERM
    assert result.delivered is True


def test_cancel_process_group_soft_is_the_default_mode() -> None:
    """Omitting ``hard`` defaults to the soft SIGTERM rung."""
    group = FakeGroup()
    cancel_process_group(9, killpg=group.killpg)
    assert group.signals == [(9, signal.SIGTERM)]


# ---------------------------------------------------------------------------
# cancel_process_group — hard path (SIGKILL the group)
# ---------------------------------------------------------------------------


def test_cancel_process_group_hard_sends_sigkill_to_group() -> None:
    """A hard cancel delivers SIGKILL to the whole group via killpg."""
    group = FakeGroup()
    result = cancel_process_group(7777, hard=True, killpg=group.killpg)

    assert group.signals == [(7777, signal.SIGKILL)]
    assert result.signal_sent == signal.SIGKILL
    assert result.delivered is True


def test_cancel_process_group_targets_the_supplied_pgid() -> None:
    """The pgid argument is the exact group the signal targets (the pid the
    on_spawn callback handed back, since the child is its own group leader)."""
    group = FakeGroup()
    cancel_process_group(5555, hard=True, killpg=group.killpg)
    pgid, _sig = group.signals[0]
    assert pgid == 5555


# ---------------------------------------------------------------------------
# cancel_process_group — already-exited group (idempotent no-op)
# ---------------------------------------------------------------------------


def test_cancel_process_group_already_dead_reports_not_delivered() -> None:
    """A group that is already gone is a no-op, not an error (soft)."""
    group = FakeGroup(alive=False)
    result = cancel_process_group(4321, killpg=group.killpg)

    assert group.signals == []
    assert result.delivered is False
    assert result.signal_sent == signal.SIGTERM
    assert result.pgid == 4321


def test_cancel_process_group_hard_already_dead_reports_not_delivered() -> None:
    """A hard cancel of an already-gone group is also a tolerated no-op."""
    group = FakeGroup(alive=False)
    result = cancel_process_group(4321, hard=True, killpg=group.killpg)

    assert group.signals == []
    assert result.delivered is False
    assert result.signal_sent == signal.SIGKILL


# ---------------------------------------------------------------------------
# cancel_process_group — pgid validation (fail-fast boundary)
# ---------------------------------------------------------------------------


def test_cancel_process_group_rejects_zero_pgid() -> None:
    """pgid 0 (the caller's own group) is rejected before any signal."""
    group = FakeGroup()
    with pytest.raises(ValueError, match="pgid must be a positive group id"):
        cancel_process_group(0, killpg=group.killpg)
    assert group.signals == []


def test_cancel_process_group_rejects_negative_pgid() -> None:
    """A negative pgid is rejected before any signal is delivered."""
    group = FakeGroup()
    with pytest.raises(ValueError, match="pgid must be a positive group id"):
        cancel_process_group(-1, killpg=group.killpg)
    assert group.signals == []


# ---------------------------------------------------------------------------
# cancel_with_grace — soft -> grace -> hard escalation
# ---------------------------------------------------------------------------


def test_cancel_with_grace_escalates_soft_to_hard_after_window() -> None:
    """A group that ignores SIGTERM is SIGKILL'd only after the grace window."""
    clock = FakeClock()
    # Never dies on its own — only the SIGKILL fan-out can stop it.
    group = FakeGroup(clock=clock, alive=True)

    result = cancel_with_grace(
        4321,
        grace_seconds=15.0,
        poll_interval=1.0,
        monotonic=clock.time,
        sleep=clock.sleep,
        killpg=group.killpg,
        getpgid=group.getpgid,
    )

    sent = [sig for _pgid, sig in group.signals]
    # SIGTERM fanned out first, SIGKILL exactly once as the escalation rung.
    assert sent[0] == signal.SIGTERM
    assert sent.count(signal.SIGKILL) == 1
    assert sent.index(signal.SIGKILL) > sent.index(signal.SIGTERM)
    # Every signal targeted the requested group.
    assert {pgid for pgid, _sig in group.signals} == {4321}
    # The ladder did not escalate before the grace window elapsed.
    assert clock.now >= 15.0
    assert isinstance(result, TerminationResult)
    assert result.sigterm_sent is True
    assert result.sigkill_sent is True
    assert result.exited_on_term is False


def test_cancel_with_grace_soft_only_when_group_exits_in_window() -> None:
    """A group that exits during the window in response to SIGTERM is never
    SIGKILL'd — soft was enough."""
    clock = FakeClock()
    # Group leader becomes unreachable 5 s into a 15 s window.
    group = FakeGroup(clock=clock, alive=True, dead_after=5.0)

    result = cancel_with_grace(
        4321,
        grace_seconds=15.0,
        poll_interval=1.0,
        monotonic=clock.time,
        sleep=clock.sleep,
        killpg=group.killpg,
        getpgid=group.getpgid,
    )

    sent = [sig for _pgid, sig in group.signals]
    assert sent == [signal.SIGTERM]
    assert signal.SIGKILL not in sent
    assert result.sigterm_sent is True
    assert result.sigkill_sent is False
    assert result.exited_on_term is True
    assert result.waited_seconds == pytest.approx(5.0)


def test_cancel_with_grace_already_dead_group_sends_nothing() -> None:
    """A group already gone on entry gets no signals at all (ladder no-op)."""
    clock = FakeClock()
    group = FakeGroup(clock=clock, alive=False)

    result = cancel_with_grace(
        4321,
        grace_seconds=15.0,
        poll_interval=1.0,
        monotonic=clock.time,
        sleep=clock.sleep,
        killpg=group.killpg,
        getpgid=group.getpgid,
    )

    assert group.signals == []
    assert result.sigterm_sent is False
    assert result.sigkill_sent is False
    assert result.exited_on_term is True


def test_cancel_with_grace_rejects_non_positive_pgid() -> None:
    """pgid validation fires before any signal is sent."""
    group = FakeGroup()
    with pytest.raises(ValueError, match="pgid must be a positive group id"):
        cancel_with_grace(0, killpg=group.killpg, getpgid=group.getpgid)
    assert group.signals == []


def test_cancel_with_grace_propagates_negative_grace_rejection() -> None:
    """A negative grace window is rejected by the underlying ladder."""
    clock = FakeClock()
    group = FakeGroup(clock=clock, alive=True)
    with pytest.raises(ValueError, match="grace_seconds must be non-negative"):
        cancel_with_grace(
            4321,
            grace_seconds=-1.0,
            monotonic=clock.time,
            sleep=clock.sleep,
            killpg=group.killpg,
            getpgid=group.getpgid,
        )
