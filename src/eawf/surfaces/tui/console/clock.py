"""The console clock and the timed behaviours that read it.

Every timed console behaviour (the go-prefix cancellation, the guarded-quit window, toast
expiry) compares a session timestamp against :meth:`Clock.now` here, and nothing is ever
handed to the toolkit's scheduler. A deadline is therefore checked on the next key or
tick rather than fired by a callback, so a frame depends only on its setup, its keys and
its rack. The product reads the monotonic clock; the golden harness holds a
:class:`FakeClock` it never advances, which holds every deadline where it was, and the
timing tests advance one explicitly.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import NamedTuple

from eawf.surfaces.tui.console.format import seconds
from eawf.surfaces.tui.console.session import Session, Toast
from eawf.surfaces.tui.console.tokens import Severity

# How long an armed go prefix waits for its destination letter.
PREFIX_TIMEOUT = 1.5
# A second Escape closer than this is one leaked terminal burst, not two presses.
QUIT_FLOOR = 0.080
# A second Escape later than this re-arms instead of quitting.
QUIT_CEILING = 1.5
# How long a toast stands after it was raised.
TOAST_DWELL = 5.0
# The rack never holds more toasts than this; a newer one drops the oldest.
RACK_MAX = 3
# The key-log key (an em dash) a clock-driven change is recorded under, since no key
# caused it.
TICK_KEY = "—"


class Clock:
    """The console's time source, in seconds on the monotonic clock."""

    def now(self) -> float:
        """Return the current monotonic reading."""
        return time.monotonic()


class FakeClock(Clock):
    """A clock that moves only when a test advances it.

    Args:
        start: The first reading; far enough from zero that a disarmed guard's zero
            timestamp never looks recent.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def now(self) -> float:
        """Return the held reading."""
        return self._now

    def advance(self, by: float) -> None:
        """Move the clock forward by ``by`` seconds.

        Raises:
            ValueError: ``by`` is negative; a monotonic clock never runs backwards.
        """
        if by < 0:
            raise ValueError(f"a monotonic clock cannot move back by {by}")
        self._now += by


def arm_prefix(session: Session, clock: Clock) -> int:
    """Arm the go prefix with its own sequence number and deadline.

    A later arming overwrites the deadline, so an earlier arming's expiry can never cancel
    it.

    Returns:
        The arming's sequence number.
    """
    session.prefix = "g"
    session.prefix_seq += 1
    session.prefix_deadline = clock.now() + PREFIX_TIMEOUT
    return session.prefix_seq


def expire_prefix(session: Session, clock: Clock) -> bool:
    """Drop an armed go prefix whose own deadline has passed, and count the cancellation.

    Returns:
        Whether the prefix was dropped.
    """
    deadline = session.prefix_deadline
    if session.prefix != "g" or deadline is None or clock.now() < deadline:
        return False
    session.prefix = None
    session.prefix_deadline = None
    session.prefix_cancels += 1
    session.log_key(TICK_KEY, f"prefix timed out after {seconds(PREFIX_TIMEOUT)}")
    return True


class QuitStep(StrEnum):
    """What one Escape at a quiet scope home does to the quit guard."""

    ARMED = "armed"
    QUIT = "quit"
    BURST = "burst"


class QuitCheck(NamedTuple):
    """The guard's answer to one Escape.

    Attributes:
        step: Whether the press armed the guard, quit, or was too close to be a press.
        gap_ms: Milliseconds since the arming press; ``0`` when the guard was disarmed.
    """

    step: QuitStep
    gap_ms: int


def quit_step(session: Session, clock: Clock) -> QuitCheck:
    """Apply one Escape to the guarded double-Escape quit.

    A disarmed guard arms. An armed guard quits when the second press lands strictly
    between the floor and the ceiling, stays armed on a press inside the floor, and
    re-arms on a press past the ceiling. The caller decides the press is eligible (scope
    home, nothing open, empty back stack).

    Returns:
        The step taken and the gap it was judged on.
    """
    now = clock.now()
    if not session.last_esc:
        session.last_esc = now
        return QuitCheck(QuitStep.ARMED, 0)
    gap = now - session.last_esc
    gap_ms = int(gap * 1000)
    if QUIT_FLOOR < gap < QUIT_CEILING:
        session.disarm_quit()
        return QuitCheck(QuitStep.QUIT, gap_ms)
    if gap <= QUIT_FLOOR:
        return QuitCheck(QuitStep.BURST, gap_ms)
    session.last_esc = now
    return QuitCheck(QuitStep.ARMED, gap_ms)


def notify(session: Session, clock: Clock, *, text: str, title: str, sev: Severity) -> None:
    """Raise a toast stamped with the console clock, dropping the oldest past the cap."""
    session.toasts.append(Toast(title=title, text=text, sev=sev, at=clock.now()))
    del session.toasts[:-RACK_MAX]


def sweep_toasts(session: Session, clock: Clock) -> list[Toast]:
    """Expire every toast that has stood for its dwell, logging each one.

    Returns:
        The expired toasts, oldest first; empty when the rack did not change.
    """
    now = clock.now()
    session.rack_last = now
    gone = [toast for toast in session.toasts if now - toast.at >= TOAST_DWELL]
    if gone:
        session.toasts = [toast for toast in session.toasts if now - toast.at < TOAST_DWELL]
        for toast in gone:
            session.log_key(TICK_KEY, f"toast expired · {toast.title} · {seconds(TOAST_DWELL)}")
    return gone
