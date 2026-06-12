"""Process-group cancel primitive for live runtime spawns.

The spawn seam
(:meth:`~eawf.runtime.runtimes.claude.adapter.ClaudeAdapter.spawn_session`)
starts every child with ``start_new_session=True``, so the child is its
own session **and** process-group leader: its pgid equals its pid. That
makes the whole subtree (the runtime CLI plus any helpers it forks)
addressable by a single process-group signal.

This module owns the pgid-group cancel primitive built on that
invariant:

* :func:`cancel_process_group` — the raw one-shot group signal. ``soft``
  delivers SIGTERM to the group; ``hard`` delivers SIGKILL. This is the
  primitive the safety-floor HALT (a later wave, FLOOR-6) wires to when
  a budget / wall-clock cap trips and a runaway wave's whole process
  tree must be stopped.
* :func:`cancel_with_grace` — the soft -> grace-window -> hard
  escalation. It does **not** re-implement the SIGTERM/SIGKILL ladder:
  it adapts the process group to the
  :class:`~eawf.runtime.budget.service.TerminableProcess` surface and
  drives the existing
  :func:`~eawf.runtime.budget.service.terminate_with_grace` ladder over
  the *group* rather than a single process, so the grace timing,
  liveness polling, and escalation invariants stay defined in exactly
  one place.

The single-process ladder in :mod:`eawf.runtime.budget.service` signals
one pid via ``proc.send_signal``; this module's contribution is the
*group* fan-out (``os.killpg``) plus the ``pid -> pgid`` resolution
(``os.getpgid``) so a caller that only retained the spawn pid (the
``on_spawn`` callback hands back the pid, not the pgid) can still reach
the whole group.

POSIX coupling: process groups, ``os.killpg``, and ``os.getpgid`` are
POSIX semantics, mirroring the spawn seam's ``start_new_session=True``.
A group that is already fully dead is a no-op rather than an error
(``ProcessLookupError`` is caught and reported, never raised), so a
cancel that races the subprocess's own exit is safe.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass

from eawf.runtime.budget.service import (
    DEFAULT_GRACE_SECONDS,
    TerminationResult,
    terminate_with_grace,
)

#: How often (seconds) the grace ladder polls the group for liveness.
#: Mirrors the single-process ladder's default so group and per-process
#: cancels poll at the same cadence.
_DEFAULT_POLL_INTERVAL_SECONDS: float = 0.1

logger = logging.getLogger(__name__)


def _unsupported_killpg(pgid: int, sig: int) -> None:
    """Reject a process-group signal on a platform without ``os.killpg``.

    Process groups are a POSIX construct: ``os.killpg`` / ``os.getpgid``
    do not exist on Windows, where the spawn seam terminates a runaway
    tree through the named-pipe-side ``CREATE_NEW_PROCESS_GROUP`` +
    ``TerminateProcess`` path instead. Binding this stub as the default
    ``killpg`` argument lets the module IMPORT on Windows (the default
    is evaluated at def time) while a real call fails fast with a clear
    message rather than an opaque ``AttributeError``.

    Raises:
        NotImplementedError: Always, on a platform without ``os.killpg``.
    """
    raise NotImplementedError("process-group cancel (os.killpg) is POSIX-only")


def _unsupported_getpgid(pid: int) -> int:
    """Reject a process-group lookup on a platform without ``os.getpgid``.

    The Windows counterpart of :func:`_unsupported_killpg` for the
    group-liveness probe. See that function for the import-time rationale.

    Raises:
        NotImplementedError: Always, on a platform without ``os.getpgid``.
    """
    raise NotImplementedError("process-group lookup (os.getpgid) is POSIX-only")


#: Module-bound group-signal syscall. ``os.killpg`` on POSIX; a fail-fast
#: stub on Windows so the module imports there (the default-argument
#: bindings below read this at def time).
_KILLPG: Callable[[int, int], None] = getattr(os, "killpg", _unsupported_killpg)

#: Module-bound group-liveness probe. ``os.getpgid`` on POSIX; a fail-fast
#: stub on Windows for the same import-time reason as :data:`_KILLPG`.
_GETPGID: Callable[[int], int] = getattr(os, "getpgid", _unsupported_getpgid)


@dataclass(frozen=True, slots=True)
class CancelResult:
    """Outcome of a one-shot :func:`cancel_process_group` call.

    Attributes:
        pgid: The process-group id the signal targeted.
        signal_sent: The signal number delivered (``signal.SIGTERM`` for
            ``soft``, ``signal.SIGKILL`` for ``hard``) — even when the
            group was already gone, this records which rung was
            attempted.
        delivered: ``True`` when ``os.killpg`` accepted the signal;
            ``False`` when the group was already dead
            (``ProcessLookupError``) so no process received it.
    """

    pgid: int
    signal_sent: int
    delivered: bool


#: Signal sent for a ``soft`` cancel — request the group terminate.
_SOFT_SIGNAL: int = signal.SIGTERM

#: Signal sent for a ``hard`` cancel — force-kill the group. ``SIGKILL``
#: is POSIX-only; on Windows it is absent, so this falls back to
#: ``SIGTERM`` purely to let the module import (the group-signal stub
#: rejects any actual call on that platform before the value is used).
_HARD_SIGNAL: int = getattr(signal, "SIGKILL", signal.SIGTERM)


def cancel_process_group(
    pgid: int,
    *,
    hard: bool = False,
    killpg: Callable[[int, int], None] = _KILLPG,
) -> CancelResult:
    """Signal an entire process group by pgid (the pgid-kill primitive).

    ``soft`` (the default) delivers SIGTERM to every process in the
    group, asking it to terminate cleanly; ``hard`` delivers SIGKILL,
    force-killing the group. The signal fans out to the whole group via
    ``os.killpg`` so a runtime CLI that forked helpers is stopped as a
    unit rather than orphaning children.

    A group that is already fully dead is **not** an error: the
    ``ProcessLookupError`` ``os.killpg`` raises in that case is caught and
    reported as ``delivered=False`` so a HALT that races the
    subprocess's own exit is idempotent.

    This is the primitive the safety-floor HALT (FLOOR-6) wires to: the
    pgid comes from ``os.getpgid(pid)`` where ``pid`` is the value the
    spawn seam's ``on_spawn`` callback handed back (the child is its own
    group leader, so ``pgid == pid``).

    Args:
        pgid: Target process-group id. Must be a positive group leader id
            — ``0`` (caller's own group) and negative values are rejected
            so a HALT can never signal the daemon's own group by mistake.
        hard: When ``True`` send SIGKILL; otherwise send SIGTERM.
        killpg: Injection seam for the group-signal syscall (defaults to
            :func:`os.killpg`); tests substitute a fake so no real signal
            is delivered.

    Returns:
        A :class:`CancelResult` recording the pgid, the signal attempted,
        and whether it reached a live group.

    Raises:
        ValueError: ``pgid`` is not a positive integer.
    """
    if pgid <= 0:
        raise ValueError(f"pgid must be a positive group id; got {pgid}")

    sig = _HARD_SIGNAL if hard else _SOFT_SIGNAL
    mode = "hard" if hard else "soft"
    try:
        killpg(pgid, sig)
    except ProcessLookupError:
        logger.info(
            f"cancel_process_group pgid={pgid} mode={mode} delivered=false already_dead=true"
        )
        return CancelResult(pgid=pgid, signal_sent=sig, delivered=False)
    logger.info(f"cancel_process_group pgid={pgid} mode={mode} delivered=true")
    return CancelResult(pgid=pgid, signal_sent=sig, delivered=True)


@dataclass(slots=True)
class _ProcessGroupHandle:
    """Adapt a process group to the single-process termination surface.

    Satisfies the structural
    :class:`~eawf.runtime.budget.service.TerminableProcess` protocol
    (``poll`` + ``send_signal``) so the existing
    :func:`~eawf.runtime.budget.service.terminate_with_grace` ladder can
    drive a whole group: :meth:`send_signal` fans the signal out to the
    group via ``os.killpg`` instead of to one pid, and :meth:`poll`
    reports the group dead once its leader is gone.

    Liveness is probed off the group **leader** (``pgid``): a session
    leader started with ``start_new_session=True`` is the last member to
    be reaped under normal teardown, so ``os.getpgid(pgid)`` raising
    ``ProcessLookupError`` is a sound "group gone" signal for the cancel
    ladder.
    """

    pgid: int
    killpg: Callable[[int, int], None] = _KILLPG
    getpgid: Callable[[int], int] = _GETPGID

    def poll(self) -> int | None:
        """Return a non-``None`` sentinel once the group leader is gone.

        ``terminate_with_grace`` only checks ``poll() is not None`` to
        decide liveness, so the exact value is irrelevant; ``0`` stands
        in for "no longer running".
        """
        try:
            self.getpgid(self.pgid)
        except ProcessLookupError:
            return 0
        return None

    def send_signal(self, sig: int) -> None:
        """Deliver *sig* to the whole group, tolerating an already-dead group."""
        try:
            self.killpg(self.pgid, sig)
        except ProcessLookupError:
            # The group exited between the liveness poll and this signal;
            # the ladder's next poll observes the exit. Nothing to do.
            logger.debug(f"send_signal pgid={self.pgid} sig={sig} already_dead=true")


def cancel_with_grace(
    pgid: int,
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    killpg: Callable[[int, int], None] = _KILLPG,
    getpgid: Callable[[int], int] = _GETPGID,
) -> TerminationResult:
    """Cancel a process group with a soft -> grace -> hard escalation.

    Sends the soft signal (SIGTERM) to the group, waits out
    ``grace_seconds`` while polling the group leader for liveness, and
    escalates to the hard signal (SIGKILL) only if the group is still
    alive when the window elapses — never before. The SIGTERM/grace/SIGKILL
    ladder itself is **not** re-implemented here: this composes the
    existing :func:`~eawf.runtime.budget.service.terminate_with_grace`
    over a :class:`_ProcessGroupHandle`, so the escalation invariants
    live in one place and only the group fan-out is new.

    Args:
        pgid: Target process-group id (the spawn child's pid, since the
            child is its own group leader). Must be positive.
        grace_seconds: Seconds to wait after the soft signal before
            escalating to the hard signal.
        poll_interval: Seconds between group-liveness polls within the
            grace window.
        monotonic: Monotonic clock source, injectable so the ladder is
            deterministic under test (defaults to :func:`time.monotonic`).
        sleep: Sleep function, injectable for the same reason (defaults to
            :func:`time.sleep`).
        killpg: Injection seam for the group-signal syscall (defaults to
            :func:`os.killpg`).
        getpgid: Injection seam for the group-liveness probe (defaults to
            :func:`os.getpgid`).

    Returns:
        The :class:`~eawf.runtime.budget.service.TerminationResult` from
        the underlying ladder, recording which signals fired and how long
        it waited.

    Raises:
        ValueError: ``pgid`` is not a positive integer (raised before any
            signal is sent), or ``grace_seconds`` / ``poll_interval`` are
            out of range (propagated from the underlying ladder).
    """
    if pgid <= 0:
        raise ValueError(f"pgid must be a positive group id; got {pgid}")

    handle = _ProcessGroupHandle(pgid=pgid, killpg=killpg, getpgid=getpgid)
    logger.info(f"cancel_with_grace pgid={pgid} grace={grace_seconds}")
    return terminate_with_grace(
        handle,
        grace_seconds=grace_seconds,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
    )


__all__ = [
    "CancelResult",
    "cancel_process_group",
    "cancel_with_grace",
]
