"""Service layer for per-wave token budgets.

Two concerns live here:

* **State mutation** — :func:`set_budget`, :func:`record_consumption`,
  and :func:`check_budget` operate on a live
  :class:`~eawf.kernel.state.models.State` and mutate the targeted
  :class:`~eawf.kernel.state.models.Wave` in place. They do **not** touch
  ``state.json`` directly — persistence is the CLI / daemon handler's
  job (it wraps the call in the locked transaction).

* **Process termination ladder** — :func:`terminate_with_grace`
  implements the SIGTERM -> grace-window -> SIGKILL escalation the
  ``hard`` budget-enforce mode (and the daemon wave-wall-clock cap) use
  to stop a runaway wave subprocess. SIGTERM is sent first; SIGKILL only
  fires after the grace window elapses *and* the process is still alive.
  The clock and sleep are injectable so the ladder is deterministically
  testable against a fake process.

Contract (state mutation):

* :func:`set_budget` — assign or revise a wave's budget. Negative
  budgets are rejected with :class:`ValueError`. Unknown waves raise
  :class:`KeyError`.
* :func:`record_consumption` — add positive ``tokens`` to
  ``Wave.tokens_consumed`` and return the wave plus its post-add policy
  classification. Negative deltas are :class:`ValueError`. Unknown
  waves raise :class:`KeyError`.
* :func:`check_budget` — read-only classify the wave against
  :mod:`eawf.runtime.budget.policy`.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from eawf.kernel.state.models import State, Wave
from eawf.runtime.budget.policy import classify

logger = logging.getLogger(__name__)

#: Default grace window (seconds) between SIGTERM and SIGKILL. Mirrors the
#: daemon wave-wall-clock cap ladder ("SIGTERM at cap, SIGKILL +15 s").
DEFAULT_GRACE_SECONDS: float = 15.0

#: How often (seconds) the ladder polls the process for liveness while
#: waiting out the grace window. Kept small so a process that exits early
#: is reaped promptly rather than blocking for the whole window.
_POLL_INTERVAL_SECONDS: float = 0.1


def _get_wave_or_raise(state: State, wave_id: str) -> Wave:
    wave = state.waves.get(wave_id)
    if wave is None:
        raise KeyError(f"unknown wave: {wave_id!r}")
    return wave


def set_budget(state: State, wave_id: str, budget: int) -> Wave:
    """Set ``Wave.token_budget`` for *wave_id*.

    Args:
        state: Live state. Mutated in place.
        wave_id: Wave to update.
        budget: Non-negative token cap. ``0`` is permitted (instantly
            "over-budget" once any consumption is recorded).

    Raises:
        KeyError: Wave does not exist.
        ValueError: ``budget`` is negative.
    """
    if budget < 0:
        raise ValueError(f"budget must be non-negative; got {budget}")
    wave = _get_wave_or_raise(state, wave_id)
    wave.token_budget = budget
    logger.info(f"set_budget wave={wave_id} budget={budget}")
    return wave


def record_consumption(
    state: State,
    wave_id: str,
    tokens: int,
) -> tuple[Wave, str | None]:
    """Accumulate *tokens* into ``Wave.tokens_consumed`` and classify.

    Args:
        state: Live state. Mutated in place.
        wave_id: Wave to update.
        tokens: Non-negative consumption delta. ``0`` is a no-op for the
            counter; the classification is still re-evaluated.

    Returns:
        Tuple of the mutated :class:`Wave` and the classification
        string from :func:`eawf.runtime.budget.policy.classify` after the
        increment (``None`` when no budget configured).

    Raises:
        KeyError: Wave does not exist.
        ValueError: ``tokens`` is negative.
    """
    if tokens < 0:
        raise ValueError(f"tokens must be non-negative; got {tokens}")
    wave = _get_wave_or_raise(state, wave_id)
    wave.tokens_consumed += tokens
    tag = classify(wave.tokens_consumed, wave.token_budget)
    logger.info(
        f"record_consumption wave={wave_id} delta={tokens} "
        f"consumed={wave.tokens_consumed} budget={wave.token_budget} tag={tag}"
    )
    return wave, tag


def check_budget(state: State, wave_id: str) -> str | None:
    """Return the policy classification for *wave_id* without mutation.

    Raises:
        KeyError: Wave does not exist.
    """
    wave = _get_wave_or_raise(state, wave_id)
    return classify(wave.tokens_consumed, wave.token_budget)


class TerminableProcess(Protocol):
    """Minimal process surface the termination ladder drives.

    Mirrors the subset of :class:`subprocess.Popen` the ladder needs, so
    a fake can be substituted in tests without spawning a real process.
    """

    def poll(self) -> int | None:
        """Return the exit code, or ``None`` while the process is alive."""
        ...

    def send_signal(self, sig: int) -> None:
        """Deliver signal *sig* to the process."""
        ...


@dataclass(frozen=True, slots=True)
class TerminationResult:
    """Outcome of a :func:`terminate_with_grace` call.

    Attributes:
        sigterm_sent: Always ``True`` when a live process was signalled
            (SIGTERM is the first rung); ``False`` only when the process
            was already dead before the ladder started.
        sigkill_sent: ``True`` when the grace window elapsed with the
            process still alive and SIGKILL was delivered.
        exited_on_term: ``True`` when the process exited during the grace
            window (so SIGKILL was unnecessary).
        waited_seconds: Total time spent polling the grace window before
            the ladder resolved.
    """

    sigterm_sent: bool
    sigkill_sent: bool
    exited_on_term: bool
    waited_seconds: float


def terminate_with_grace(
    proc: TerminableProcess,
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    poll_interval: float = _POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TerminationResult:
    """Run the SIGTERM -> grace -> SIGKILL termination ladder on *proc*.

    The ladder sends SIGTERM, then polls *proc* until either it exits or
    the grace window elapses. SIGKILL is delivered **only** when the
    window is fully exhausted and the process is still alive — never
    before. A process that is already dead on entry is a no-op (no signal
    is sent).

    The ``monotonic`` clock and ``sleep`` callables are injectable so the
    ladder is deterministic under test: a fake clock advances the window
    in controlled steps and a fake sleep records the requested durations
    without real wall-clock delay.

    Args:
        proc: The process to terminate (see :class:`TerminableProcess`).
        grace_seconds: Seconds to wait after SIGTERM before escalating to
            SIGKILL. Must be non-negative.
        poll_interval: Seconds between liveness polls within the window.
            Must be strictly positive.
        monotonic: Monotonic clock source (defaults to
            :func:`time.monotonic`).
        sleep: Sleep function (defaults to :func:`time.sleep`).

    Returns:
        A :class:`TerminationResult` recording which signals fired and how
        long the ladder waited.

    Raises:
        ValueError: ``grace_seconds`` is negative or ``poll_interval`` is
            not strictly positive.
    """
    if grace_seconds < 0:
        raise ValueError(f"grace_seconds must be non-negative; got {grace_seconds}")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be positive; got {poll_interval}")

    if proc.poll() is not None:
        # Process already dead — nothing to signal.
        logger.info("terminate_with_grace already_dead=true sigterm=false sigkill=false")
        return TerminationResult(
            sigterm_sent=False,
            sigkill_sent=False,
            exited_on_term=True,
            waited_seconds=0.0,
        )

    proc.send_signal(signal.SIGTERM)
    logger.info(f"terminate_with_grace sigterm=true grace={grace_seconds}")

    start = monotonic()
    while True:
        if proc.poll() is not None:
            waited = monotonic() - start
            logger.info(
                f"terminate_with_grace exited_on_term=true sigkill=false waited={waited:.3f}"
            )
            return TerminationResult(
                sigterm_sent=True,
                sigkill_sent=False,
                exited_on_term=True,
                waited_seconds=waited,
            )
        if monotonic() - start >= grace_seconds:
            break
        sleep(poll_interval)

    proc.send_signal(signal.SIGKILL)
    waited = monotonic() - start
    logger.info(f"terminate_with_grace sigkill=true waited={waited:.3f}")
    return TerminationResult(
        sigterm_sent=True,
        sigkill_sent=True,
        exited_on_term=False,
        waited_seconds=waited,
    )
