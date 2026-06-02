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
  :class:`~eawf.kernel.store.kinds.event.Event` rows for the three
  dispatch-side event kinds adapters emit: ``runtime_switched``,
  ``session_continued``, ``session_failover``. The ``Event`` model
  from :mod:`eawf.kernel.store.kinds.event` is the single source of truth;
  adapters never roll their own envelope.

Naming convention
-----------------

The adapter ``id`` strings are the canonical runtime identifiers used
in :class:`~eawf.kernel.state.models.SessionAttempt.runtime`,
``runtime.preference`` config keys, and the dispatch CLI flag:

* ``"claude-code"``
* ``"codex"``
* ``"opencode"``
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.kinds.event import Event, EventKind, EventPayload

if TYPE_CHECKING:
    from eawf.workflow.agents.specs.models import RoleContract

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
    :class:`~eawf.kernel.state.models.DispatchAnnotation` with
    ``DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH`` (per the V8
    fall-through).
    """


class RuntimeSpawnError(RuntimeError):
    """Raised when a live runtime spawn fails to produce a usable result.

    Covers a non-zero subprocess exit, empty stdout, an unparseable
    result envelope, a non-object envelope, or a runtime-reported error
    result. The daemon's dispatch path catches this so a raw
    :class:`json.JSONDecodeError` / partial-output failure surfaces as a
    typed adapter-layer error rather than leaking out of the spawn seam.

    Carries the optional spawn-failure context (:attr:`exit_status` +
    :attr:`stderr`) so a caller can feed it back through
    :meth:`RuntimeAdapter.parse_error` to classify the failure into a
    canonical :data:`ErrorClass` for the V5 reactive-switch ladder. The
    raise sites that have the subprocess exit + stderr in hand (a non-zero
    exit, a timeout) populate them; the parse-level raise sites (empty
    stdout, unparseable JSON) leave the defaults, so a classifier coerces
    them to the conservative ``RUNTIME_API_ERROR`` (a switch signal).

    Attributes:
        exit_status: Subprocess exit code when known (``None`` for a
            parse-level failure with no exit context).
        stderr: Captured stderr bytes when known (``b""`` for a
            parse-level failure).
    """

    def __init__(
        self,
        message: str,
        *,
        exit_status: int | None = None,
        stderr: bytes = b"",
    ) -> None:
        super().__init__(message)
        self.exit_status = exit_status
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Live-spawn outcome (transient; NOT state-resident)
# ---------------------------------------------------------------------------


class SpawnResult(BaseModel):
    """Outcome of one **live** runtime subprocess spawn.

    Transient — NOT state-resident. Carries the raw runtime ``text`` +
    the parsed per-call token usage from a single
    :meth:`~eawf.runtime.runtimes.claude.adapter.ClaudeAdapter.spawn_session`
    call. Distinct from
    :class:`~eawf.kernel.state.models.SessionAttempt` (the ``state.json``
    bookkeeping row) precisely because the raw ``text`` must never land
    in ``state.json`` per rule 16: a later wave validates + meters +
    persists a typed body from this result, then stamps the lean
    ``SessionAttempt``.

    The bare name ``SessionResult`` is already taken twice in the tree
    (the ``agent.session`` JSON-RPC response model and the session-store
    operation outcome), so the live-spawn result is named ``SpawnResult``
    to keep one canonical name per concept (rule 17). The schema-forced
    ``LLMAssistResult`` store a later wave adds wraps the validated body
    derived from this transient result.

    Attributes:
        session_id: Runtime-emitted session identifier.
        runtime: Adapter id that produced the result (e.g.
            ``"claude-code"``).
        model: Model alias/id the spawn was *requested* with (what the
            caller passed to ``--model``).
        resolved_model: Full model id the runtime actually billed against
            (claude reports this under ``modelUsage``); ``None`` when the
            envelope does not disclose it. A later metering writer prices
            against ``resolved_model or model`` so an alias like ``haiku``
            still resolves to a priced ledger row.
        subprocess_pid: PID of the spawned subprocess (always populated
            on a live spawn).
        exit_status: Subprocess exit code.
        text: Raw runtime answer text (the ``result`` field of the
            runtime JSON envelope). Never persisted to ``state.json``.
        input_tokens: Non-cached input tokens billed this call.
        output_tokens: Output tokens billed this call.
        cache_creation_input_tokens: Prompt-cache write tokens (total
            across both TTL tiers).
        cache_creation_5m_input_tokens: Cache-write tokens at the 5-minute
            TTL. When the envelope discloses no TTL split, the whole write
            total lands here (the conservative prior rate).
        cache_creation_1h_input_tokens: Cache-write tokens at the 1-hour
            TTL.
        cache_read_input_tokens: Prompt-cache read tokens.
        cost_usd_reported: Runtime self-reported cost when the envelope
            carries one (claude ``total_cost_usd``). A later metering
            writer prices independently via the Decimal ledger; this is a
            cross-check only.
        started_at: When the subprocess started.
        ended_at: When the subprocess exited.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    runtime: str = Field(min_length=1)
    model: str = Field(min_length=1)
    resolved_model: str | None = None
    subprocess_pid: Annotated[int, Field(ge=1)]
    exit_status: int
    text: str
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    cache_creation_input_tokens: Annotated[int, Field(ge=0)] = 0
    cache_creation_5m_input_tokens: Annotated[int, Field(ge=0)] = 0
    cache_creation_1h_input_tokens: Annotated[int, Field(ge=0)] = 0
    cache_read_input_tokens: Annotated[int, Field(ge=0)] = 0
    cost_usd_reported: Decimal | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RuntimeAdapter(Protocol):
    """Per-runtime dispatcher contract.

    Every concrete adapter implementation declares the five
    class-level attributes + the session methods
    (:meth:`open_session`, :meth:`spawn_session`,
    :meth:`continue_session`, :meth:`session_log_handle`) +
    :meth:`parse_error` + :meth:`supports_continue`. The Protocol is decorated
    ``@runtime_checkable`` so the daemon can validate third-party
    adapters with ``isinstance(adapter, RuntimeAdapter)`` at load
    time.

    Attributes:
        id: Canonical runtime identifier (e.g. ``"claude-code"``).
            Matches :attr:`~eawf.kernel.state.models.SessionAttempt.runtime`.
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
        role_contract: RoleContract | None = None,
    ) -> SessionAttempt:
        """Spawn a fresh subprocess for ``wave`` with ``prompt``.

        Returns the typed :class:`SessionAttempt` row the daemon
        appends to ``wave.sessions``. Implementations stamp
        ``attempt``, ``runtime``, ``session_id``,
        ``session_log_handle``, and ``started_at`` at minimum; the
        token / exit fields stay ``None`` until the subprocess
        completes.

        The optional *role_contract* keyword carries the typed projection
        of the dispatched wave's role
        (:class:`~eawf.workflow.agents.specs.models.RoleContract`); it
        feeds the spawn seam's role-driven knobs (``system_prompt``,
        ``allowed_tools``, ``denied_tools``, ``model``, ``memory``) so
        the freshly-spawned runtime receives the role registry's body
        rather than a hardcoded executor preamble. ``None`` (default)
        keeps the spawn byte-equivalent to the pre-W13 surface for
        callers that have not yet plumbed the contract through; the
        live subprocess spawn that consumes the contract lands in
        P26-SURFACES.
        """

    async def spawn_session(
        self,
        prompt: str,
        *,
        model: str,
        cwd: str | None = None,
        extra_args: Sequence[str] = (),
        denied_tools: Sequence[str] = (),
        timeout: float | None = None,
        on_spawn: Callable[[int], None] | None = None,
    ) -> SpawnResult:
        """Spawn a live runtime subprocess for ``prompt`` against ``model``.

        The vendor-neutral live-spawn seam: every adapter forks its own
        CLI binary headlessly (Claude ``claude -p``, Codex ``codex exec``,
        OpenCode ``opencode run``), captures the runtime's result envelope,
        and parses it into a transient
        :class:`SpawnResult` (raw answer text + the per-call token classes +
        the child pid + the exit status). The result is NOT state-resident:
        a later wave validates + meters + persists a typed body from it
        before stamping the lean
        :class:`~eawf.kernel.state.models.SessionAttempt` (rule 16 keeps the
        raw text out of ``state.json``).

        The *denied_tools* keyword is the per-wave sandbox deny-list the
        daemon resolves from ``state.sandbox_policies`` via
        :func:`eawf.runtime.sandbox.policy.resolve_denied_tools`. Each adapter
        maps it to its own runtime's deny flag so a spawned child CLI is
        actually launched with those tools disabled per the wave policy
        (Claude ``--disallowedTools``); an empty deny-list adds no flag,
        keeping the spawn byte-equivalent to a deny-free dispatch. The
        vendor flag spelling stays inside the adapter -- the daemon caller
        passes only the tool names, never a CLI flag.

        The optional *on_spawn* callback fires with the child PID the moment
        the subprocess exists -- before output is awaited -- so a cancel path
        can register the pid and halt a still-running call mid-flight.

        Args:
            prompt: Rendered prompt passed to the runtime CLI.
            model: Model alias/id the spawn is requested with. No hardcoded
                floor -- the caller resolves it (the routing decision feeds
                this).
            cwd: Working directory for the subprocess; ``None`` inherits the
                parent's.
            extra_args: Extra CLI args appended verbatim (the routing /
                structured-output escape hatch).
            denied_tools: Per-wave sandbox deny-list (tool names). Each
                adapter maps it to its runtime's deny flag; empty (the
                default) adds no flag.
            timeout: Wall-clock ceiling in seconds; ``None`` waits
                indefinitely. On expiry the child is killed and a typed
                error is raised.
            on_spawn: Optional callback invoked with the child PID right
                after spawn (before output is awaited).

        Returns:
            The validated :class:`SpawnResult` for the completed call.

        Raises:
            RuntimeSpawnError: the spawn timed out, exited non-zero, or
                returned an unparseable / error result envelope.
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

        Per :class:`~eawf.kernel.state.models.SessionAttempt.session_log_handle`
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
# ``EventKind`` literal at :data:`eawf.kernel.store.kinds.event.EventKind`;
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

    The :class:`~eawf.kernel.store.kinds.event.Event` model is the **single
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
    "RuntimeSpawnError",
    "SessionResumeFailedError",
    "SpawnResult",
    "emit_runtime_event",
]
