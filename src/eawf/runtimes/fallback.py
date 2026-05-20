"""Runtime fallback ladder — V5 reactive switchover + V8 fall-through.

C04d §5 wires the runtime fallback ladder into the skill -> adapter
handshake. Two distinct fall-throughs share this module:

* **V8 fall-through (F-d02).** Adapter session-handle *resolution* fails
  mid-dispatch (a ``--continue`` resume cannot find/replay the session
  log). The daemon falls through to a *fresh* ``open_session`` on the
  same runtime, annotating the new attempt with
  :attr:`DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH`. No runtime
  switch happens — the session policy degrades from continue to fresh.
* **V5 reactive switchover.** A dispatch *fails* with a normalized
  error class (C07a §5.5). Availability errors
  (``RUNTIME_SERVER_ERROR`` / ``RUNTIME_TIMEOUT`` / ``RUNTIME_API_ERROR``)
  fall through immediately to the next runtime in
  ``Wave.runtime_preference``; ``RUNTIME_RATE_LIMIT`` retries the same
  runtime once before falling through; ``RUNTIME_AUTH_ERROR`` **halts**
  with ``BLOCKED`` and never falls through (auth is not an availability
  signal). The switched attempt is annotated with
  :attr:`DispatchNote.SWITCH_ON_ERROR`.

This module owns the *policy* — the typed action for an error class and
the typed annotation a fall-through produces. It performs no I/O and
spawns no subprocess; the daemon dispatch router (C07a §5.8) drives it.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from eawf.runtimes.adapter import ALL_ERROR_CLASSES, ErrorClass
from eawf.state.enums import DispatchNote
from eawf.state.models import DispatchAnnotation
from eawf.state.types import UtcDatetime

logger = logging.getLogger(__name__)


class FallbackAction(StrEnum):
    """Action the V5 ladder takes for a normalized error class (C07a §5.5).

    - :attr:`RETRY_SAME` — retry the *same* runtime once (rate limit;
      honour ``Retry-After`` cap). A second failure escalates to
      :attr:`SWITCH_RUNTIME`.
    - :attr:`SWITCH_RUNTIME` — immediate fall-through to the next runtime
      in ``Wave.runtime_preference`` (server / timeout / generic API
      errors are availability signals).
    - :attr:`HALT` — stop the wave with ``BLOCKED``; never fall through
      (auth failures are not availability signals — switching runtimes
      cannot fix an expired token).
    """

    RETRY_SAME = "retry_same"
    SWITCH_RUNTIME = "switch_runtime"
    HALT = "halt"


#: V5 error-class -> action map (C07a §5.5 fallback-policy table). The
#: map is total over :data:`ALL_ERROR_CLASSES`; an error class missing a
#: row is a programming error caught by the module-level totality check.
_FALLBACK_POLICY: dict[ErrorClass, FallbackAction] = {
    "RUNTIME_RATE_LIMIT": FallbackAction.RETRY_SAME,
    "RUNTIME_SERVER_ERROR": FallbackAction.SWITCH_RUNTIME,
    "RUNTIME_TIMEOUT": FallbackAction.SWITCH_RUNTIME,
    "RUNTIME_API_ERROR": FallbackAction.SWITCH_RUNTIME,
    "RUNTIME_AUTH_ERROR": FallbackAction.HALT,
}

# Totality guard: every canonical error class has exactly one action.
# Kept as an import-time assertion so a new error class without a
# fallback row fails fast rather than silently defaulting.
assert set(_FALLBACK_POLICY) == set(ALL_ERROR_CLASSES)


def fallback_action(error_class: ErrorClass) -> FallbackAction:
    """Return the V5 ladder action for *error_class* (C07a §5.5).

    Args:
        error_class: A canonical error class from
            :data:`~eawf.runtimes.adapter.ErrorClass`.

    Returns:
        The :class:`FallbackAction` for the class.

    Raises:
        KeyError: *error_class* is not a canonical error class (the
            daemon coerces unknown adapter return values to
            ``RUNTIME_API_ERROR`` upstream, so this fires only on a
            programming error).
    """
    return _FALLBACK_POLICY[error_class]


def next_runtime_on_error(
    *,
    failed_runtime: str,
    preference: list[str],
    error_class: ErrorClass,
) -> str | None:
    """Pick the next runtime for a V5 reactive switchover.

    Walks ``preference`` past *failed_runtime* and returns the next
    distinct runtime when the error class is an availability signal
    (:attr:`FallbackAction.SWITCH_RUNTIME`). Returns ``None`` when the
    action is :attr:`FallbackAction.HALT` (auth — never switch), when the
    action is :attr:`FallbackAction.RETRY_SAME` (the caller retries the
    same runtime first), or when the preference ladder is exhausted.

    Args:
        failed_runtime: Runtime id whose dispatch just failed.
        preference: ``Wave.runtime_preference`` ordered runtime list.
        error_class: Normalized error class of the failure.

    Returns:
        The next runtime id to try, or ``None`` when no switch applies.
    """
    action = fallback_action(error_class)
    if action is not FallbackAction.SWITCH_RUNTIME:
        return None
    if failed_runtime in preference:
        tail = preference[preference.index(failed_runtime) + 1 :]
    else:
        # The failed runtime is not in the preference list (an explicit
        # override). Fall through to the whole preference list, skipping
        # the failed one.
        tail = preference
    for runtime_id in tail:
        if runtime_id != failed_runtime:
            return runtime_id
    return None


def fall_back_to_fresh(
    *,
    attempt: int,
    runtime: str,
    occurred_at: UtcDatetime,
    reason: str | None = None,
) -> DispatchAnnotation:
    """Build the V8 fall-through annotation for a resolution failure (F-d02).

    When an adapter's ``continue_session`` raises
    :class:`~eawf.runtimes.adapter.SessionResumeFailedError` (the session
    log is gone / expired / corrupt), the daemon opens a *fresh* session
    on the **same** runtime and records the degradation with
    :attr:`DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH`. No runtime
    switch happens, so ``runtime_from`` is left unset and ``runtime_to``
    is the unchanged runtime.

    Args:
        attempt: Attempt number of the fresh session this annotation
            belongs to.
        runtime: Runtime id (unchanged across the fall-through).
        occurred_at: Wall-clock timestamp of the fall-through.
        reason: Optional scrubbed context (e.g. the
            ``SessionResumeFailedError`` message, already scrubbed).

    Returns:
        A :class:`DispatchAnnotation` for the fresh attempt.
    """
    logger.info(f"fall_back_to_fresh runtime={runtime!r} attempt={attempt} note=continue_failed")
    return DispatchAnnotation(
        attempt=attempt,
        note=DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH,
        runtime_from=None,
        runtime_to=runtime,
        occurred_at=occurred_at,
        reason=reason,
    )


def switch_runtime_annotation(
    *,
    attempt: int,
    runtime_from: str,
    runtime_to: str,
    occurred_at: UtcDatetime,
    reason: str | None = None,
) -> DispatchAnnotation:
    """Build the V5 reactive-switchover annotation for a runtime swap.

    Records a runtime change driven by an availability error
    (:attr:`FallbackAction.SWITCH_RUNTIME`) with
    :attr:`DispatchNote.SWITCH_ON_ERROR`. Per C04d D-d3 the switch
    happens *between attempts* — the prior attempt is already closed; this
    annotation belongs to the new attempt on ``runtime_to``.

    Args:
        attempt: Attempt number of the new (switched) session.
        runtime_from: Runtime id of the failed prior attempt.
        runtime_to: Runtime id of the new attempt (per
            :func:`next_runtime_on_error`).
        occurred_at: Wall-clock timestamp of the switch.
        reason: Optional scrubbed context (e.g. the normalized error
            class string).

    Returns:
        A :class:`DispatchAnnotation` for the switched attempt.
    """
    logger.info(
        f"switch_runtime_annotation from={runtime_from!r} to={runtime_to!r} "
        f"attempt={attempt} note=switch_on_error"
    )
    return DispatchAnnotation(
        attempt=attempt,
        note=DispatchNote.SWITCH_ON_ERROR,
        runtime_from=runtime_from,
        runtime_to=runtime_to,
        occurred_at=occurred_at,
        reason=reason,
    )


__all__ = [
    "FallbackAction",
    "fall_back_to_fresh",
    "fallback_action",
    "next_runtime_on_error",
    "switch_runtime_annotation",
]
