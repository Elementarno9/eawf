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

import threading
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import MeasurementQuality, MeasurementStatus
from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.kinds.event import Event, EventKind, EventPayload
from eawf.runtime.runtimes.stream_json import vendor_error_signal

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

    Carries the spawn-failure context (:attr:`exit_status` + :attr:`stderr` +
    :attr:`stdout`) so a caller can classify the failure into a canonical
    :data:`ErrorClass` for the V5 reactive-switch ladder. Both output streams
    are carried because the vendors disagree about which one a failure lands
    on: a non-zero exit writes to stderr, while a ``--output-format
    stream-json`` failure routes the vendor's error envelope to **stdout** and
    leaves stderr empty. Classifying from stderr alone therefore misses the
    whole stream-json failure population -- :func:`classify_stream_error`
    reads :attr:`stdout` so the taxonomy sees the stream the vendor actually
    wrote to.

    Attributes:
        exit_status: Subprocess exit code when known (``None`` for a
            parse-level failure with no exit context).
        stderr: Captured stderr bytes when known (``b""`` when the vendor
            wrote nothing there).
        stdout: Captured stdout bytes when known (``b""`` when the raise site
            has no output in hand). Carries the stream-json transcript whose
            error envelope :func:`classify_stream_error` reads.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_status: int | None = None,
        stderr: bytes = b"",
        stdout: bytes = b"",
    ) -> None:
        super().__init__(message)
        self.exit_status = exit_status
        self.stderr = stderr
        self.stdout = stdout


# ---------------------------------------------------------------------------
# Concurrent-spawn ceiling (the one effective cap)
# ---------------------------------------------------------------------------

CONCURRENT_SPAWN_CAP: Final[int] = 16
"""The one effective ceiling on live agent spawns in flight at once.

Every adapter reserves its slot from this module, so the ceiling is a
*process-wide* count rather than a per-vendor one: three vendor modules each
holding their own counter would have made the effective cap three times the
number any one of them stated, which is exactly the runaway fan-out the cap
exists to bound. Chosen to comfortably cover the parallel-wave fleet while
still bounding the blast radius of a dispatch-loop bug.
"""

CAP_RETRY_AFTER_SECONDS: Final[float] = 5.0
"""Seconds a cap-saturated caller should wait before re-requesting a slot.

A saturated cap is a *local* backpressure signal, not a vendor outage: the
slots free themselves as in-flight spawns finish, so the wait is short and
fixed rather than derived from a vendor ``Retry-After`` header.
"""


class ConcurrentSpawnCapError(RuntimeSpawnError):
    """Raised when a spawn would exceed :data:`CONCURRENT_SPAWN_CAP`.

    The spawn floor refuses to fork a new jailed child once the ceiling is
    already in flight, so a runaway dispatch loop fails fast at the spawn
    boundary rather than exhausting process / socket resources.

    Subclasses :class:`RuntimeSpawnError` so the bounded retry ladder
    (:func:`~eawf.workflow.dispatch.retry.spawn_with_retry`) catches a
    saturated cap on the same seam as every other spawn failure instead of
    letting it escape uncaught; the ladder reads :attr:`retry_after_seconds`
    to surface typed backpressure rather than switching runtimes (no other
    runtime has a free slot either -- the ceiling is process-wide).

    Attributes:
        inflight: Spawns already in flight when the slot was refused.
        cap: The ceiling in force (:data:`CONCURRENT_SPAWN_CAP`).
        retry_after_seconds: How long the caller should wait before
            re-requesting a slot.
    """

    def __init__(
        self,
        *,
        inflight: int,
        cap: int,
        retry_after_seconds: float = CAP_RETRY_AFTER_SECONDS,
    ) -> None:
        super().__init__(f"concurrent spawn cap reached: inflight={inflight} cap={cap}")
        self.inflight = inflight
        self.cap = cap
        self.retry_after_seconds = retry_after_seconds


#: Live in-flight spawn counter + its lock. Module-global because the cap is
#: per-process (the daemon hosts every spawn); guarded by a lock so the
#: increment / cap-check is atomic under the asyncio + worker-thread mix the
#: daemon runs spawns on.
_spawn_inflight: int = 0
_spawn_lock = threading.Lock()


def acquire_spawn_slot() -> None:
    """Reserve one in-flight spawn slot or fail fast at the ceiling.

    Raises:
        ConcurrentSpawnCapError: :data:`CONCURRENT_SPAWN_CAP` spawns are
            already in flight.
    """
    global _spawn_inflight
    with _spawn_lock:
        if _spawn_inflight >= CONCURRENT_SPAWN_CAP:
            raise ConcurrentSpawnCapError(
                inflight=_spawn_inflight,
                cap=CONCURRENT_SPAWN_CAP,
            )
        _spawn_inflight += 1


def release_spawn_slot() -> None:
    """Release one in-flight spawn slot (never drops below zero)."""
    global _spawn_inflight
    with _spawn_lock:
        _spawn_inflight = max(0, _spawn_inflight - 1)


def spawn_inflight() -> int:
    """Return the number of spawn slots currently reserved."""
    with _spawn_lock:
        return _spawn_inflight


# ---------------------------------------------------------------------------
# Stream-aware error classification (§5.5)
# ---------------------------------------------------------------------------

#: Vendor error-type token -> canonical class. Keyed on the machine-readable
#: token the vendor stamps on its error envelope (never on the human-readable
#: message, which is not a stable contract). Spans the Anthropic vocabulary the
#: claude / opencode lanes surface and the OpenAI vocabulary codex surfaces.
_CLASS_FOR_ERROR_TYPE: Final[dict[str, ErrorClass]] = {
    "authentication_error": RUNTIME_AUTH_ERROR,
    "invalid_api_key": RUNTIME_AUTH_ERROR,
    "permission_error": RUNTIME_AUTH_ERROR,
    "permission_denied": RUNTIME_AUTH_ERROR,
    "insufficient_quota": RUNTIME_AUTH_ERROR,
    "rate_limit_error": RUNTIME_RATE_LIMIT,
    "rate_limit_exceeded": RUNTIME_RATE_LIMIT,
    "overloaded_error": RUNTIME_SERVER_ERROR,
    "server_error": RUNTIME_SERVER_ERROR,
    "api_error": RUNTIME_SERVER_ERROR,
    "deadline_exceeded": RUNTIME_TIMEOUT,
    "timeout": RUNTIME_TIMEOUT,
    "timeout_error": RUNTIME_TIMEOUT,
    "error_max_turns": RUNTIME_API_ERROR,
    "invalid_request_error": RUNTIME_API_ERROR,
    "not_found_error": RUNTIME_API_ERROR,
    "request_too_large": RUNTIME_API_ERROR,
}

#: HTTP status -> canonical class. The second reading of a vendor error
#: envelope, consulted when the envelope names no error-type token.
_CLASS_FOR_STATUS: Final[dict[int, ErrorClass]] = {
    400: RUNTIME_API_ERROR,
    401: RUNTIME_AUTH_ERROR,
    403: RUNTIME_AUTH_ERROR,
    404: RUNTIME_API_ERROR,
    408: RUNTIME_TIMEOUT,
    413: RUNTIME_API_ERROR,
    422: RUNTIME_API_ERROR,
    429: RUNTIME_RATE_LIMIT,
    500: RUNTIME_SERVER_ERROR,
    502: RUNTIME_SERVER_ERROR,
    503: RUNTIME_SERVER_ERROR,
    504: RUNTIME_TIMEOUT,
    529: RUNTIME_SERVER_ERROR,
}


def classify_stream_error(stdout: bytes) -> ErrorClass | None:
    """Classify a failure from the stream-json payload the vendor wrote to stdout.

    Under ``--output-format stream-json`` a failing call routes the vendor's
    error envelope to stdout and leaves stderr empty, so the per-adapter
    :meth:`RuntimeAdapter.parse_error` stderr ladder sees nothing and returns
    its ``RUNTIME_API_ERROR`` default for every such failure regardless of the
    real cause. This reads the stream the vendor actually wrote to: it lifts
    the structured error fields off the transcript
    (:func:`~eawf.runtime.runtimes.stream_json.vendor_error_signal`) and maps
    them through :data:`_CLASS_FOR_ERROR_TYPE` then :data:`_CLASS_FOR_STATUS`.

    Returns ``None`` rather than a default when the payload names no
    recognised signal, so the caller falls through to the adapter's stderr
    ladder instead of this reading masking it.

    Args:
        stdout: Raw captured stdout bytes of the failed spawn.

    Returns:
        The canonical :data:`ErrorClass` the payload names, or ``None`` when
        it names none.
    """
    if not stdout:
        return None
    signal = vendor_error_signal(stdout.decode(errors="replace"))
    if signal is None:
        return None
    if signal.error_type is not None:
        by_type = _CLASS_FOR_ERROR_TYPE.get(signal.error_type.strip().lower())
        if by_type is not None:
            return by_type
    if signal.status_code is not None:
        return _CLASS_FOR_STATUS.get(signal.status_code)
    return None


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
        measurement_quality: Provenance quality of the parsed usage.
        measurement_status: Whether token usage was actually observed.
        measurement_reason: Machine-readable cause when usage is unavailable.
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
    input_tokens: Annotated[int, Field(ge=0)] | None = 0
    output_tokens: Annotated[int, Field(ge=0)] | None = 0
    cache_creation_input_tokens: Annotated[int, Field(ge=0)] | None = 0
    cache_creation_5m_input_tokens: Annotated[int, Field(ge=0)] | None = 0
    cache_creation_1h_input_tokens: Annotated[int, Field(ge=0)] | None = 0
    cache_read_input_tokens: Annotated[int, Field(ge=0)] | None = 0
    cost_usd_reported: Decimal | None = None
    measurement_quality: MeasurementQuality = MeasurementQuality.EXACT
    measurement_status: MeasurementStatus = MeasurementStatus.USAGE_OBSERVED
    measurement_reason: Annotated[str, Field(min_length=1, max_length=200)] | None = None
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
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
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

        The optional *on_chunk* async callback fires once per stdout line AS
        IT ARRIVES (the adapter reads the child's stdout incrementally rather
        than buffering the whole output to process exit), so a downstream
        wave can surface model output live. Each call receives the decoded
        line string (trailing newline preserved for an interior line; the
        final partial line at EOF carries none). The full stdout is still
        accumulated and fed to the existing result parser unchanged, so with
        ``on_chunk=None`` the returned :class:`SpawnResult` is byte-equivalent
        to the buffered path.

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
            on_chunk: Optional async callback invoked once per stdout line as
                it arrives (live streaming); ``None`` (the default) leaves the
                spawn byte-equivalent to the buffered path.

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
    "CAP_RETRY_AFTER_SECONDS",
    "CONCURRENT_SPAWN_CAP",
    "RUNTIME_API_ERROR",
    "RUNTIME_AUTH_ERROR",
    "RUNTIME_RATE_LIMIT",
    "RUNTIME_SERVER_ERROR",
    "RUNTIME_TIMEOUT",
    "ConcurrentSpawnCapError",
    "DispatchEventKind",
    "ErrorClass",
    "RuntimeAdapter",
    "RuntimeSpawnError",
    "SessionResumeFailedError",
    "SpawnResult",
    "acquire_spawn_slot",
    "classify_stream_error",
    "emit_runtime_event",
    "release_spawn_slot",
    "spawn_inflight",
]
