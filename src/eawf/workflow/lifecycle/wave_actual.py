"""Write the ``actual.jsonl`` record behind a close-created actual summary.

:func:`eawf.workflow.lifecycle.wave.close_wave` stays a pure in-memory state
flip, so the store write lives here. Every close path that persists state
outside the daemon's mutation pipeline closes through
:func:`close_wave_recording_actual`, which pairs the flip with the record the
new summary names; the daemon pipeline calls :func:`append_wave_close_actual`
itself because it orders the write against its WAL.
"""

from __future__ import annotations

import logging
from pathlib import Path

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State, Wave
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.actual import ActualPayload
from eawf.kernel.store.paths import store_path
from eawf.workflow.lifecycle.wave import close_wave

logger = logging.getLogger(__name__)


def append_wave_close_actual(state: State, *, wave_id: str, state_path: Path) -> None:
    """Append the ``actual.jsonl`` record a close-created actual summary points at.

    A close that finds no operator-authored actual creates the
    :class:`~eawf.kernel.state.models.ActualSummary` itself, naming a store
    record it never wrote. Writing that record through
    :func:`eawf.kernel.store.append.append_envelope` makes the close-time
    measurement readable from the actual store the way ``eawf actual stop``
    records are, so a consumer of the store sees every closed wave's effort.
    The close records no segments because it tracked none: the figures are the
    measured runtime totals.

    Args:
        state: The closed state carrying the close-created summary for
            *wave_id*.
        wave_id: The wave the close created the summary for.
        state_path: Path to ``state.json``; anchors
            ``<state_dir>/store/actual.jsonl``.

    Raises:
        KeyError: When *state* carries no actual summary for *wave_id*.
    """
    summary = (state.actuals or {})[wave_id]
    payload = ActualPayload(
        segments=[],
        elapsed_eu=summary.elapsed_eu,
        attention_eu=summary.attention_eu,
        agent_runtime_eu=summary.agent_runtime_eu,
        outcome=summary.status.value,
        idle_policy="wave_close_runtime",
    )
    envelope = Envelope(
        id=summary.current_store_record_id,
        kind=StoreKind.ACTUAL,
        scope_id=wave_id,
        created_at=summary.updated_at,
        updated_at=summary.updated_at,
        summary=f"actual recorded at close for {wave_id}",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, StoreKind.ACTUAL), envelope)
    logger.info(
        f"append_wave_close_actual wave={wave_id!r} record_id={envelope.id!r} "
        f"elapsed_eu={summary.elapsed_eu}"
    )


def close_wave_recording_actual(
    state: State,
    *,
    state_path: Path,
    wave_id: str,
    outcome: str,
    tokens_consumed: int | None = None,
    actual_attention_eu: float | None = None,
    actual_agent_runtime_eu: float | None = None,
    actual_elapsed_eu: float | None = None,
    actual_cost_usd: float | None = None,
) -> Wave:
    """Close *wave_id* and write the actual record a new summary points at.

    An operator-authored actual already has its own store record, so the
    record is written only when the close itself creates the summary. The
    record lands before the caller persists state: a failed state write then
    leaves a record for an unclosed wave, which a retried close re-appends
    under the same id, rather than a closed wave whose summary names nothing.

    Args:
        state: State to mutate in place.
        state_path: Path to ``state.json``; anchors the actual store.
        wave_id: Id of the claimed/in-progress wave to close.
        outcome: Human-readable outcome summary.
        tokens_consumed: Forwarded to :func:`close_wave`.
        actual_attention_eu: Forwarded to :func:`close_wave`.
        actual_agent_runtime_eu: Forwarded to :func:`close_wave`.
        actual_elapsed_eu: Forwarded to :func:`close_wave`.
        actual_cost_usd: Forwarded to :func:`close_wave`.

    Returns:
        The closed wave.

    Raises:
        LifecycleError: Propagated from :func:`close_wave` when the wave is
            unknown, not closable, or an actual value is negative; nothing is
            written to the store in that case.
    """
    creates_summary = wave_id not in (state.actuals or {})
    wave = close_wave(
        state,
        wave_id=wave_id,
        outcome=outcome,
        tokens_consumed=tokens_consumed,
        actual_attention_eu=actual_attention_eu,
        actual_agent_runtime_eu=actual_agent_runtime_eu,
        actual_elapsed_eu=actual_elapsed_eu,
        actual_cost_usd=actual_cost_usd,
    )
    if creates_summary:
        append_wave_close_actual(state, wave_id=wave_id, state_path=state_path)
    return wave
