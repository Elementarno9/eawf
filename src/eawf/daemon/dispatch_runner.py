"""Daemon-side dispatch runner — emits C09 dispatch event payloads.

The runner is the daemon-internal component that drives a single wave
dispatch attempt and emits the C09 typed ``EventPayload`` sub-classes
(``runtime_switched`` on a V5 fallback, ``dispatch_cost`` post-dispatch)
defined in :mod:`eawf.store.kinds.events`.

Every event the runner produces is routed through the **daemon canonical
writer** for ``event.jsonl`` — :func:`eawf.store.append.append_envelope`
under the per-file portalock + fsync — and then published to the
subscription bus via :meth:`eawf.daemon.bus.EventBus.publish`. This
mirrors the :func:`eawf.daemon.methods.state.mutate` persistence path so
subscribers cannot tell a dispatch-runner event apart from a mutator
event: both converge on the same on-disk row. The runner never opens
``event.jsonl`` directly nor calls ``atomic_write_json`` — persistence
authority for the event store stays with the canonical append helper
(per the daemon-as-sole-mutator rule).

The typed payload is validated through :data:`C09EventPayloadUnion`
*before* it is folded into the generic
:class:`eawf.store.envelope.Envelope` ``payload`` dict, so a payload
whose body does not match its ``event_type`` discriminator fails fast
with :class:`pydantic.ValidationError` at emit time rather than at
projection time (the §5.11 discriminator-emit invariant).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from eawf.state.enums import StoreKind
from eawf.store.append import append_envelope
from eawf.store.envelope import Envelope
from eawf.store.kinds.events import (
    C09EventPayloadUnion,
    DispatchCostPayload,
    RuntimeSwitchedPayload,
)
from eawf.store.kinds.events.base import RuntimeTriple, TracedEventPayload
from eawf.telemetry.models import RuntimeErrorClass

if TYPE_CHECKING:
    from eawf.daemon.methods import MethodContext

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


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of :func:`run_dispatch`.

    Attributes:
        runtime: Runtime that ultimately served the dispatch.
        attempt_id: Dispatch-attempt id of the serving attempt.
        switched: ``True`` when a V5 fallback fired and the dispatch
            switched runtimes mid-flight.
        event_ids: Ids of every envelope the runner emitted, in append
            order (``runtime_switched`` first when a fallback fired, then
            ``dispatch_cost``).
    """

    runtime: RuntimeTriple
    attempt_id: str
    switched: bool
    event_ids: tuple[str, ...]


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
    :func:`eawf.store.append.append_envelope` (the canonical event-store
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
        cause: Typed :class:`~eawf.telemetry.models.RuntimeErrorClass`
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
) -> DispatchResult:
    """Drive one wave dispatch attempt, emitting the C09 dispatch events.

    The runner mints a fresh attempt id for the primary runtime. When
    *primary_error* is set, it simulates a V5 fallback: a fresh attempt id
    is minted for *fallback_runtime*, a ``runtime_switched`` event is
    emitted through the canonical writer, and the fallback runtime serves
    the dispatch. Once the serving attempt completes, a ``dispatch_cost``
    event is emitted with the token tally + priced cost.

    Args:
        ctx: Daemon method context.
        wave_id: ``W<NN>`` wave being dispatched.
        primary_runtime: Runtime tried first.
        fallback_runtime: Runtime the V5 ladder falls through to.
        model: Model the serving runtime priced its cost against.
        pricing_version: ``PRICING`` snapshot version pinning the cost.
        primary_error: Typed
            :class:`~eawf.telemetry.models.RuntimeErrorClass` member when
            the primary runtime fails (triggers a V5 fallback), or ``None``
            when the primary serves the dispatch with no switch.
        tokens: Token tally the serving attempt accrued.
        cost_usd: Priced cost in USD for the serving attempt.
        trace_request_id: Optional daemon RPC request id.

    Returns:
        A :class:`DispatchResult` naming the serving runtime, its attempt
        id, whether a fallback fired, and the emitted envelope ids.
    """
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

    logger.info(
        f"run_dispatch wave={wave_id} serving_runtime={serving_runtime} "
        f"switched={switched} events={len(event_ids)}"
    )
    return DispatchResult(
        runtime=serving_runtime,
        attempt_id=serving_attempt,
        switched=switched,
        event_ids=tuple(event_ids),
    )
