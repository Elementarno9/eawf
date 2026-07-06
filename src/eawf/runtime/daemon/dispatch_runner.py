"""Daemon-side dispatch runner — emits C09 dispatch event payloads.

The runner is the daemon-internal component that drives a single wave
dispatch attempt and emits the C09 typed ``EventPayload`` sub-classes
(``runtime_switched`` on a V5 fallback, ``dispatch_cost`` post-dispatch)
defined in :mod:`eawf.kernel.store.kinds.events`.

Every event the runner produces is routed through the **daemon canonical
writer** for ``event.jsonl`` — :func:`eawf.kernel.store.append.append_envelope`
under the per-file portalock + fsync — and then published to the
subscription bus via :meth:`eawf.runtime.daemon.bus.EventBus.publish`. This
mirrors the :func:`eawf.runtime.daemon.methods.state.mutate` persistence path so
subscribers cannot tell a dispatch-runner event apart from a mutator
event: both converge on the same on-disk row. The runner never opens
``event.jsonl`` directly nor calls ``atomic_write_json`` — persistence
authority for the event store stays with the canonical append helper
(per the daemon-as-sole-mutator rule).

The typed payload is validated through :data:`C09EventPayloadUnion`
*before* it is folded into the generic
:class:`eawf.kernel.store.envelope.Envelope` ``payload`` dict, so a payload
whose body does not match its ``event_type`` discriminator fails fast
with :class:`pydantic.ValidationError` at emit time rather than at
projection time (the §5.11 discriminator-emit invariant).

On dispatch completion the runner also emits a typed ``agent_end``
executor report through the canonical agent-report writer
:func:`eawf.workflow.agent_report.store.append_agent_report` (the same writer the
operator-facing ``eawf hook event`` AGENT_END path uses). The report
uses the dispatched wave's executor :class:`~eawf.kernel.state.models.AgentSession`
as authority — role, scope, attempt, and store kind are derived from the
session — so the persisted row passes
:func:`eawf.kernel.validate.invariants.check_agent_report_invariants`. Report
emission is opt-in: it fires only when the caller supplies the
``session_id`` of the executor session AND the daemon context is wired to
an on-disk ``state.json`` (plan-only and stateless unit-test contexts
skip it).
"""
# noqa: EAWF010 cohesive dispatch-runner + live-output producer surface; the W45
# agent.output.chunk emitter belongs beside its sibling emit_agent_output (shared
# capture_output_lines + canonical event-store path), so it is kept here rather
# than split into a one-helper module.

from __future__ import annotations

import errno
import logging
import signal
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.io import state_version
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportBody,
    AuditorReportBody,
    DomainSpecialistReportBody,
    ExecutorReportBody,
    OperatorReportBody,
    PlannerReportBody,
    PolisherReportBody,
    ResearcherReportBody,
    ReviewerReportBody,
)
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.kinds.events import (
    AgentOutputChunkPayload,
    C09EventPayloadUnion,
    DispatchCostPayload,
    RuntimeSwitchedPayload,
)
from eawf.kernel.store.kinds.events.base import RuntimeTriple, TracedEventPayload
from eawf.observability.telemetry.models import RuntimeErrorClass
from eawf.runtime.budget.policy import DEFAULT_ENFORCE, DEFAULT_MULTIPLIER, EnforceMode
from eawf.runtime.budget.service import record_consumption
from eawf.runtime.daemon.budget_interlock import InterlockOutcome, enforce_token_cap
from eawf.runtime.lock import portalock
from eawf.runtime.runtimes.adapter import RuntimeSpawnError
from eawf.runtime.sandbox.egress_proxy import SandboxEnforcementEvent
from eawf.workflow.agent_report.store import append_agent_report
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle.wave import start_wave
from eawf.workflow.verify.dispatch_close import (
    DispatchCloseBlockedError,
    evidence_rung_inputs,
    verify_close_readiness,
)

if TYPE_CHECKING:
    from eawf.runtime.daemon.methods import MethodContext

logger = logging.getLogger(__name__)

#: Adapter used to validate a typed payload through the discriminated
#: union before it is folded into the envelope ``payload`` dict. Building
#: the adapter once at import keeps the per-emit cost to a dict round-trip.
_PAYLOAD_ADAPTER: TypeAdapter[Any] = TypeAdapter(C09EventPayloadUnion)


@dataclass(frozen=True)
class DispatchTokens:
    """Per-invocation token tally returned by a runtime dispatch.

    Attributes:
        input_tokens: Non-cached input tokens billed.
        output_tokens: Output tokens billed.
        cache_creation_input_tokens: Tokens written to the prompt cache.
        cache_read_input_tokens: Tokens served from the prompt cache.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int

    @property
    def total(self) -> int:
        """Return the sum of every billed token field for this dispatch.

        The live burn gauge tracks ``Wave.tokens_consumed`` as a single
        scalar, so the per-invocation accrual folds all four billed
        tallies (non-cached input, output, cache-creation, cache-read)
        into one delta.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of :func:`run_dispatch`.

    Attributes:
        runtime: Runtime that ultimately served the dispatch.
        attempt_id: Dispatch-attempt id of the serving attempt.
        switched: ``True`` when a V5 fallback fired and the dispatch
            switched runtimes mid-flight.
        event_ids: Ids of the C09 *event*-store envelopes the runner
            emitted, in append order (``runtime_switched`` first when a
            fallback fired, then ``dispatch_cost``).
        report_id: Envelope id of the typed ``agent_end`` executor report
            persisted on dispatch completion, or ``None`` when the caller
            supplied no executor ``session_id`` (or the daemon context
            carries no ``state.json``) and report emission was skipped.
            The report lands in the role-specific ``executor_report``
            store, not the event store, so it is intentionally kept off
            :attr:`event_ids`.
        terminated: ``True`` when the post-accrual token-cap interlock
            HALTed under ``hard`` enforce AND an addressable spawn pgid was
            threaded in, so the kill ladder reaped the wave's process group.
            ``False`` on every soft / under-cap dispatch and on a HALT with
            no addressable pgid.
    """

    runtime: RuntimeTriple
    attempt_id: str
    switched: bool
    event_ids: tuple[str, ...]
    report_id: str | None = None
    terminated: bool = False


class SpawnFailureClass(StrEnum):
    """Closed taxonomy of how a live agent-cli spawn failed -- DL-11.

    The fleet auto-drain loop spawns agents unattended, so a spawn that fails
    must classify into exactly one of these closed members rather than leaking a
    raw :class:`~eawf.runtime.runtimes.adapter.RuntimeSpawnError` that the loop
    cannot reason about. The classifier (:func:`classify_spawn_failure`) reads
    the failure's exit status to split a transient, retryable spawn failure from
    the two HARD failures that the bounded retry ladder must NOT respawn:

    - :attr:`RECOVERABLE` -- a transient spawn failure (a non-zero exit the
      retry ladder may recover via RETRY_SAME / SWITCH): the loop hands it to
      the bounded retry driver, which respawns up to ``max_total_attempts``
      before HALTing to a ``retry_exhausted`` fork.
    - :attr:`RUNTIME_SPAWN_ERROR` -- a hard launch failure (ENOENT / permission:
      the agent CLI binary is missing or not executable). The binary never
      starts, so it surfaces as a chained :class:`FileNotFoundError` /
      :class:`PermissionError` (an :class:`OSError` with the ENOENT / EACCES /
      EPERM errno) rather than a subprocess exit. No runtime switch and no retry
      can launch a binary that is not there, so the lane terminates cleanly to a
      ``runtime_spawn_error`` fork on the FIRST such failure.
    - :attr:`SUBPROCESS_OOM` -- the spawned subprocess was OOM-killed (the
      kernel reaped it via SIGKILL for exceeding memory, surfacing on the
      subprocess exit status as the negative ``-SIGKILL`` convention or the
      ``128 + SIGKILL`` shell convention). Respawning would re-OOM, so the lane
      terminates cleanly to a ``subprocess_oom`` fork rather than looping.
    """

    RECOVERABLE = "recoverable"
    RUNTIME_SPAWN_ERROR = "runtime_spawn_error"
    SUBPROCESS_OOM = "subprocess_oom"


#: POSIX SIGKILL signum. ``signal.SIGKILL`` is POSIX-only (absent on Windows),
#: so code paths below use this module constant instead of reading the signal
#: module directly at classification time.
_SIGKILL_SIGNUM = int(getattr(signal, "SIGKILL", 9))

#: Exit status of a subprocess reaped by SIGKILL on the POSIX shell convention
#: (``128 + signal``). The kernel OOM-killer delivers SIGKILL, so a child the
#: OOM-killer reaped surfaces this exit code when the parent reports the signal
#: as ``128 + signum`` rather than the negative ``-signum`` convention.
#: ``signal.SIGKILL`` is POSIX-only (absent on Windows), so the value is read
#: through ``getattr`` with the fixed POSIX signal number 9 as the fallback --
#: this keeps the constant identical (137) on every platform while letting the
#: daemon module graph import on Windows where the OOM-killer convention is moot.
_SIGKILL_EXIT_STATUS = 128 + _SIGKILL_SIGNUM

#: OSError errno values that mark a HARD launch failure -- the agent CLI binary
#: is missing (``ENOENT``) or not executable / not permitted (``EACCES`` /
#: ``EPERM``). A chained OSError carrying one of these is unrecoverable: no retry
#: or runtime switch can launch a binary that is not there.
_LAUNCH_FAILURE_ERRNOS = frozenset({errno.ENOENT, errno.EACCES, errno.EPERM})


def classify_spawn_failure(exc: RuntimeSpawnError) -> SpawnFailureClass:
    """Classify a live-spawn :class:`RuntimeSpawnError` into the DL-11 taxonomy.

    Splits the two HARD spawn failures the bounded retry ladder must NOT respawn
    from the transient, retryable rest. The two signals are read from DISTINCT
    places so a subprocess that merely exits non-zero (a recoverable failure) is
    never confused with a launch errno:

    - A launch failure (the agent CLI binary is missing or not executable)
      surfaces as a chained :class:`OSError` (``__cause__`` / ``__context__``) --
      a :class:`FileNotFoundError` / :class:`PermissionError` carrying an
      ``ENOENT`` / ``EACCES`` / ``EPERM`` errno -- because the process never
      started to produce an exit status. It classifies
      :attr:`SpawnFailureClass.RUNTIME_SPAWN_ERROR`.
    - A SIGKILL-reaped child (the kernel OOM-killer delivers SIGKILL, surfacing
      on the subprocess :attr:`~eawf.runtime.runtimes.adapter.RuntimeSpawnError.exit_status`
      as the negative ``-SIGKILL`` convention or the ``128 + SIGKILL`` shell
      convention) classifies :attr:`SpawnFailureClass.SUBPROCESS_OOM` --
      respawning would re-OOM.
    - Every other spawn failure (an ordinary non-zero exit, a timeout, an
      unparseable envelope, or a missing exit status) classifies
      :attr:`SpawnFailureClass.RECOVERABLE`, so the loop hands it to the bounded
      retry driver rather than terminating the lane on the first failure.

    Args:
        exc: The :class:`RuntimeSpawnError` the live spawn raised.

    Returns:
        The :class:`SpawnFailureClass` member the failure classifies to.
    """
    cause = exc.__cause__ or exc.__context__
    if isinstance(cause, OSError) and cause.errno in _LAUNCH_FAILURE_ERRNOS:
        return SpawnFailureClass.RUNTIME_SPAWN_ERROR
    status = exc.exit_status
    if status is not None and status in {-_SIGKILL_SIGNUM, _SIGKILL_EXIT_STATUS}:
        return SpawnFailureClass.SUBPROCESS_OOM
    return SpawnFailureClass.RECOVERABLE


def _event_path(ctx: MethodContext) -> Path:
    """Return the ``event.jsonl`` path the runner appends through.

    Args:
        ctx: Daemon method context.

    Returns:
        The configured ``event.jsonl`` path.

    Raises:
        RuntimeError: When ``ctx.event_path`` is not configured (the
            runner cannot route through the canonical writer without it).
    """
    if ctx.event_path is None:
        raise RuntimeError("event_path not configured on daemon context")
    return Path(ctx.event_path)


def _emit(
    ctx: MethodContext,
    payload: TracedEventPayload,
    *,
    scope_id: str | None,
    summary: str,
) -> str:
    """Persist *payload* as a ``StoreKind.EVENT`` envelope via the canonical writer.

    Validates *payload* through :data:`C09EventPayloadUnion` (fail-fast on
    a discriminator/body mismatch), wraps the validated body in the generic
    :class:`Envelope`, appends it through
    :func:`eawf.kernel.store.append.append_envelope` (the canonical event-store
    writer), then publishes to the subscription bus when one is attached.

    Args:
        ctx: Daemon method context — supplies ``event_path`` + ``bus``.
        payload: C09 typed event payload to persist.
        scope_id: Scope id stamped on the envelope (typically a wave id).
        summary: One-line human-readable envelope summary.

    Returns:
        The id of the appended envelope.

    Raises:
        RuntimeError: When ``ctx.event_path`` is not configured.
        pydantic.ValidationError: When *payload* fails discriminated-union
            validation (body does not match its ``event_type`` tag).
    """
    event_path = _event_path(ctx)
    body = _PAYLOAD_ADAPTER.validate_python(payload).model_dump(mode="json")
    now = datetime.now(UTC)
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=body,
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(event_path, envelope)
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id
    logger.info(
        f"emit event_type={body['event_type']!r} scope={scope_id!r} envelope_id={envelope.id!r}"
    )
    return envelope.id


def persist_enforcement_event(
    ctx: MethodContext,
    event: SandboxEnforcementEvent,
) -> str:
    """Persist one sandbox-enforcement event to the event feed.

    The sandbox boundary (egress proxy, env-scrub, argv-policy, cwd-guard)
    hands its :class:`~eawf.runtime.sandbox.egress_proxy.SandboxEnforcementEvent`
    here; the runner folds it into the generic
    :class:`~eawf.kernel.store.envelope.Envelope` as a flat
    :class:`~eawf.kernel.store.kinds.event.EventPayload` and appends it
    through the daemon canonical event writer
    (:func:`eawf.kernel.store.append.append_envelope` -- the same portalock +
    fsync path every other dispatch event takes). The five named
    enforcement fields (``ts``, ``session``, ``kind``, ``target``,
    ``severity``) ride the payload's ``extras`` map so a TUI denial-timeline
    surface can read the row off the event feed without a parallel store.

    Args:
        ctx: Daemon method context -- supplies ``event_path`` + ``bus``.
        event: The enforcement decision to persist.

    Returns:
        The id of the appended envelope.

    Raises:
        RuntimeError: When ``ctx.event_path`` is not configured.
    """
    event_path = _event_path(ctx)
    now = datetime.now(UTC)
    summary = (
        f"sandbox_enforcement kind={event.kind} target={event.target!r} severity={event.severity}"
    )
    payload = EventPayload(
        timestamp=event.ts,
        event_type=f"sandbox.enforcement.{event.kind}",
        actor="daemon",
        command="dispatch_runner.persist_enforcement_event",
        args_hash="",
        status=event.severity,
        message=summary,
        extras={
            "ts": event.ts.isoformat(),
            "session": event.session,
            "kind": event.kind,
            "target": event.target,
            "severity": event.severity,
        },
    ).model_dump(mode="json")
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=event.session or None,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(event_path, envelope)
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id
    logger.info(
        f"persist_enforcement_event kind={event.kind} session={event.session!r} "
        f"target={event.target!r} severity={event.severity} envelope_id={envelope.id!r}"
    )
    return envelope.id


def enforcement_sink(ctx: MethodContext) -> Callable[[SandboxEnforcementEvent], None]:
    """Return an :data:`~eawf.runtime.sandbox.egress_proxy.EnforcementSink` bound to *ctx*.

    The sandbox boundary takes a sink that accepts one
    :class:`~eawf.runtime.sandbox.egress_proxy.SandboxEnforcementEvent` and
    returns ``None``; this binds that sink to the daemon context so a
    boundary fire persists through :func:`persist_enforcement_event` (the
    canonical event-feed writer). Wiring the boundary with this sink is how
    a spawned session's argv-deny / egress-block / env-scrub / cwd-guard
    decisions reach the TUI denial timeline.

    Args:
        ctx: Daemon method context the persisted events are written through.

    Returns:
        The closure the sandbox boundary calls per enforcement decision.
    """

    def _sink(event: SandboxEnforcementEvent) -> None:
        persist_enforcement_event(ctx, event)

    return _sink


#: The ``event_type`` the live-output producer stamps on its envelope -- the
#: discriminator the TUI App keys on to route a row to the agent-watch tail
#: (FA4, W08) rather than the typed lifecycle stream.
AGENT_OUTPUT_EVENT_TYPE: str = "agent.output"

#: The ``event_type`` the LIVE per-chunk producer (W45) stamps on its envelope.
#: The TUI App keys on it to route a streamed batch of stdout lines to the same
#: agent-watch tail as the terminal ``agent.output`` row, but AS the spawn runs.
#: Unlike the terminal type this one is a typed C09 union member so it persists.
AGENT_OUTPUT_CHUNK_EVENT_TYPE: str = "agent.output.chunk"

#: Stdout lines the live-chunk producer buffers before flushing one chunk event.
#: Batching bounds ``event.jsonl`` growth (a chatty spawn emits hundreds of
#: lines); a final flush at spawn end empties any partial batch.
_CHUNK_BATCH_LINES: int = 20

#: Ring-buffer cap on the number of raw output lines one spawned session fans to
#: the live tail (W08). A spawn can emit a very large answer; capping the lines
#: the producer publishes bounds the event payload + the App-side
#: ``live_output_buffer`` so the tail never grows unbounded -- the operator reads
#: the freshest output, the oldest scrolls off. The TAIL of the output is kept
#: (the most recent lines) since that is what an operator watching a live run
#: cares about.
AGENT_OUTPUT_LINE_CAP: int = 200

#: Max characters of any single captured output line the producer publishes, so a
#: pathological no-newline blob cannot blow the event payload.
_AGENT_OUTPUT_LINE_MAX_CHARS: int = 2000


def capture_output_lines(text: str, *, cap: int = AGENT_OUTPUT_LINE_CAP) -> list[str]:
    """Split a spawn's captured *text* into the bounded tail of output lines -- W08, pure.

    The live-output producer captures the spawned child's stdout/stderr as the
    completed spawn's answer text; this splits it into non-empty lines and keeps
    the LAST *cap* of them (the freshest tail the operator watches), each bounded
    to :data:`_AGENT_OUTPUT_LINE_MAX_CHARS` so a no-newline blob cannot blow the
    payload. Blank lines are dropped so the tail carries signal, not whitespace.

    Args:
        text: The spawn's captured output text.
        cap: Maximum lines to keep (the ring-buffer tail cap).

    Returns:
        The bounded tail of non-empty output lines, oldest-first.
    """
    lines = [line[:_AGENT_OUTPUT_LINE_MAX_CHARS] for line in text.splitlines() if line.strip()]
    return lines[-cap:] if cap > 0 else []


def emit_agent_output(ctx: MethodContext, *, wave_id: str, text: str) -> str | None:
    """Fan a spawned session's captured stdout/stderr to the live output tail -- W08.

    The FA4 producer the agent-watch tail consumes: the dispatch runner captures
    the spawned child's output (the completed spawn's answer text) and publishes
    it as a single ``agent.output`` event carrying the bounded tail of output
    lines (:func:`capture_output_lines`). The TUI App keys on the
    :data:`AGENT_OUTPUT_EVENT_TYPE` discriminator to route the lines to the
    agent-watch session zoom's tail (``EaApp.append_output``) rather than the
    typed lifecycle stream, so the operator reads the agent's OWN words live.

    The event is persisted through the daemon canonical event writer (the same
    portalock + fsync path every dispatch event takes) and published on the bus.
    A spawn that produced no capturable output is a no-op (no empty event); a
    bus-less / event-less context (a stateless unit test) is likewise a no-op.

    Args:
        ctx: Daemon method context -- supplies ``event_path`` + ``bus``.
        wave_id: ``W<NN>`` wave the spawned session scopes to (the tail filters
            on this so a multi-lane fleet routes each line to its own session).
        text: The spawn's captured stdout/stderr text.

    Returns:
        The id of the appended envelope, or ``None`` when there was no output to
        fan (or no event store configured).
    """
    if ctx.event_path is None:
        return None
    lines = capture_output_lines(text)
    if not lines:
        return None
    now = datetime.now(UTC)
    summary = f"agent_output wave={wave_id} lines={len(lines)}"
    # The EventPayload ``extras`` map is scalar-valued (str|int|float|bool), so the
    # bounded line tail rides as one newline-joined ``lines`` string the App splits
    # back on the consumer side; ``line_count`` carries the count for the renderer.
    payload = EventPayload(
        timestamp=now,
        event_type=AGENT_OUTPUT_EVENT_TYPE,
        actor="daemon",
        command="dispatch_runner.emit_agent_output",
        args_hash="",
        status="ok",
        message=summary,
        extras={"wave_id": wave_id, "lines": "\n".join(lines), "line_count": len(lines)},
    ).model_dump(mode="json")
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=wave_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(Path(ctx.event_path), envelope)
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id
    logger.info(f"emit_agent_output wave={wave_id} lines={len(lines)} envelope_id={envelope.id!r}")
    return envelope.id


def emit_agent_output_chunk(
    ctx: MethodContext,
    *,
    wave_id: str,
    session_id: str | None,
    seq: int,
    text: str,
    trace_request_id: str | None = None,
) -> str | None:
    """Persist + fan one LIVE batch of a spawned session's stdout -- W45.

    The streaming counterpart of :func:`emit_agent_output`: the live-spawn path
    drives this AS the spawned child's stdout arrives (one call per batch) rather
    than only once at completion, so the Watch tail fills live AND the per-chunk
    output persists durably for review after the TUI is closed. The chunk rides a
    typed :class:`AgentOutputChunkPayload` (a C09 union member) through the same
    union-validated canonical-writer path :func:`_emit` takes; its ``lines`` field
    packs the batched text newline-joined, mirroring the terminal ``agent.output``
    event so the TUI render path is reused. Empty output or a store-less context
    (a stateless unit test) is a no-op.

    Args:
        ctx: Daemon method context -- supplies ``event_path`` + ``bus``.
        wave_id: ``W<NN>`` wave the spawned session scopes to (the Watch tail
            filters on this so a multi-lane fleet routes each chunk to its lane).
        session_id: Runtime session id of the spawn, or ``None`` when unknown.
        seq: Per-spawn monotonic chunk index (0-based) so the chunk order is
            reconstructible from the persisted rows.
        text: The batched output text for this chunk (one or more stdout lines).
        trace_request_id: Optional daemon RPC request id for the correlation
            chain.

    Returns:
        The id of the appended envelope, or ``None`` when there was no output to
        fan (or no event store configured).
    """
    if ctx.event_path is None:
        return None
    lines = capture_output_lines(text)
    if not lines:
        return None
    now = datetime.now(UTC)
    joined = "\n".join(lines)
    summary = f"agent_output_chunk wave={wave_id} seq={seq} lines={len(lines)}"
    payload = AgentOutputChunkPayload(
        timestamp=now,
        wave_id=wave_id,
        session_id=session_id,
        seq=seq,
        lines=joined,
        trace_request_id=trace_request_id,
        trace_wave_id=wave_id,
    )
    envelope_id = _emit(ctx, payload, scope_id=wave_id, summary=summary)
    logger.info(
        f"emit_agent_output_chunk wave={wave_id} seq={seq} lines={len(lines)} "
        f"envelope_id={envelope_id!r}"
    )
    return envelope_id


def emit_runtime_switched(
    ctx: MethodContext,
    *,
    wave_id: str,
    attempt_id_from: str,
    attempt_id_to: str,
    runtime_from: RuntimeTriple,
    runtime_to: RuntimeTriple,
    cause: RuntimeErrorClass,
    error_detail: str,
    idempotency_key: str,
    trace_request_id: str | None = None,
) -> str:
    """Emit a ``runtime_switched`` event for a V5 runtime fallback.

    Args:
        ctx: Daemon method context.
        wave_id: ``W<NN>`` wave whose dispatch switched runtimes.
        attempt_id_from: Attempt id that failed / was superseded.
        attempt_id_to: Attempt id minted for the replacement runtime.
        runtime_from: Runtime switched away from.
        runtime_to: Runtime switched to.
        cause: Typed :class:`~eawf.observability.telemetry.models.RuntimeErrorClass`
            member that triggered the switch (scrubbed).
        error_detail: Scrubbed stderr / failure detail for diagnosis.
        idempotency_key: De-dup key so a retried emit does not
            double-count the switchover.
        trace_request_id: Optional daemon RPC request id for the §5.8
            correlation chain.

    Returns:
        The id of the appended envelope.
    """
    payload = RuntimeSwitchedPayload(
        timestamp=datetime.now(UTC),
        wave_id=wave_id,
        attempt_id_from=attempt_id_from,
        attempt_id_to=attempt_id_to,
        runtime_from=runtime_from,
        runtime_to=runtime_to,
        cause=cause,
        error_detail=error_detail,
        idempotency_key=idempotency_key,
        trace_request_id=trace_request_id,
        trace_wave_id=wave_id,
        trace_attempt_id=attempt_id_to,
    )
    summary = f"runtime_switched wave={wave_id} {runtime_from}->{runtime_to} cause={cause}"
    return _emit(ctx, payload, scope_id=wave_id, summary=summary)


def emit_dispatch_cost(
    ctx: MethodContext,
    *,
    wave_id: str | None,
    attempt_id: str | None,
    runtime: RuntimeTriple,
    model: str,
    tokens: DispatchTokens,
    cost_usd: Decimal,
    pricing_version: str,
    trace_request_id: str | None = None,
) -> str:
    """Emit a ``dispatch_cost`` event after a dispatch attempt completes.

    Args:
        ctx: Daemon method context.
        wave_id: ``W<NN>`` wave the dispatch served, or ``None`` for an
            interactive (non-wave) CLI session.
        attempt_id: Attempt id of the serving attempt, or ``None`` for an
            interactive session.
        runtime: Runtime that incurred the cost.
        model: Model identifier the cost is priced against.
        tokens: Per-invocation token tally.
        cost_usd: Priced cost in USD.
        pricing_version: ``PRICING`` snapshot version used to compute
            ``cost_usd``.
        trace_request_id: Optional daemon RPC request id for the §5.8
            correlation chain.

    Returns:
        The id of the appended envelope.
    """
    payload = DispatchCostPayload(
        timestamp=datetime.now(UTC),
        wave_id=wave_id,
        attempt_id=attempt_id,
        runtime=runtime,
        model=model,
        input_tokens=tokens.input_tokens,
        output_tokens=tokens.output_tokens,
        cache_creation_input_tokens=tokens.cache_creation_input_tokens,
        cache_read_input_tokens=tokens.cache_read_input_tokens,
        cost_usd=cost_usd,
        pricing_version=pricing_version,
        trace_request_id=trace_request_id,
        trace_wave_id=wave_id,
        trace_attempt_id=attempt_id,
    )
    summary = f"dispatch_cost wave={wave_id} runtime={runtime} cost_usd={cost_usd}"
    return _emit(ctx, payload, scope_id=wave_id, summary=summary)


def _publish_state_revision(
    ctx: MethodContext,
    *,
    wave_id: str,
    before_version: str,
    after_version: str,
    tokens_consumed: int,
) -> None:
    """Publish a ``state_mutated`` revision envelope on the subscription bus.

    The accrual writes ``state.json`` directly under the runner's
    portalock (the daemon-internal canonical-writer path), so the
    mtime-poll STATE_REVISION feed advances on the next tick. This helper
    drives the **daemon-push** STATE_REVISION feed: it wakes live
    subscribers (the TUI burn gauge) immediately with a canonical
    ``state_mutated`` event envelope carrying the before/after state
    digests, mirroring the envelope :func:`eawf.runtime.daemon.methods.state.mutate`
    publishes so subscribers cannot tell the two paths apart.

    A bus-less context (stateless unit-test paths) is a no-op.

    Args:
        ctx: Daemon method context — supplies ``bus``.
        wave_id: ``W<NN>`` wave whose token burn advanced.
        before_version: State digest before the accrual write.
        after_version: State digest after the accrual write.
        tokens_consumed: The wave's post-accrual cumulative
            ``tokens_consumed`` value (carried on the envelope summary).
    """
    if ctx.bus is None or not hasattr(ctx.bus, "publish"):
        return
    now = datetime.now(UTC)
    summary = f"state_mutated wave={wave_id} tokens_consumed={tokens_consumed}"
    payload = EventPayload(
        timestamp=now,
        event_type="state.mutate.wave_accrue_tokens",
        event_kind="state_mutated",
        actor="daemon",
        command="dispatch_runner.accrue_tokens_consumed",
        args_hash="",
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=summary,
    ).model_dump(mode="json")
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=wave_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )
    ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id


def accrue_tokens_consumed(
    ctx: MethodContext,
    *,
    wave_id: str,
    tokens: DispatchTokens,
    pgid: int | None = None,
    enforce: EnforceMode = DEFAULT_ENFORCE,
) -> InterlockOutcome | None:
    """Fold a dispatch's token tally into ``Wave.tokens_consumed``, live.

    Called once per ``dispatch_cost`` event so the live burn gauge on the
    TUI advances **during** execution rather than only on the manual
    ``eawf wave budget consume`` CLI. The increment runs under the same
    defense-in-depth ``portalock(state.json)`` + locked-atomic-write the
    daemon's ``state.mutate`` path uses (per the daemon-as-sole-mutator
    rule), and delegates to :func:`eawf.runtime.budget.service.record_consumption`
    so the accrual reuses the canonical consume semantics rather than a
    raw field edit.

    The atomic state write bumps the ``state.json`` mtime (the mtime-poll
    STATE_REVISION feed), and :func:`_publish_state_revision` wakes live
    subscribers on the bus (the daemon-push STATE_REVISION feed) so both
    revision feeds advance.

    After the accrual persists, the safety-floor token-cap interlock
    (:func:`eawf.runtime.daemon.budget_interlock.enforce_token_cap`) classifies
    the post-increment burn against the wave's budget and, under ``hard``
    enforce at the cap, reaps the wave's spawned process group via the kill
    ladder. The *pgid* is the captured process-group id of the live spawn
    (the spawn's child pid, since the jail wrapper is the group leader): the
    live-dispatch caller threads it in so a hard-cap breach can
    ``os.killpg`` the runaway group (``terminated=True`` on the returned
    :class:`~eawf.runtime.daemon.budget_interlock.InterlockOutcome`). A
    ``None`` pgid (a stateless / plan-only caller) still computes + logs the
    decision but sends no signal. *enforce* defaults to the documented
    config default (``soft``, which never reaches HALT); the live caller
    threads the config-resolved mode so ``hard`` can fire.

    The accrual is opt-in and tolerant: it is skipped (returning ``None``)
    when ``ctx.state_path`` is unset (stateless unit-test contexts). The
    total delta is the sum of every billed token field
    (:attr:`DispatchTokens.total`); a zero delta still rewrites the state
    (bumping the revision) but leaves the counter unchanged.

    Args:
        ctx: Daemon method context — supplies ``state_path`` + ``bus``.
        wave_id: ``W<NN>`` wave the dispatch served.
        tokens: Per-invocation token tally from the dispatch.
        pgid: Process-group id of the wave's live spawn, threaded so a
            hard-cap breach can reap the group. ``None`` (the default)
            computes + logs the decision but signals nothing.
        enforce: Enforce mode the interlock runs under -- ``soft`` (default,
            never HALTs) or ``hard`` (HALTs + reaps at the cap).

    Returns:
        The :class:`~eawf.runtime.daemon.budget_interlock.InterlockOutcome`
        the token-cap interlock produced (carrying ``terminated`` +
        ``decision``), or ``None`` when the accrual was skipped (no
        ``state_path``).

    Raises:
        KeyError: When *wave_id* is absent from ``state.json`` — the
            dispatch served a wave that no longer exists in state, which
            is a fail-fast inconsistency the caller must surface.
    """
    if ctx.state_path is None:
        return None
    delta = tokens.total
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        before_version = state_version(state.model_dump(mode="json"))
        wave, _tag = record_consumption(state, wave_id, delta)
        state.updated_at = datetime.now(UTC)
        new_payload = state.model_dump(mode="json")
        after_version = state_version(new_payload)
        atomic_write_json_locked(state_path, new_payload)
        tokens_consumed = wave.tokens_consumed
        token_budget = wave.token_budget
    _publish_state_revision(
        ctx,
        wave_id=wave_id,
        before_version=before_version,
        after_version=after_version,
        tokens_consumed=tokens_consumed,
    )
    # Safety-floor token-cap interlock: classify the post-increment burn
    # against the wave's budget and, on a hard-enforce HALT, reap the wave's
    # spawned process group. Run outside the state portalock so the kill
    # ladder never signals while holding the state lock. The pgid is the
    # captured process-group id of the live spawn (None on stateless /
    # plan-only callers, which compute + log the decision but signal
    # nothing); enforce is the config-resolved mode the live caller threads.
    outcome = enforce_token_cap(
        consumed=tokens_consumed,
        base_budget=token_budget,
        enforce=enforce,
        multiplier=DEFAULT_MULTIPLIER,
        pgid=pgid,
    )
    logger.info(
        f"accrue_tokens_consumed wave={wave_id} delta={delta} consumed={tokens_consumed} "
        f"pgid={pgid} terminated={outcome.terminated}"
    )
    return outcome


def _completion_verdict(*, switched: bool) -> AgentReportVerdict:
    """Derive the executor report verdict from the dispatch outcome.

    A clean dispatch (no runtime switch) completes as ``pass``. A V5
    fallback means the primary runtime failed and a switch served the
    dispatch instead; the work still landed, so the verdict is
    ``pass-with-followups`` to flag the switchover for review rather than
    a hard ``fail``.

    Args:
        switched: ``True`` when a V5 fallback fired during the dispatch.

    Returns:
        The derived :class:`~eawf.kernel.state.enums.AgentReportVerdict`.
    """
    if switched:
        return AgentReportVerdict.PASS_WITH_FOLLOWUPS
    return AgentReportVerdict.PASS


def _build_completion_body(
    *,
    role: AgentSessionRole,
    wave_id: str,
    commit_sha: str,
    outcome: str,
    files_changed: list[str] | None,
    tests_run: list[str] | None,
    verdict: AgentReportVerdict,
    confidence: Confidence,
) -> AgentReportBody:
    """Return the typed completion body for a dispatched session *role*.

    Routes by the session's role so each role's dispatch lands a body
    of its own type — an auditor session lands an :class:`AuditorReportBody`,
    a reviewer lands a :class:`ReviewerReportBody`, and so on. The
    executor path keeps the rich body (``wave_id`` + ``commit_sha`` +
    ``files_changed`` + ``tests_run`` + ``outcome``); the other seven
    roles build a minimal completion body keyed by the role's
    required field (``target_id`` for auditor/reviewer, ``scope_id``
    for polisher, ``phase_id`` for operator, ``question`` /
    ``recommendation`` for researcher, ``objective`` for planner,
    ``domain`` / ``assessment`` for domain-specialist).

    The minimal-body strategy: use *wave_id* as the role-required id
    field and *outcome* as the role-required prose field, mirroring
    the dispatch runner's pre-W13 executor-only surface so the
    completion contract stays uniform across roles. Future waves
    under I02/I03 wire the rich per-role validators (criteria,
    coverage_refs, etc.); this seam opens the kind routing today
    without pre-building those validators.

    Args:
        role: The session role driving the body type selection.
        wave_id: Wave id used as the role-required id field for
            non-executor roles AND as the executor body's ``wave_id``.
        commit_sha: Executor commit SHA (used only when role is
            :attr:`AgentSessionRole.EXECUTOR`).
        outcome: One-line completion outcome (used by every body).
        files_changed: Repo-relative paths (executor only).
        tests_run: Test commands (executor only).
        verdict: Resolved report verdict.
        confidence: Report confidence.

    Returns:
        A typed :class:`AgentReportBody` discriminated-union member
        matching *role*.

    Raises:
        KeyError: When *role* has no mapped body class (cannot happen
            for a valid :class:`AgentSessionRole`).
    """
    if role is AgentSessionRole.EXECUTOR:
        return ExecutorReportBody(
            verdict=verdict,
            confidence=confidence,
            summary=outcome,
            wave_id=wave_id,
            files_changed=list(files_changed or []),
            tests_run=list(tests_run or []),
            commit_sha=commit_sha,
            outcome=outcome,
        )
    if role is AgentSessionRole.AUDITOR:
        return AuditorReportBody(
            verdict=verdict, confidence=confidence, summary=outcome, target_id=wave_id
        )
    if role is AgentSessionRole.REVIEWER:
        return ReviewerReportBody(
            verdict=verdict, confidence=confidence, summary=outcome, target_id=wave_id
        )
    if role is AgentSessionRole.POLISHER:
        return PolisherReportBody(
            verdict=verdict, confidence=confidence, summary=outcome, scope_id=wave_id
        )
    if role is AgentSessionRole.OPERATOR:
        return OperatorReportBody(
            verdict=verdict, confidence=confidence, summary=outcome, phase_id=wave_id
        )
    if role is AgentSessionRole.RESEARCHER:
        return ResearcherReportBody(
            verdict=verdict,
            confidence=confidence,
            summary=outcome,
            question=outcome,
            recommendation=outcome,
        )
    if role is AgentSessionRole.PLANNER:
        return PlannerReportBody(
            verdict=verdict, confidence=confidence, summary=outcome, objective=outcome
        )
    if role is AgentSessionRole.DOMAIN_SPECIALIST:
        return DomainSpecialistReportBody(
            verdict=verdict,
            confidence=confidence,
            summary=outcome,
            domain=wave_id,
            assessment=outcome,
        )
    raise KeyError(f"no completion body builder for role: {role.value!r}")


def emit_agent_end_report(
    ctx: MethodContext,
    *,
    session_id: str,
    wave_id: str,
    commit_sha: str,
    outcome: str,
    files_changed: list[str] | None = None,
    tests_run: list[str] | None = None,
    runtime: RuntimeTriple,
    verdict: AgentReportVerdict | None = None,
    confidence: Confidence = Confidence.HIGH,
    switched: bool = False,
    report_body: AgentReportBody | None = None,
) -> str:
    """Emit a typed ``agent_end`` report on dispatch completion.

    When *report_body* is supplied (the live-spawn path), it is the
    already-validated body parsed from the spawned agent's OWN output via
    the schema-assist re-ask loop, and it is persisted verbatim — the
    persisted row carries the agent's words (outcome / files_changed /
    verdict), not a runner-minted placeholder. When *report_body* is
    ``None`` (the hand-fed-outcome path and the direct unit-test callers)
    the runner builds the role-appropriate
    :class:`~eawf.kernel.store.kinds.agent_report.AgentReportCommonBody`
    subclass for the dispatched session and persists it through the
    canonical agent-report writer
    :func:`eawf.workflow.agent_report.store.append_agent_report`, using
    the :class:`~eawf.kernel.state.models.AgentSession` named by
    *session_id* as authority. The session's role drives both the body
    type (via :func:`_build_completion_body`) and the destination
    :class:`~eawf.kernel.state.enums.StoreKind` (via the writer's call
    to :func:`~eawf.kernel.store.kinds.agent_report.store_kind_for_role`),
    so the persisted envelope passes
    :func:`eawf.kernel.validate.invariants.check_agent_report_invariants`
    and lands in the per-role store (``auditor_report.jsonl``,
    ``reviewer_report.jsonl``, ...) rather than always
    ``executor_report.jsonl``.

    The dispatched *wave_id* is used as the report ``base_id`` so retried
    dispatches for the same wave append monotonic attempts under one
    ``(role, base_id)`` series.

    The session's :attr:`~eawf.kernel.state.models.AgentSession.agent_principal_id`
    (a v0.3-v0.5 placeholder for the per-repo Principal id of the agent
    that drove the dispatch) is copied by the canonical agent-report
    writer onto the persisted
    :attr:`AgentReportHeader.agent_principal_id` and logged here so the
    dispatch trace records the Principal binding for the served
    attempt.

    Args:
        ctx: Daemon method context — supplies ``state_path``.
        session_id: Id of the session that ran the dispatch; must
            exist in ``state.json``. The session's role decides the
            body type + destination store kind.
        wave_id: ``W<NN>`` wave the dispatch served. Must exist in
            ``state.waves`` for the role's wave-presence invariant
            (executor-wave / auditor-target / reviewer-target / etc.).
        commit_sha: Commit the session landed; required for the
            executor path so the executor-commit invariant holds.
            Non-executor roles ignore the value.
        outcome: One-line implementation outcome for the report body.
        files_changed: Repo-relative paths the dispatch changed
            (executor only).
        tests_run: Test commands the dispatch executed (executor only).
        runtime: Runtime that served the dispatch, recorded on the
            report header.
        verdict: Report verdict; derived from *switched* when ``None``.
            Ignored when *report_body* is supplied (the spawned agent's
            own verdict is authoritative on the live-spawn path).
        confidence: Report confidence (defaults to ``high``). Ignored when
            *report_body* is supplied.
        switched: ``True`` when a V5 fallback fired; feeds the derived
            verdict when *verdict* is ``None`` and *report_body* is
            ``None``.
        report_body: Optional already-validated typed body parsed from the
            spawned agent's own output. When supplied it is persisted
            verbatim (the synthetic completion body is NOT minted); when
            ``None`` the runner builds the role-appropriate completion
            body from *outcome* / *files_changed* / *tests_run*.

    Returns:
        The id of the appended role-specific report envelope.

    Raises:
        RuntimeError: When ``ctx.state_path`` is not configured (the
            writer needs state to resolve the session authority).
        KeyError: When *session_id* is absent from ``state.json``.
        eawf.workflow.agent_report.store.AgentReportRoleMismatchError: When the
            session role disagrees with the constructed body's role
            (cannot happen on this path — :func:`_build_completion_body`
            keys off the session role).
        eawf.workflow.agent_report.store.AgentReportScrubError: When the report
            body text contains local or sensitive tokens.
        DispatchCloseBlockedError: When the post-execution verify gate
            (:func:`~eawf.workflow.verify.dispatch_close.verify_close_readiness`)
            refuses the report — e.g. a ``FAIL`` / ``BLOCKED`` verdict
            or an executor body whose ``wave_id`` disagrees with the
            dispatched wave. The report has already been persisted at
            this point; the raise prevents the close path from
            advancing on an unverified attempt.
    """
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state_path = Path(ctx.state_path)
    state = load_state(state_path)
    session = state.agent_sessions.get(session_id)
    if session is None:
        raise KeyError(f"unknown agent session: {session_id!r}")
    if report_body is not None:
        # Live-spawn path: persist the agent's OWN validated body verbatim.
        # Its verdict / outcome / files_changed are authoritative — the
        # runner does not mint a synthetic completion body here.
        body: AgentReportBody = report_body
        resolved_verdict = report_body.verdict
    else:
        resolved_verdict = (
            verdict if verdict is not None else _completion_verdict(switched=switched)
        )
        body = _build_completion_body(
            role=session.role,
            wave_id=wave_id,
            commit_sha=commit_sha,
            outcome=outcome,
            files_changed=files_changed,
            tests_run=tests_run,
            verdict=resolved_verdict,
            confidence=confidence,
        )
    result = append_agent_report(
        state=state,
        state_path=state_path,
        session_id=session_id,
        base_id=wave_id,
        body=body,
        runtime=runtime,
    )
    # Surface the Principal-linked identity of the dispatched session
    # alongside the runtime id. The canonical writer has already copied
    # this value into ``AgentReportHeader.agent_principal_id``.
    agent_principal_id = session.agent_principal_id
    logger.info(
        f"emit_agent_end_report wave={wave_id} session={session_id!r} "
        f"role={session.role.value} verdict={resolved_verdict.value} "
        f"store_kind={result.store_kind} report_id={result.envelope.id!r} "
        f"agent_principal_id={agent_principal_id!r}"
    )
    # Post-execution verify gate: the report is persisted (so the failed
    # attempt is recorded on disk), then the runner refuses to advance
    # the close path when the gate fires. A clean pass returns the
    # report id; a blocked close raises with the structured reasons.
    typed_count, teeth_bit = evidence_rung_inputs(
        state, wave_id, repo_root=state_path.parent.parent
    )
    verify_result = verify_close_readiness(
        wave_id,
        body,
        typed_criteria_count=typed_count,
        require_evidence_refs=teeth_bit,
    )
    if not verify_result.passed:
        raise DispatchCloseBlockedError(wave_id=wave_id, result=verify_result)
    return result.envelope.id


def _mark_wave_in_progress(ctx: MethodContext, *, wave_id: str) -> bool:
    """Flip *wave_id* CLAIMED -> IN_PROGRESS through the daemon canonical writer.

    Called at the head of :func:`run_dispatch` so a dispatched wave's
    ``state.json`` status reflects that the claim has been picked up and
    implementation has begun. The mutation runs under the same defense-in-
    depth ``portalock(state.json)`` + locked-atomic-write the daemon's
    ``state.mutate`` path uses (per the daemon-as-sole-mutator rule), and
    applies the pure-functional :func:`eawf.workflow.lifecycle.wave.start_wave`
    transition so the inline-start and dispatched-start paths converge on
    one transition.

    The flip is opt-in and tolerant: it is skipped (returning ``False``)
    when ``ctx.state_path`` is unset (stateless unit-test contexts) or the
    wave is absent from state. A wave that is already IN_PROGRESS is a
    no-op via ``start_wave``'s own idempotency. A wave in any other status
    (PENDING / terminal) is left untouched — the claim-gate upstream owns
    that precondition, so the runner does not abort a dispatch over it.

    Args:
        ctx: Daemon method context — supplies ``state_path``.
        wave_id: ``W<NN>`` wave being dispatched.

    Returns:
        ``True`` when the wave was flipped to (or already at) IN_PROGRESS
        and the new status was persisted; ``False`` when the flip was
        skipped (no ``state_path``, unknown wave, or a non-claimable
        status).
    """
    if ctx.state_path is None:
        return False
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        wave = state.waves.get(wave_id)
        if wave is None:
            logger.debug(f"_mark_wave_in_progress skip wave={wave_id!r} not in state")
            return False
        if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
            logger.debug(
                f"_mark_wave_in_progress skip wave={wave_id!r} status={wave.status.value!r}"
            )
            return False
        start_wave(state, wave_id=wave_id)
        state.updated_at = datetime.now(UTC)
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    logger.info(f"_mark_wave_in_progress wave={wave_id}")
    return True


def run_dispatch(
    ctx: MethodContext,
    *,
    wave_id: str,
    primary_runtime: RuntimeTriple,
    fallback_runtime: RuntimeTriple,
    model: str,
    pricing_version: str,
    primary_error: RuntimeErrorClass | None,
    tokens: DispatchTokens,
    cost_usd: Decimal,
    trace_request_id: str | None = None,
    session_id: str | None = None,
    commit_sha: str | None = None,
    outcome: str | None = None,
    files_changed: list[str] | None = None,
    tests_run: list[str] | None = None,
    verdict: AgentReportVerdict | None = None,
    confidence: Confidence = Confidence.HIGH,
    report_body: AgentReportBody | None = None,
    pgid: int | None = None,
    enforce: EnforceMode = DEFAULT_ENFORCE,
    output_text: str | None = None,
    accrue_wave_budget: bool = True,
) -> DispatchResult:
    """Drive one wave dispatch attempt, emitting the C09 dispatch events.

    The runner mints a fresh attempt id for the primary runtime. When
    *primary_error* is set, it simulates a V5 fallback: a fresh attempt id
    is minted for *fallback_runtime*, a ``runtime_switched`` event is
    emitted through the canonical writer, and the fallback runtime serves
    the dispatch. Once the serving attempt completes, a ``dispatch_cost``
    event is emitted with the token tally + priced cost, and the tally is
    folded into ``Wave.tokens_consumed`` via :func:`accrue_tokens_consumed`
    (triggering a STATE_REVISION) so the live burn gauge advances during
    execution.

    On completion the runner also emits a typed ``agent_end`` executor
    report through :func:`emit_agent_end_report` when *session_id* is
    supplied (and the daemon context carries a ``state.json``); the
    report's envelope id rides back on :attr:`DispatchResult.report_id`.
    Stateless contexts (no ``session_id`` or no ``state_path``) skip the
    report and leave ``report_id`` ``None``.

    Args:
        ctx: Daemon method context.
        wave_id: ``W<NN>`` wave being dispatched.
        primary_runtime: Runtime tried first.
        fallback_runtime: Runtime the V5 ladder falls through to.
        model: Model the serving runtime priced its cost against.
        pricing_version: ``PRICING`` snapshot version pinning the cost.
        primary_error: Typed
            :class:`~eawf.observability.telemetry.models.RuntimeErrorClass` member when
            the primary runtime fails (triggers a V5 fallback), or ``None``
            when the primary serves the dispatch with no switch.
        tokens: Token tally the serving attempt accrued.
        cost_usd: Priced cost in USD for the serving attempt.
        trace_request_id: Optional daemon RPC request id.
        session_id: Id of the executor session that ran the dispatch. When
            supplied (and ``ctx.state_path`` is configured) the runner
            emits the typed ``agent_end`` executor report on completion.
        commit_sha: Commit the executor landed; required for the
            ``agent_end`` report (defaults to the serving attempt id when
            omitted so the executor-commit invariant still holds).
        outcome: One-line implementation outcome for the report body;
            defaults to a generated summary when omitted.
        files_changed: Repo-relative paths the dispatch changed (report).
        tests_run: Test commands the dispatch executed (report).
        verdict: ``agent_end`` report verdict; derived from the fallback
            outcome when ``None``. Ignored when *report_body* is supplied.
        confidence: ``agent_end`` report confidence (defaults to ``high``).
            Ignored when *report_body* is supplied.
        report_body: Optional already-validated typed report body parsed
            from the spawned agent's own output. When supplied it is
            threaded into :func:`emit_agent_end_report` and persisted
            verbatim instead of the runner minting a synthetic completion
            body from *outcome*. The live-spawn caller supplies this so the
            persisted report carries the agent's words; the hand-fed-outcome
            caller leaves it ``None`` (the synthetic body is built).
        output_text: Optional captured stdout/stderr of the spawned child
            (FA4, W08). When supplied the runner fans its bounded line tail to
            the live output tail via :func:`emit_agent_output` (an
            ``agent.output`` event the TUI routes to the agent-watch zoom);
            ``None`` (the hand-fed-outcome path) fans no output.

    Returns:
        A :class:`DispatchResult` naming the serving runtime, its attempt
        id, whether a fallback fired, the emitted C09 event ids (including the
        ``agent.output`` envelope when *output_text* was fanned), and the
        ``agent_end`` report id (``None`` when no report was emitted).

    Raises:
        DispatchCloseBlockedError: Propagated from
            :func:`emit_agent_end_report` when the post-execution
            verify gate refuses the persisted report (W57). The
            failure is fail-fast: the C09 events have already been
            persisted, the token tally has already accrued, and the
            report row is on disk; the raise stops the close path
            from advancing past an unverified attempt.
    """
    # Head transition: a dispatched wave moves CLAIMED -> IN_PROGRESS the
    # moment the runner starts driving it, so the wave's persisted status
    # reflects that implementation is underway before any event/report
    # lands. Skipped when the daemon context carries no state.json
    # (stateless unit-test contexts) or the wave is not in a claimable
    # status (the claim-gate upstream owns that precondition).
    _mark_wave_in_progress(ctx, wave_id=wave_id)

    primary_attempt = uuid.uuid4().hex
    event_ids: list[str] = []
    serving_runtime = primary_runtime
    serving_attempt = primary_attempt
    switched = False

    if primary_error is not None:
        fallback_attempt = uuid.uuid4().hex
        switched = True
        serving_runtime = fallback_runtime
        serving_attempt = fallback_attempt
        event_ids.append(
            emit_runtime_switched(
                ctx,
                wave_id=wave_id,
                attempt_id_from=primary_attempt,
                attempt_id_to=fallback_attempt,
                runtime_from=primary_runtime,
                runtime_to=fallback_runtime,
                cause=primary_error,
                error_detail=f"{primary_runtime} failed: {primary_error}",
                idempotency_key=uuid.uuid4().hex,
                trace_request_id=trace_request_id,
            )
        )

    event_ids.append(
        emit_dispatch_cost(
            ctx,
            wave_id=wave_id,
            attempt_id=serving_attempt,
            runtime=serving_runtime,
            model=model,
            tokens=tokens,
            cost_usd=cost_usd,
            pricing_version=pricing_version,
            trace_request_id=trace_request_id,
        )
    )

    # FA4 (W08): fan the spawned child's captured stdout/stderr to the live
    # output tail. The producer publishes the bounded line tail as an
    # ``agent.output`` event the TUI App routes to the agent-watch session zoom;
    # a hand-fed-outcome dispatch (no captured text) is a no-op.
    if output_text is not None:
        output_id = emit_agent_output(ctx, wave_id=wave_id, text=output_text)
        if output_id is not None:
            event_ids.append(output_id)

    # Fold the dispatch's token tally into Wave.tokens_consumed so the live
    # burn gauge advances during execution. Routes through the daemon
    # canonical state writer (portalock + atomic write) and triggers a
    # STATE_REVISION on both feeds (mtime-poll via the state.json write +
    # daemon-push via the bus). Skipped on a stateless context. The captured
    # spawn pgid + config-resolved enforce mode thread through so a hard-cap
    # breach can reap the live wave's process group.
    # A campaign-scoped researcher dispatch (W14) has no execution wave to fold
    # its tokens into, so the wave-budget accrual is skipped; the dispatch_cost
    # event above still books the spend against the campaign scope.
    interlock = (
        accrue_tokens_consumed(ctx, wave_id=wave_id, tokens=tokens, pgid=pgid, enforce=enforce)
        if accrue_wave_budget
        else None
    )
    terminated = interlock is not None and interlock.terminated

    report_id: str | None = None
    if session_id is not None and ctx.state_path is not None:
        # On the hand-fed-outcome path the runner mints a fallback outcome
        # string when the caller omits one. The live-spawn path supplies
        # *report_body* (the agent's OWN validated body) so that fallback is
        # never the persisted source — the synthetic string is bypassed.
        report_id = emit_agent_end_report(
            ctx,
            session_id=session_id,
            wave_id=wave_id,
            commit_sha=commit_sha if commit_sha is not None else serving_attempt,
            outcome=outcome
            if outcome is not None
            else f"dispatch served by {serving_runtime} (switched={switched})",
            files_changed=files_changed,
            tests_run=tests_run,
            runtime=serving_runtime,
            verdict=verdict,
            confidence=confidence,
            switched=switched,
            report_body=report_body,
        )

    logger.info(
        f"run_dispatch wave={wave_id} serving_runtime={serving_runtime} "
        f"switched={switched} events={len(event_ids)} report_id={report_id!r} "
        f"terminated={terminated}"
    )
    return DispatchResult(
        runtime=serving_runtime,
        attempt_id=serving_attempt,
        switched=switched,
        event_ids=tuple(event_ids),
        report_id=report_id,
        terminated=terminated,
    )
