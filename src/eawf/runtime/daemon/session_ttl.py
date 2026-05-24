"""Background TTL sweep for session-handle bookkeeping.

The TTL sweep scans ``state.waves[*].sessions[*]`` once an hour for
attempts whose ``ended_at + ttl < now`` and emits a
``session_handle_pruned`` event per evicted attempt, plus a mutation
plan the mutator turns into a real ``state.mutate`` call. The
:func:`prune_handles_for_wave` shim delegates to
:mod:`eawf.runtime.daemon.session` so the in-process registry stays bounded.

The TTL defaults to one day (``86_400`` seconds) and is configurable
via ``daemon.session_handle_ttl_seconds`` once the config plumbing is
wired; until then the default applies.

Per AGENTS rule 16 (secrets / PII hygiene): the eviction *event*
carries only the opaque handle + wave id + attempt — never the raw
path. The path itself is dropped from the in-process registry on
prune; readers that need it after eviction must re-spawn (the runtime
adapter resolves a new path on the next dispatch).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.session import prune_handles_for_wave as _drop_wave_handles
from eawf.workflow.evidence._io import load_state

logger = logging.getLogger(__name__)


#: Default session-handle TTL (one day) — keeps inactive sessions on
#: short-running operator boxes from accumulating indefinitely.
DEFAULT_TTL_SECONDS: Final[int] = 86_400

#: Default sweep interval — hourly is granular enough for a 1-day TTL.
DEFAULT_SWEEP_SECONDS: Final[int] = 3_600


@dataclass(frozen=True)
class PrunePlan:
    """One attempt the TTL sweep marked for eviction.

    W09 turns each :class:`PrunePlan` row into a typed mutation
    (``DropSessionAttempt``) against ``state.json``. Until W09 lands
    the sweep just emits the plan + the event envelope so consumers
    can observe the eviction shape.

    Attributes:
        wave_id: Owning wave id.
        attempt: Attempt number to drop.
        session_log_handle: Opaque handle being evicted.
        ended_at: When the attempt terminated; used as the TTL anchor.
    """

    wave_id: str
    attempt: int
    session_log_handle: str
    ended_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _expired(ended_at: datetime, *, ttl_seconds: int, now: datetime) -> bool:
    """Return True when *ended_at + ttl* has elapsed before *now*."""
    return ended_at + timedelta(seconds=ttl_seconds) < now


def plan_evictions(
    state: State,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> list[PrunePlan]:
    """Scan *state* and return the attempts whose TTL has elapsed.

    Pure function — does not mutate the state and does not touch the
    in-process handle registry. Callers feed the plan list into
    :func:`prune_handles_for_wave` to evict the in-memory entries and
    into W09's ``state.mutate`` to drop the rows from ``state.json``.

    Args:
        state: Validated state document.
        ttl_seconds: TTL threshold; defaults to
            :data:`DEFAULT_TTL_SECONDS`.
        now: Reference time. Defaults to ``datetime.now(UTC)`` —
            tests override it to drive the boundary.

    Returns:
        List of :class:`PrunePlan` rows, one per attempt to evict.
    """
    reference = now or _now()
    plans: list[PrunePlan] = []
    for wave in state.waves.values():
        for attempt, row in wave.sessions.items():
            if row.ended_at is None:
                continue
            if not _expired(row.ended_at, ttl_seconds=ttl_seconds, now=reference):
                continue
            plans.append(
                PrunePlan(
                    wave_id=wave.id,
                    attempt=attempt,
                    session_log_handle=row.session_log_handle,
                    ended_at=row.ended_at,
                )
            )
    return plans


def build_pruned_envelope(plan: PrunePlan, *, now: datetime | None = None) -> Envelope:
    """Build the ``session_handle_pruned`` event envelope for *plan*.

    Per AGENTS rule 16: the payload carries only the opaque handle +
    wave id + attempt — never the raw filesystem path the daemon
    resolved internally.

    Args:
        plan: Eviction plan emitted by :func:`plan_evictions`.
        now: Envelope timestamp; defaults to ``datetime.now(UTC)``.

    Returns:
        :class:`Envelope` ready to publish on the event bus + append
        to ``event.jsonl`` (W09 wires the persistent append).
    """
    timestamp = now or _now()
    millis = int(timestamp.timestamp() * 1000)
    envelope_id = f"SESSION-HANDLE-PRUNED-{plan.wave_id}-{plan.attempt}-{millis}"
    return Envelope(
        id=envelope_id,
        kind=StoreKind.EVENT,
        scope_id=plan.wave_id,
        created_at=timestamp,
        summary=(f"session_handle_pruned wave={plan.wave_id} attempt={plan.attempt}"),
        payload={
            "timestamp": timestamp.isoformat(),
            "event_type": "session_handle_pruned",
            "actor": "daemon",
            "command": "session_ttl.sweep",
            "args_hash": "0",
            "status": "ok",
            "message": (
                f"session_handle_pruned wave={plan.wave_id} attempt={plan.attempt} "
                f"handle={plan.session_log_handle}"
            ),
        },
    )


def prune_handles_for_wave(wave_id: str) -> int:
    """Shim that delegates to :func:`eawf.runtime.daemon.session.prune_handles_for_wave`.

    Re-exported here so the TTL sweep + daemon-internal callers
    converge on the same wave-scoped eviction surface. Idempotent.

    Args:
        wave_id: Wave whose handles should be evicted from the
            in-process registry.

    Returns:
        Number of handles dropped from the registry.
    """
    return _drop_wave_handles(wave_id)


async def sweep_once(
    *,
    state_path: Path,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    publish: Callable[[Envelope], None] | None = None,
    now: datetime | None = None,
) -> list[PrunePlan]:
    """Run one TTL sweep against *state_path*.

    Loads ``state.json``, plans evictions, prunes the in-process
    handle registry per wave, and (when *publish* is supplied)
    emits a ``session_handle_pruned`` event per eviction. Returns
    the plan list so W09's mutator can turn it into typed
    ``state.mutate`` calls.

    Args:
        state_path: Filesystem path to ``state.json``.
        ttl_seconds: TTL threshold; defaults to
            :data:`DEFAULT_TTL_SECONDS`.
        publish: Optional ``EventBus.publish``-shaped callable. When
            omitted the sweep computes plans but stays silent on the
            bus — used in unit tests + the bootstrap path before W06's
            bus is attached.
        now: Reference time override for deterministic tests.

    Returns:
        The plan list emitted by :func:`plan_evictions`.
    """
    if not state_path.exists():
        logger.debug(f"sweep_once skip state-missing path={state_path!s}")
        return []
    state = load_state(state_path)
    plans = plan_evictions(state, ttl_seconds=ttl_seconds, now=now)
    if not plans:
        return plans
    seen_waves: set[str] = set()
    for plan in plans:
        if plan.wave_id not in seen_waves:
            prune_handles_for_wave(plan.wave_id)
            seen_waves.add(plan.wave_id)
        if publish is not None:
            publish(build_pruned_envelope(plan, now=now))
    logger.info(f"sweep_once pruned={len(plans)} waves={len(seen_waves)} ttl={ttl_seconds}")
    return plans


async def run_sweep_loop(
    *,
    state_path: Path,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    interval_seconds: int = DEFAULT_SWEEP_SECONDS,
    publish: Callable[[Envelope], None] | None = None,
    stop_event: asyncio.Event | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run the TTL sweep on a loop until *stop_event* is set.

    Args:
        state_path: Path to ``state.json``.
        ttl_seconds: TTL threshold.
        interval_seconds: Sweep interval. Default
            :data:`DEFAULT_SWEEP_SECONDS` (hourly).
        publish: Optional ``EventBus.publish`` callable.
        stop_event: When set, the loop exits at the next tick. ``None``
            means the loop runs forever — used by the daemon main
            entrypoint, never by tests.
        sleep: Sleep coroutine factory; tests inject a fast stub.
    """
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            await sweep_once(
                state_path=state_path,
                ttl_seconds=ttl_seconds,
                publish=publish,
            )
        except Exception:
            logger.exception("run_sweep_loop sweep failed; will retry next tick")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
        else:
            return
