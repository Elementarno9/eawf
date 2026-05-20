"""Per-runtime adapter Protocol + supporting types.

This module defines the **single Protocol** every runtime adapter
implements. The daemon's dispatch router loads adapters from
``src/eawf/runtimes/<id>/adapter.py``; each implementation is a
plain class that satisfies the structural :class:`RuntimeAdapter`
Protocol.

Boundaries
----------

* :class:`RuntimeAdapter` — structural Protocol the daemon imports +
  uses to type its adapter registry. ``@runtime_checkable`` so the
  daemon can ``isinstance(adapter, RuntimeAdapter)`` at load time
  (catches third-party-adapter Protocol-mismatch).
* :data:`ErrorClass` — closed ``Literal`` of the five canonical
  error-class strings. Adapters return ONE of these from
  :meth:`RuntimeAdapter.parse_error`.
* :class:`SessionResumeFailedError` — raised by :meth:`continue_session`
  when the runtime cannot resume the session (deleted log, corrupted
  state, expired session); the daemon catches this and falls back to
  fresh via the V8 fall-through.
* :func:`emit_runtime_event` — helper that constructs canonical
  :class:`~eawf.store.kinds.event.Event` rows for the three
  dispatch-side event kinds adapters emit: ``runtime_switched``,
  ``session_continued``, ``session_failover``. The ``Event`` model
  from :mod:`eawf.store.kinds.event` is the single source of truth;
  adapters never roll their own envelope.

Naming convention
-----------------

The adapter ``id`` strings are the canonical runtime identifiers used
in :class:`~eawf.state.models.SessionAttempt.runtime`,
``runtime.preference`` config keys, and the dispatch CLI flag:

* ``"claude-code"``
* ``"codex"``
* ``"opencode"``
"""

from __future__ import annotations

from typing import Final, Literal, Protocol, runtime_checkable

from eawf.state.models import SessionAttempt, Wave
from eawf.state.types import UtcDatetime
from eawf.store.kinds.event import Event, EventKind, EventPayload

# ---------------------------------------------------------------------------
# Closed error-class set (§5.5)
# ---------------------------------------------------------------------------

ErrorClass = Literal[
    "RUNTIME_RATE_LIMIT",
    "RUNTIME_SERVER_ERROR",
    "RUNTIME_TIMEOUT",
    "RUNTIME_API_ERROR",
    "RUNTIME_AUTH_ERROR",
]
"""Canonical error-class set.

Adapters return ONE of these from :meth:`RuntimeAdapter.parse_error`;
the daemon validates against this closed set and treats an unknown
return value as ``RUNTIME_API_ERROR`` while emitting a
``runtime_error_class_unknown`` event."""

RUNTIME_RATE_LIMIT: Final[ErrorClass] = "RUNTIME_RATE_LIMIT"
RUNTIME_SERVER_ERROR: Final[ErrorClass] = "RUNTIME_SERVER_ERROR"
RUNTIME_TIMEOUT: Final[ErrorClass] = "RUNTIME_TIMEOUT"
RUNTIME_API_ERROR: Final[ErrorClass] = "RUNTIME_API_ERROR"
RUNTIME_AUTH_ERROR: Final[ErrorClass] = "RUNTIME_AUTH_ERROR"

ALL_ERROR_CLASSES: Final[tuple[ErrorClass, ...]] = (
    RUNTIME_RATE_LIMIT,
    RUNTIME_SERVER_ERROR,
    RUNTIME_TIMEOUT,
    RUNTIME_API_ERROR,
    RUNTIME_AUTH_ERROR,
)
"""Closed-set tuple for runtime iteration / validation (matches
:data:`ErrorClass` ordering exactly)."""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionResumeFailedError(Exception):
    """Raised when :meth:`RuntimeAdapter.continue_session` cannot resume.

    The daemon's dispatch router catches this exception and falls back
    to a fresh ``open_session`` call, annotating the resulting
    :class:`~eawf.state.models.DispatchAnnotation` with
    ``DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH`` (per the V8
    fall-through).
    """


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RuntimeAdapter(Protocol):
    """Per-runtime dispatcher contract.

    Every concrete adapter implementation declares the five
    class-level attributes + the four methods + :meth:`parse_error`
    + :meth:`supports_continue`. The Protocol is decorated
    ``@runtime_checkable`` so the daemon can validate third-party
    adapters with ``isinstance(adapter, RuntimeAdapter)`` at load
    time.

    Attributes:
        id: Canonical runtime identifier (e.g. ``"claude-code"``).
            Matches :attr:`~eawf.state.models.SessionAttempt.runtime`.
        cli_binary: Bare CLI binary name (e.g. ``"claude"``).
        accepts_continue: Whether the runtime supports session
            resume via a ``--continue`` / ``resume`` verb (V8
            cache-inheritance gate).
        supports_cache_control: Whether the runtime accepts
            caller-side ``cache_control`` markers (Claude only as
            of v0.3-v0.5; Codex + OpenCode mark this ``False``).
        error_classes_emitted: Subset of :data:`ALL_ERROR_CLASSES`
            this adapter actually produces from
            :meth:`parse_error`. Used by the daemon's monitoring
            surface to declare adapter capability.
    """

    id: str
    cli_binary: str
    accepts_continue: bool
    supports_cache_control: bool
    error_classes_emitted: tuple[ErrorClass, ...]

    async def open_session(
        self,
        wave: Wave,
        prompt: str,
        *,
        cache_prefix: str | None = None,
        model_hint: str | None = None,
    ) -> SessionAttempt:
        """Spawn a fresh subprocess for ``wave`` with ``prompt``.

        Returns the typed :class:`SessionAttempt` row the daemon
        appends to ``wave.sessions``. Implementations stamp
        ``attempt``, ``runtime``, ``session_id``,
        ``session_log_handle``, and ``started_at`` at minimum; the
        token / exit fields stay ``None`` until the subprocess
        completes.
        """

    async def continue_session(
        self,
        session_id: str,
        prompt: str,
    ) -> SessionAttempt:
        """Resume a prior session by ``session_id``.

        Raises:
            SessionResumeFailedError: The runtime cannot resume (deleted
                log, expired session, corrupted state). Daemon
                catches and falls back to :meth:`open_session`.
        """

    def session_log_handle(
        self,
        session_id: str,
    ) -> str:
        """Return the daemon-internal opaque handle for the session log.

        Per :class:`~eawf.state.models.SessionAttempt.session_log_handle`
        (rule 16 secrets / PII hygiene): the returned string is an
        opaque URN-shaped handle the daemon resolves via its
        in-process map — never a filesystem path stamped onto
        ``state.json``.
        """

    def parse_error(
        self,
        exit_status: int,
        stderr: bytes,
    ) -> ErrorClass:
        """Map subprocess exit_status + stderr to a canonical class.

        Returns one of the five strings in :data:`ErrorClass`. Per
        §5.5 each adapter applies its runtime-specific stderr
        pattern matching (HTTP code substrings, error keywords).
        The daemon validates the return against the closed set;
        unknown values are coerced to ``RUNTIME_API_ERROR``.
        """

    def supports_continue(self) -> bool:
        """Whether this adapter supports :meth:`continue_session`.

        OpenCode's v0.3 adapter returns ``False`` until the session
        path catalog is fully verified; the daemon treats every
        dispatch as fresh under that branch.
        """


# ---------------------------------------------------------------------------
# Event emission helpers (canonical Event model)
# ---------------------------------------------------------------------------


# The dispatch-side event kinds adapters emit. Subset of the closed
# ``EventKind`` literal at :data:`eawf.store.kinds.event.EventKind`;
# narrowing here documents what adapters are allowed to emit (the
# router emits the wave / phase lifecycle kinds).
DispatchEventKind = Literal[
    "runtime_switched",
    "session_continued",
    "session_failover",
]


def emit_runtime_event(
    *,
    event_id: str,
    scope_id: str,
    occurred_at: UtcDatetime,
    event_kind: DispatchEventKind,
    actor: str,
    command: str,
    args_hash: str,
    status: str,
    message: str,
    error_class: ErrorClass | None = None,
    extras: dict[str, str | int | float | bool] | None = None,
    idempotency_key: str | None = None,
) -> Event:
    """Construct a canonical :class:`Event` for adapter-side emission.

    The :class:`~eawf.store.kinds.event.Event` model is the **single
    source of truth** — adapters do NOT roll their own envelope. This
    helper centralises the construction so every
    adapter populates ``event_kind`` from the closed Literal subset
    :data:`DispatchEventKind` (rules out emitting wave-lifecycle
    kinds from the adapter layer).

    Args:
        event_id: Event identifier (caller-allocated; format follows
            ``e-<YYYY-MM-DD>-<seq>-<kind>``).
        scope_id: Scope URN-or-id the event belongs to (typically a
            wave id when the event is emitted from dispatch).
        occurred_at: UTC timestamp of the event.
        event_kind: One of ``runtime_switched`` / ``session_continued``
            / ``session_failover``.
        actor: Identity string of the emitter (typically the adapter
            id, e.g. ``"claude-code"``).
        command: Command surface that triggered the event
            (e.g. ``"agent.dispatch"``).
        args_hash: Stable hash of the call args (for replay dedup).
        status: Outcome status string (e.g. ``"ok"`` / ``"failed"``).
        message: Human-readable line.
        error_class: Optional canonical error-class string when the
            event accompanies a failure path.
        extras: Optional structured key/value extras (limited to
            primitive JSON-safe values).
        idempotency_key: Optional UUID-v4 carried from the dispatch
            envelope (V5 cross-runtime re-issue dedup window).

    Returns:
        Validated :class:`Event` ready for the subscription bus / the
        event JSONL store. The caller (typically the daemon)
        persists the row.
    """

    # ``event_kind`` is the closed-Literal DispatchEventKind subset
    # at the adapter layer; assigning it into the broader
    # EventKind-typed field is structurally sound.
    payload_kind: EventKind = event_kind
    payload = EventPayload(
        timestamp=occurred_at,
        event_type=event_kind,
        event_kind=payload_kind,
        actor=actor,
        command=command,
        args_hash=args_hash,
        status=status,
        message=message,
        error_class=error_class,
        extras=extras or {},
    )
    return Event(
        id=event_id,
        scope_id=scope_id,
        occurred_at=occurred_at,
        idempotency_key=idempotency_key,
        payload=payload,
    )


__all__ = [
    "ALL_ERROR_CLASSES",
    "RUNTIME_API_ERROR",
    "RUNTIME_AUTH_ERROR",
    "RUNTIME_RATE_LIMIT",
    "RUNTIME_SERVER_ERROR",
    "RUNTIME_TIMEOUT",
    "DispatchEventKind",
    "ErrorClass",
    "RuntimeAdapter",
    "SessionResumeFailedError",
    "emit_runtime_event",
]
