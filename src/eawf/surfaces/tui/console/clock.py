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
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import NamedTuple

from eawf.surfaces.tui.console.format import seconds
from eawf.surfaces.tui.console.session import TOAST_DWELL, Session, Toast
from eawf.surfaces.tui.console.tokens import Severity

# How long an armed go prefix waits for its destination letter.
PREFIX_TIMEOUT = 1.5
# A second Escape closer than this is one leaked terminal burst, not two presses.
QUIT_FLOOR = 0.080
# A second Escape later than this re-arms instead of quitting.
QUIT_CEILING = 1.5
# The one toast an armed quit guard raises; it stands exactly as long as the guard.
QUIT_PROMPT = "Press again to exit."
# How a disarming note opens, so the rack never repeats it as a toast.
DISARMED = "quit disarmed"
# The rack never holds more toasts than this; a newer one drops the oldest.
RACK_MAX = 3
# The key-log key (an em dash) a clock-driven change is recorded under, since no key
# caused it.
TICK_KEY = "—"
# Where a held clock's wall readings start, so a stamped field is the same on every run.
_FAKE_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class Clock:
    """The console's time source, in seconds on the monotonic clock."""

    def now(self) -> float:
        """Return the current monotonic reading."""
        return time.monotonic()

    def wall(self) -> datetime:
        """Return the wall-clock instant a confirmed write stamps a field with."""
        return datetime.now(UTC)


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

    def wall(self) -> datetime:
        """Return a wall-clock instant that moves only with the held reading."""
        return _FAKE_EPOCH + timedelta(seconds=self._now)

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


def disarm(session: Session, key: str) -> None:
    """Disarm an armed quit guard, saying so in the key log so the outcome is observable."""
    if session.last_esc:
        session.disarm_quit()
        session.toasts = [toast for toast in session.toasts if toast.text != QUIT_PROMPT]
        session.log_key("Esc", f"{DISARMED} · {key} came between the presses")


def prompt_quit(session: Session, clock: Clock) -> None:
    """Raise the one quit prompt, standing for the guard's window and no longer."""
    notify(session, clock, text=QUIT_PROMPT, title="", sev=Severity.INFO, dwell=QUIT_CEILING)


def guarded_quit(session: Session, clock: Clock, *, outstanding: int) -> bool:
    """Apply one Escape at a quiet scope home to the guard, logging the step it took.

    An operation the daemon has not answered holds the guard disarmed, so the console is
    never left while a control's outcome is still owed.

    Args:
        session: The session whose guard and key log are used.
        clock: The console clock the gap is judged on.
        outstanding: How many sent operations still await the daemon's answer.

    Returns:
        Whether the console should quit now.
    """
    if outstanding:
        session.disarm_quit()
        session.log_key("Esc", f"{outstanding} outstanding · quit waits for the daemon's answer")
        return False
    check = quit_step(session, clock)
    if check.step is QuitStep.QUIT:
        note = f"quit — guarded: scope home, nothing open, {check.gap_ms}ms apart"
        session.log_key("Esc Esc", note)
        return True
    if check.step is QuitStep.BURST:
        session.log_key("Esc", "too fast to be two presses — still armed")
    else:
        session.log_key("Esc", "at scope home · press again within 1.5s to quit")
        prompt_quit(session, clock)
    return False


def notify(
    session: Session,
    clock: Clock,
    *,
    text: str,
    title: str,
    sev: Severity,
    dwell: float = TOAST_DWELL,
) -> None:
    """Raise a toast stamped with the console clock, dropping the oldest past the cap.

    A toast saying what the newest one already says replaces it rather than stacking a
    copy, so a key pressed again restarts the dwell of its answer instead of repeating it.
    """
    newest = session.toasts[-1] if session.toasts else None
    if newest is not None and (newest.text, newest.sev) == (text, sev):
        session.toasts.pop()
    session.toasts.append(Toast(title=title, text=text, sev=sev, at=clock.now(), dwell=dwell))
    del session.toasts[:-RACK_MAX]


def sweep_toasts(session: Session, clock: Clock) -> list[Toast]:
    """Expire every toast that has stood for its dwell, logging each one.

    Returns:
        The expired toasts, oldest first; empty when the rack did not change.
    """
    now = clock.now()
    session.rack_last = now
    gone = [toast for toast in session.toasts if now - toast.at >= toast.dwell]
    if gone:
        session.toasts = [toast for toast in session.toasts if now - toast.at < toast.dwell]
        for toast in gone:
            name = toast.title or toast.text
            session.log_key(TICK_KEY, f"toast expired · {name} · {seconds(toast.dwell)}")
    return gone
