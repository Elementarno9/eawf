"""Background estimate-crossing producer for active waves.

Each active wave is measured against its own estimate: an explicit
estimate row, else its effort bucket's default, else a generous absolute
window for a wave that has neither. Short of the notice policy's notify
fraction nothing happens at all -- no event, no notice, no prompt -- and
the elapsed fraction stays readable in the effort gauge.

At the notify fraction the estimate has been passed. That is recorded
once per claim as an activity event and nothing more, because passing an
estimate proves elapsed-time arithmetic, not that anything is wrong. Only
once no progress has been observed for the policy's grace period does the
crossing open a budget notice, and a notice never pauses, asks or opens
anything: it is one non-blocking row in the notice ledger, upserted so a
restart or a second sweep leaves the same one row.

Progress is any event the wave's own work wrote -- a claim, an agent's
output -- rather than the daemon's own bookkeeping about it. The elapsed
clock anchors on ``Wave.claimed_at``; a wave that was never claimed has no
work-start fact to elapse from.

On start the loop also imports the over-budget pauses the detector used
to raise, so each lands once as a notice instead of lingering as a pause.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import orjson

from eawf.kernel.economics.notice_policy import EstimatedTimeNotice
from eawf.kernel.state.enums import StoreKind, WaveStatus
from eawf.kernel.state.models import State
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.budget.legacy_notices import import_legacy_notices
from eawf.runtime.budget.notices import (
    LOCAL_OPERATOR,
    BudgetCrossing,
    UpsertOutcome,
    budget_window_digest,
    notices_path,
    upsert_notice,
)
from eawf.runtime.daemon.admission import load_economics
from eawf.workflow.estimation.thresholds import wave_budget_minutes
from eawf.workflow.evidence._io import load_state

logger = logging.getLogger(__name__)


DEFAULT_SWEEP_SECONDS: Final[int] = 60

#: The activity event recording that a wave passed its estimate.
ESTIMATE_PASSED_EVENT_TYPE: Final = "runtime.budget_notice.estimate_passed"

#: The activity event recording that the crossing opened a notice.
NOTICE_OPENED_EVENT_TYPE: Final = "runtime.budget_notice.opened"

_ACTIVE_WAVE_STATUSES: Final[frozenset[WaveStatus]] = frozenset(
    {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
)
_CLOSED_WAVE_STATUSES: Final[frozenset[WaveStatus]] = frozenset(
    {WaveStatus.CLOSED, WaveStatus.FAILED, WaveStatus.ABANDONED}
)

#: Event types the daemon writes about a wave rather than the wave's own
#: work doing anything; none of them is progress.
_NOT_PROGRESS: Final[frozenset[str]] = frozenset(
    {ESTIMATE_PASSED_EVENT_TYPE, NOTICE_OPENED_EVENT_TYPE, "wave_elapsed_update"}
)

_WAVE_ID_KEY: Final = "wave_id"
_CLAIM_KEY: Final = "claim_ref"


@dataclass(frozen=True)
class EstimateCrossing:
    """One active wave whose elapsed time passed its estimate.

    Attributes:
        wave_id: The wave.
        scope_id: The state URN the activity event is filed under.
        claim_ref: What identifies this claim of the wave.
        anchor: When the claim started.
        elapsed_seconds: Wall seconds since the claim.
        estimate_seconds: The estimate those seconds passed.
        last_progress_at: When the wave's own work last wrote an event.
        grace_met: Whether no progress was seen for the grace period.
    """

    wave_id: str
    scope_id: str
    claim_ref: str
    anchor: datetime
    elapsed_seconds: float
    estimate_seconds: float
    last_progress_at: datetime
    grace_met: bool


def _now() -> datetime:
    return datetime.now(UTC)


def _iter_event_payloads(events_path: Path) -> list[tuple[str | None, EventPayload]]:
    """Return valid event payloads from *events_path*, skipping malformed rows."""
    if not events_path.is_file():
        return []
    out: list[tuple[str | None, EventPayload]] = []
    with events_path.open("rb") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                envelope = Envelope.model_validate(orjson.loads(line))
            except (orjson.JSONDecodeError, ValueError) as exc:
                logger.debug(f"_iter_event_payloads skip envelope cause={exc!r}")
                continue
            if envelope.kind is not StoreKind.EVENT:
                continue
            try:
                payload = EventPayload.model_validate(envelope.payload)
            except ValueError as exc:
                logger.debug(f"_iter_event_payloads skip payload cause={exc!r}")
                continue
            out.append((envelope.scope_id, payload))
    return out


def _wave_of(scope_id: str | None) -> str | None:
    """Return the wave an event's scope names: the wave itself or a lane of it."""
    if scope_id is None:
        return None
    return scope_id.split("::", 1)[0]


def _last_progress(events: Iterable[tuple[str | None, EventPayload]]) -> dict[str, datetime]:
    """Return, per wave, when its own work last wrote an event."""
    out: dict[str, datetime] = {}
    for scope_id, payload in events:
        wave_id = _wave_of(scope_id)
        if wave_id is None or payload.event_type in _NOT_PROGRESS:
            continue
        stamp = payload.timestamp
        if wave_id not in out or stamp > out[wave_id]:
            out[wave_id] = stamp
    return out


def _recorded_crossings(events: Iterable[tuple[str | None, EventPayload]]) -> set[tuple[str, str]]:
    """Return the ``(wave, claim)`` pairs whose crossing is already on file."""
    out: set[tuple[str, str]] = set()
    for _scope, payload in events:
        if payload.event_type != ESTIMATE_PASSED_EVENT_TYPE:
            continue
        wave_id, claim = payload.extras.get(_WAVE_ID_KEY), payload.extras.get(_CLAIM_KEY)
        if isinstance(wave_id, str) and isinstance(claim, str):
            out.add((wave_id, claim))
    return out


def plan_estimate_crossings(
    state: State,
    *,
    events: list[tuple[str | None, EventPayload]],
    policy: EstimatedTimeNotice,
    now: datetime | None = None,
) -> list[EstimateCrossing]:
    """Return every active, claimed wave that passed its estimate.

    Args:
        state: Validated state document.
        events: The event store's payloads, read for progress.
        policy: When an estimate is passed and when its grace is met.
        now: Reference time; defaults to wall-clock UTC.

    Returns:
        One crossing per wave at or past the notify fraction, in state
        iteration order; nothing for a wave short of it.
    """
    reference = now or _now()
    progress = _last_progress(events)
    crossings: list[EstimateCrossing] = []
    for wave in state.waves.values():
        if wave.status not in _ACTIVE_WAVE_STATUSES or wave.claimed_at is None:
            continue
        anchor = wave.claimed_at
        elapsed = max((reference - anchor).total_seconds(), 0.0)
        estimate = wave_budget_minutes(state, wave.id) * 60
        if not policy.passed(elapsed_seconds=elapsed, estimate_seconds=estimate):
            continue
        last_progress = max(anchor, progress.get(wave.id, anchor))
        crossings.append(
            EstimateCrossing(
                wave_id=wave.id,
                scope_id=state.urn,
                claim_ref=wave.claim_session_id or anchor.isoformat(),
                anchor=anchor,
                elapsed_seconds=elapsed,
                estimate_seconds=estimate,
                last_progress_at=last_progress,
                grace_met=policy.grace_met(last_progress_at=last_progress, now=reference),
            )
        )
    return crossings


def _activity_envelope(
    crossing: EstimateCrossing, *, event_type: str, message: str, now: datetime
) -> Envelope:
    """Build one activity event about *crossing*; it asks and pauses nothing."""
    payload = EventPayload(
        timestamp=now,
        event_type=event_type,
        actor="daemon",
        command="stale_wave.sweep",
        args_hash=uuid.uuid5(
            uuid.NAMESPACE_URL, f"{event_type}:{crossing.wave_id}:{crossing.claim_ref}"
        ).hex[:16],
        status="ok",
        message=message,
        extras={
            _WAVE_ID_KEY: crossing.wave_id,
            _CLAIM_KEY: crossing.claim_ref,
            "elapsed_seconds": round(crossing.elapsed_seconds, 1),
            "estimate_seconds": round(crossing.estimate_seconds, 1),
        },
    ).model_dump(mode="json")
    return Envelope(
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=crossing.scope_id,
        created_at=now,
        updated_at=None,
        summary=f"{event_type} wave={crossing.wave_id}",
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def _passed_message(crossing: EstimateCrossing) -> str:
    """Say what the crossing establishes and nothing more."""
    minutes = round(crossing.estimate_seconds / 60, 1)
    return f"Wave {crossing.wave_id} exceeded its {minutes:g}-minute estimate; execution continues"


def _opened_message(crossing: EstimateCrossing, grace_seconds: int) -> str:
    """Say what the crossing and the missing progress establish together."""
    minutes = round(crossing.estimate_seconds / 60, 1)
    return (
        f"Wave {crossing.wave_id} exceeded its {minutes:g}-minute estimate and no progress was "
        f"observed for {grace_seconds // 60} minutes; execution continues"
    )


async def sweep_once(
    *,
    state_path: Path,
    policy: EstimatedTimeNotice,
    event_path: Path | None = None,
    publish: Callable[[Envelope], None] | None = None,
    now: datetime | None = None,
) -> list[EstimateCrossing]:
    """Run one sweep: record new crossings, and open a notice past the grace.

    Args:
        state_path: The ``state.json`` the waves are read from.
        policy: When an estimate is passed and when its grace is met.
        event_path: The event store; defaults to the one beside the state.
        publish: Called with every event appended, after the append.
        now: Reference time; defaults to wall-clock UTC.

    Returns:
        Every crossing the sweep saw, recorded before or now.
    """
    if not state_path.exists():
        logger.debug(f"sweep_once skip state-missing path={state_path!s}")
        return []
    reference = now or _now()
    events_path = event_path or store_path(state_path, StoreKind.EVENT)
    events = _iter_event_payloads(events_path)
    crossings = plan_estimate_crossings(
        load_state(state_path),
        events=events,
        policy=policy,
        now=reference,
    )
    recorded = _recorded_crossings(events)
    for crossing in crossings:
        appended: list[Envelope] = []
        if (crossing.wave_id, crossing.claim_ref) not in recorded:
            appended.append(
                _activity_envelope(
                    crossing,
                    event_type=ESTIMATE_PASSED_EVENT_TYPE,
                    message=_passed_message(crossing),
                    now=reference,
                )
            )
        if crossing.grace_met:
            upsert = upsert_notice(
                notices_path(state_path),
                BudgetCrossing(
                    scope_id=crossing.wave_id,
                    axis="wall_seconds",
                    basis="estimate",
                    band="limit_reached",
                    observed_value=int(crossing.elapsed_seconds),
                    budget_value=int(crossing.estimate_seconds),
                    observed_at=reference,
                    contract_digest=budget_window_digest(crossing.claim_ref),
                    audience=(LOCAL_OPERATOR,),
                ),
            )
            if upsert.outcome is UpsertOutcome.CREATED:
                appended.append(
                    _activity_envelope(
                        crossing,
                        event_type=NOTICE_OPENED_EVENT_TYPE,
                        message=_opened_message(crossing, policy.require_no_progress_for),
                        now=reference,
                    )
                )
        for envelope in appended:
            append_envelope(events_path, envelope)
            if publish is not None:
                publish(envelope)
    if crossings:
        logger.info(f"sweep_once estimate_crossings={len(crossings)}")
    return crossings


def import_legacy_pauses(state_path: Path, *, event_path: Path | None = None) -> int:
    """Import the legacy over-budget pauses beside *state_path* as notices.

    Args:
        state_path: The ``state.json`` whose waves say which subjects closed.
        event_path: The event store; defaults to the one beside the state.

    Returns:
        How many notices the import added.
    """
    if not state_path.exists():
        return 0
    state = load_state(state_path)
    closed = frozenset(
        wave.id for wave in state.waves.values() if wave.status in _CLOSED_WAVE_STATUSES
    )
    events = _iter_event_payloads(event_path or store_path(state_path, StoreKind.EVENT))
    return import_legacy_notices(
        notices_path(state_path), (payload for _scope, payload in events), closed_waves=closed
    )


async def run_sweep_loop(
    *,
    state_path: Path,
    event_path: Path | None = None,
    interval_seconds: int = DEFAULT_SWEEP_SECONDS,
    publish: Callable[[Envelope], None] | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Import the legacy pauses once, then sweep until *stop_event* is set.

    The notice policy is read from the repository's ``economics`` table on
    every sweep, so an edited policy applies without a restart and an
    invalid one is reported each sweep rather than silently defaulted.

    Raises:
        ValueError: When ``interval_seconds`` is non-positive.
    """
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be positive: {interval_seconds!r}")
    stop = stop_event or asyncio.Event()
    try:
        import_legacy_pauses(state_path, event_path=event_path)
    except Exception:
        logger.exception("run_sweep_loop legacy advisory import failed")
    while not stop.is_set():
        try:
            economics = load_economics(state_path.parent.parent)
            await sweep_once(
                state_path=state_path,
                policy=economics.notice_policy.estimated_time,
                event_path=event_path,
                publish=publish,
            )
        except Exception:
            logger.exception("run_sweep_loop estimate sweep failed; will retry next tick")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
        else:
            return


__all__ = [
    "DEFAULT_SWEEP_SECONDS",
    "ESTIMATE_PASSED_EVENT_TYPE",
    "NOTICE_OPENED_EVENT_TYPE",
    "EstimateCrossing",
    "import_legacy_pauses",
    "plan_estimate_crossings",
    "run_sweep_loop",
    "sweep_once",
]
