"""Background stale-wave detector for daemon-owned operator prompts.

The detector is intentionally advisory: it appends a needs_user pause
event when an active wave crosses the stale window, publishes that
event to live subscribers, and never mutates ``state.json``. Any real
wave lifecycle action remains an operator choice routed through the
normal daemon mutator surfaces.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import orjson

from eawf.kernel.state.enums import StoreKind, WaveStatus
from eawf.kernel.state.models import State
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.paths import store_path
from eawf.workflow.evidence._io import load_state
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import PAUSE_EVENT_TYPE, build_pause_urn

logger = logging.getLogger(__name__)


DEFAULT_STALE_WINDOW_SECONDS: Final[int] = 15 * 60
DEFAULT_SWEEP_SECONDS: Final[int] = 60

_ACTIVE_WAVE_STATUSES: Final[frozenset[WaveStatus]] = frozenset(
    {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
)
_PAUSE_URN_KEY: Final[str] = "pause_urn"
_QUESTION_KEY: Final[str] = "user_question"
_SESSION_KEY: Final[str] = "session"
_STALE_WAVE_ID_KEY: Final[str] = "wave_id"


@dataclass(frozen=True)
class StaleWave:
    """One active wave that crossed the stale window."""

    wave_id: str
    scope_id: str
    session: str
    anchor: datetime
    elapsed_seconds: float

    @property
    def elapsed_minutes(self) -> float:
        """Return elapsed wall-clock minutes since :attr:`anchor`."""
        return self.elapsed_seconds / 60.0


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


def _claim_anchors(events_path: Path) -> dict[str, datetime]:
    """Return latest daemon wave-claim timestamp per wave from the event store."""
    anchors: dict[str, datetime] = {}
    for scope_id, payload in _iter_event_payloads(events_path):
        if payload.event_kind != "wave_claimed":
            continue
        wave_id = scope_id if isinstance(scope_id, str) else None
        if not wave_id:
            extra_wave_id = payload.extras.get(_STALE_WAVE_ID_KEY)
            wave_id = extra_wave_id if isinstance(extra_wave_id, str) else None
        if wave_id is None:
            continue
        anchors[wave_id] = payload.timestamp
    return anchors


def _already_notified_wave_ids(events_path: Path) -> set[str]:
    """Return wave ids that already have a stale-wave advisory event."""
    out: set[str] = set()
    for scope_id, payload in _iter_event_payloads(events_path):
        if payload.event_kind != "stale_wave_detected":
            continue
        wave_id = payload.extras.get(_STALE_WAVE_ID_KEY)
        if isinstance(wave_id, str):
            out.add(wave_id)
        elif isinstance(scope_id, str):
            out.add(scope_id)
    return out


def plan_stale_waves(
    state: State,
    *,
    events_path: Path,
    stale_window_seconds: int = DEFAULT_STALE_WINDOW_SECONDS,
    now: datetime | None = None,
    notified_wave_ids: set[str] | None = None,
) -> list[StaleWave]:
    """Return active waves that crossed the stale window and need a prompt.

    Args:
        state: Validated state document.
        events_path: Event store path used to read claim anchors and
            previous stale notifications.
        stale_window_seconds: Threshold in seconds. Defaults to
            :data:`DEFAULT_STALE_WINDOW_SECONDS`.
        now: Reference time; defaults to wall-clock UTC.
        notified_wave_ids: Optional daemon-local cache of already
            prompted wave ids.

    Returns:
        Stale-wave rows in state iteration order.

    Raises:
        ValueError: When ``stale_window_seconds`` is non-positive.
    """
    if stale_window_seconds <= 0:
        raise ValueError(f"stale_window_seconds must be positive: {stale_window_seconds!r}")
    reference = now or _now()
    claim_anchors = _claim_anchors(events_path)
    notified = set(notified_wave_ids or set())
    notified.update(_already_notified_wave_ids(events_path))
    stale: list[StaleWave] = []
    for wave in state.waves.values():
        if wave.status not in _ACTIVE_WAVE_STATUSES or wave.opened_at is None:
            continue
        if wave.id in notified:
            continue
        anchor = claim_anchors.get(wave.id, wave.opened_at)
        elapsed_seconds = (reference - anchor).total_seconds()
        if elapsed_seconds < stale_window_seconds:
            continue
        stale.append(
            StaleWave(
                wave_id=wave.id,
                scope_id=state.urn,
                session=wave.claim_session_id or "",
                anchor=anchor,
                elapsed_seconds=elapsed_seconds,
            )
        )
    return stale


def _stale_wave_question(plan: StaleWave, *, stale_window_seconds: int) -> UserQuestion:
    elapsed_minutes = round(plan.elapsed_minutes, 1)
    window_minutes = int(stale_window_seconds // 60)
    return UserQuestion(
        question=(
            f"Wave {plan.wave_id} has been active for {elapsed_minutes:g} minutes "
            f"(stale after {window_minutes} minutes)."
        ),
        options=[
            UserQuestionOption(
                label="keep",
                description="Keep it active; I will handle follow-up manually.",
            ),
            UserQuestionOption(
                label="release",
                description="Record intent to release; lifecycle mutation stays operator-run.",
            ),
            UserQuestionOption(
                label="defer",
                description="Leave the question for later without changing wave state.",
            ),
        ],
    )


def build_stale_wave_envelope(
    plan: StaleWave,
    *,
    stale_window_seconds: int = DEFAULT_STALE_WINDOW_SECONDS,
    now: datetime | None = None,
) -> Envelope:
    """Build the needs_user pause envelope for one stale wave.

    The envelope scope is the state URN so the existing TUI
    needs_user overlay discovers it with its active-scope filter. The
    wave id stays in scalar extras for consumers that need to route the
    operator's choice.
    """
    timestamp = now or _now()
    pause_urn = build_pause_urn(plan.scope_id)
    question = _stale_wave_question(plan, stale_window_seconds=stale_window_seconds)
    elapsed_minutes = round(plan.elapsed_minutes, 4)
    stale_window_minutes = round(stale_window_seconds / 60.0, 4)
    args_key = f"{plan.wave_id}:{int(plan.anchor.timestamp())}"
    payload = EventPayload(
        timestamp=timestamp,
        event_type=PAUSE_EVENT_TYPE,
        event_kind="stale_wave_detected",
        actor="daemon",
        command="stale_wave.sweep",
        args_hash=uuid.uuid5(uuid.NAMESPACE_URL, args_key).hex[:16],
        status="needs_user",
        message=question.question,
        extras={
            _PAUSE_URN_KEY: pause_urn,
            _SESSION_KEY: plan.session,
            _QUESTION_KEY: question.model_dump_json(),
            _STALE_WAVE_ID_KEY: plan.wave_id,
            "elapsed_minutes": elapsed_minutes,
            "stale_window_minutes": stale_window_minutes,
        },
    ).model_dump(mode="json")
    return Envelope(
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=plan.scope_id,
        created_at=timestamp,
        updated_at=None,
        summary=f"stale_wave_detected wave={plan.wave_id} elapsed_minutes={elapsed_minutes}",
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


async def sweep_once(
    *,
    state_path: Path,
    event_path: Path | None = None,
    stale_window_seconds: int = DEFAULT_STALE_WINDOW_SECONDS,
    publish: Callable[[Envelope], None] | None = None,
    now: datetime | None = None,
    notified_wave_ids: set[str] | None = None,
) -> list[StaleWave]:
    """Run one stale-wave sweep and append prompts for new detections."""
    if not state_path.exists():
        logger.debug(f"sweep_once skip state-missing path={state_path!s}")
        return []
    events_path = event_path or store_path(state_path, StoreKind.EVENT)
    state = load_state(state_path)
    plans = plan_stale_waves(
        state,
        events_path=events_path,
        stale_window_seconds=stale_window_seconds,
        now=now,
        notified_wave_ids=notified_wave_ids,
    )
    emitted: list[StaleWave] = []
    for plan in plans:
        envelope = build_stale_wave_envelope(
            plan,
            stale_window_seconds=stale_window_seconds,
            now=now,
        )
        append_envelope(events_path, envelope)
        if notified_wave_ids is not None:
            notified_wave_ids.add(plan.wave_id)
        if publish is not None:
            publish(envelope)
        emitted.append(plan)
    if emitted:
        logger.info(f"sweep_once stale_waves={len(emitted)}")
    return emitted


async def run_sweep_loop(
    *,
    state_path: Path,
    event_path: Path | None = None,
    stale_window_seconds: int = DEFAULT_STALE_WINDOW_SECONDS,
    interval_seconds: int = DEFAULT_SWEEP_SECONDS,
    publish: Callable[[Envelope], None] | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run stale-wave sweeps until *stop_event* is set."""
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be positive: {interval_seconds!r}")
    stop = stop_event or asyncio.Event()
    notified_wave_ids: set[str] = set()
    while not stop.is_set():
        try:
            await sweep_once(
                state_path=state_path,
                event_path=event_path,
                stale_window_seconds=stale_window_seconds,
                publish=publish,
                notified_wave_ids=notified_wave_ids,
            )
        except Exception:
            logger.exception("run_sweep_loop stale-wave sweep failed; will retry next tick")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
        else:
            return


__all__ = [
    "DEFAULT_STALE_WINDOW_SECONDS",
    "DEFAULT_SWEEP_SECONDS",
    "StaleWave",
    "build_stale_wave_envelope",
    "plan_stale_waves",
    "run_sweep_loop",
    "sweep_once",
]
