"""The fields a Run's start and stop edges record about its counters.

The daemon is the one caller of the capture producer. When a Run starts it
counts the Runs already active on the same vendor session -- that count is
the divisor the session is shared by, and it has to be taken now, because
recovering it later from another snapshot yields a different number -- and
records the baseline. When a Run stops it records what the stop reading
makes of that baseline. Both are returned as transition updates, so the
readings land in the same commit as the edge they describe.

The reads happen before the transaction opens, never under its locks: a
transcript can be large, and the transaction re-validates the record the
updates land on anyway.

A measured stop is also stated on the Run's own stream as ``usage_observed``,
before the stop commits, so the Run's usage events fold to the same tokens and
cost its captured row banks: a line written after the terminal edge would be
quarantined and derived from by nothing.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from pydantic import TypeAdapter, ValidationError

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.control import TERMINAL_RUN_STATUSES
from eawf.kernel.runtime.events import RunEventKind
from eawf.kernel.runtime.usage import UsagePayload
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.measurement import (
    CaptureSource,
    CounterName,
    MeasuredRuntime,
    Observed,
    VendorSessionRef,
)
from eawf.kernel.state.epoch2.run import Run, RunRuntimeTuple, RunStatus
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.measurement.capture import (
    capture_run_start,
    capture_run_terminal,
    observe_session_runtime,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.run_events import RunEventAppend

logger = logging.getLogger(__name__)

_INSTANT: TypeAdapter[datetime] = TypeAdapter(UtcDatetime)

_MICROUSD_PER_USD: Final = Decimal(1_000_000)


def _sharing_runs(document: dict[str, Any], *, run: Run, ref: VendorSessionRef) -> int:
    """Return how many Runs share *ref* once *run* starts, *run* included."""
    others = 0
    for key, row in document_rows(document, Epoch2Collection.RUN).items():
        if key == run.key or row.get("status") != RunStatus.RUNNING.value:
            continue
        session = row.get("vendor_session")
        if isinstance(session, Mapping) and session.get("session_digest") == ref.session_digest:
            others += 1
    return others + 1


def start_capture_updates(
    document: dict[str, Any], *, run: Run, updates: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the vendor session, baseline and runtime tuple a Run's start records.

    The runtime tuple is the one the caller stated, its gaps filled from what
    the vendor session shows; with neither, the Run records none.

    Args:
        document: The root document the concurrent Runs are counted in.
        run: The QUEUED Run being started.
        updates: The start edge's caller-supplied updates; ``started_at``
            is required by the edge, ``vendor_session`` names the session
            when the Run does not already carry one, and ``runtime_tuple``
            states what the caller knows of the runtime.

    Returns:
        The ``vendor_session``, ``counter_baseline`` and ``runtime_tuple``
        updates.

    Raises:
        ValidationError: ``started_at``, ``vendor_session`` or
            ``runtime_tuple`` is malformed.
    """
    presented = updates.get("vendor_session")
    ref = run.vendor_session if presented is None else VendorSessionRef.model_validate(presented)
    started_at = _INSTANT.validate_python(updates["started_at"])
    count = 1 if ref is None else _sharing_runs(document, run=run, ref=ref)
    baseline = capture_run_start(ref, at=started_at, concurrent_run_count=count)
    stated = updates.get("runtime_tuple")
    claimed = run.runtime_tuple if stated is None else RunRuntimeTuple.model_validate(stated)
    observed = None if ref is None else observe_session_runtime(ref, as_of=started_at)
    runtime = observed if claimed is None else claimed.filled_from(observed)
    logger.debug(
        f"start_capture_updates runtime={None if runtime is None else runtime.harness} "
        f"version_known={runtime is not None and runtime.harness_version is not None}"
    )
    return {
        "vendor_session": None if ref is None else ref.model_dump(mode="json"),
        "counter_baseline": baseline.model_dump(mode="json"),
        "runtime_tuple": None if runtime is None else runtime.model_dump(mode="json"),
    }


def terminal_capture_updates(run: Run, *, ended_at: object) -> dict[str, Any]:
    """Return the captured runtime a Run's stop records.

    Args:
        run: The Run being stopped.
        ended_at: The stop edge's ``ended_at`` update.

    Returns:
        The ``captured_runtime`` update.

    Raises:
        ValidationError: ``ended_at`` is malformed.
    """
    at = _INSTANT.validate_python(ended_at)
    captured = capture_run_terminal(
        run.vendor_session,
        baseline=run.counter_baseline,
        started_at=run.started_at or at,
        at=at,
    )
    return {"captured_runtime": captured.model_dump(mode="json")}


def _whole(
    captured: MeasuredRuntime, *names: CounterName, scale: Decimal = Decimal(1)
) -> int | None:
    """Return the observed counters *names* summed and scaled to a whole number, if any."""
    readings = [captured.counters[name] for name in names]
    observed = [reading.value for reading in readings if isinstance(reading, Observed)]
    if not observed:
        return None
    return int((sum(observed, Decimal(0)) * scale).to_integral_value())


def captured_usage(captured: MeasuredRuntime) -> UsagePayload | None:
    """Return the usage reading a Run's measured share states, or ``None`` when it has none.

    The share is the Run's whole spend from start to stop, so it is a running total: the
    fold over the stream takes it as the Run's maximum and never adds it to a reading of
    the same turn. Both prompt-cache classes are cache tokens. A share whose baseline was
    bounded is an estimate that still covers the whole Run.

    Args:
        captured: The Run's measured share of its vendor session.

    Returns:
        The reading, or ``None`` when no counter it carries was observed.
    """
    quality = captured.measurement_quality
    input_tokens = _whole(captured, CounterName.INPUT_TOKENS)
    output_tokens = _whole(captured, CounterName.OUTPUT_TOKENS)
    cache_tokens = _whole(
        captured, CounterName.CACHE_CREATION_INPUT_TOKENS, CounterName.CACHE_READ_INPUT_TOKENS
    )
    cost_microusd = _whole(captured, CounterName.COST_USD, scale=_MICROUSD_PER_USD)
    counted = (input_tokens, output_tokens, cache_tokens, cost_microusd)
    if quality == "unavailable" or all(value is None for value in counted):
        return None
    return UsagePayload(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        cost_microusd=cost_microusd,
        # The cost counter is the charge the runtime itself reported.
        price_source="billed" if cost_microusd is not None else None,
        usage_source=(
            "counter_sidecar" if captured.source is CaptureSource.SIDECAR else "provider_transcript"
        ),
        is_cumulative=True,
        measurement_quality=quality,
        coverage_fraction=1.0 if quality == "estimated" else None,
    )


def _state_captured_usage(
    context: Epoch2RootContext,
    *,
    urn: QualifiedUrn,
    actor: PrincipalKey,
    captured: Mapping[str, Any],
) -> None:
    """State a measured stop's share on the Run's stream, ahead of the stop itself.

    The line is named by its content, so a retried stop with the same reading repeats it
    and a stop re-read with another states a second running total the fold keeps the
    larger of. A line the stream refuses is logged and dropped: the stop still commits.
    """
    # the run verbs import this module, so the append is reached at call time
    from eawf.runtime.daemon.methods.run import append_run_event

    row = (
        MeasuredRuntime.model_validate(captured) if captured.get("outcome") == "measured" else None
    )
    payload = None if row is None else captured_usage(row)
    if payload is None:
        return
    body = f"{urn.entity_key}:captured:{payload.model_dump_json()}"
    try:
        append_run_event(
            context,
            RunEventAppend(
                urn=urn,
                event_ref=f"EVT-{hashlib.sha256(body.encode('utf-8')).hexdigest()[:32]}",
                run_sequence=1,
                event_kind=RunEventKind.USAGE_OBSERVED,
                provenance="daemon_observed",
                payload=payload,
                actor=actor,
            ),
            now=datetime.now(UTC),
            at_tail=True,
        )
    except DaemonValidationError as error:
        logger.warning(f"_state_captured_usage refused run={urn.entity_key!r} cause={error}")


def bind_run_capture(
    context: Epoch2RootContext,
    *,
    urn: QualifiedUrn,
    to_status: RunStatus,
    updates: Mapping[str, Any],
    actor: PrincipalKey,
) -> dict[str, Any]:
    """Return *updates* carrying the counter readings a Run edge records.

    Only a start out of the queue and a stop from a started Run take a
    reading. A Run the document does not hold, or an edge missing the
    stamp it requires, is left to the transaction to refuse. A measured
    stop is stated on the Run's stream before it is returned.

    Args:
        context: The native context of the Run's root.
        urn: The Run being moved.
        to_status: Where the edge takes it.
        updates: The edge's caller-supplied updates.
        actor: The principal moving the Run, whom the stated reading is
            attributed to.

    Returns:
        The updates, joined by the readings the edge records.

    Raises:
        ValidationError: A presented ``vendor_session`` or a stamp is
            malformed.
    """
    normalized = dict(updates)
    # A presented session is stored hashed whatever the Run's status, so a
    # retry that finds the Run already moved presents the same request.
    if normalized.get("vendor_session") is not None:
        ref = VendorSessionRef.model_validate(normalized["vendor_session"])
        normalized["vendor_session"] = ref.model_dump(mode="json")
    with context.session([urn]) as session:
        document = session.read_document()
    row = document_rows(document, Epoch2Collection.RUN).get(urn.entity_key)
    if row is None:
        return normalized
    try:
        run = Run.model_validate(row)
    except ValidationError:
        return normalized
    if to_status is RunStatus.RUNNING and run.status is RunStatus.QUEUED:
        if "started_at" not in normalized:
            return normalized
        return {**normalized, **start_capture_updates(document, run=run, updates=normalized)}
    if to_status in TERMINAL_RUN_STATUSES and "ended_at" in normalized:
        stop = terminal_capture_updates(run, ended_at=normalized["ended_at"])
        _state_captured_usage(context, urn=urn, actor=actor, captured=stop["captured_runtime"])
        return {**normalized, **stop}
    return normalized


__all__ = [
    "bind_run_capture",
    "captured_usage",
    "start_capture_updates",
    "terminal_capture_updates",
]
