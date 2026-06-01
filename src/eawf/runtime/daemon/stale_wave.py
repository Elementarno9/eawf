"""Background over-budget wave detector for daemon-owned operator prompts.

The detector is intentionally advisory: it appends a needs_user pause
event when an active wave crosses a size-relative over-budget band,
publishes that event to live subscribers, and never mutates
``state.json``. Any real wave lifecycle action remains an operator choice
routed through the normal daemon mutator surfaces.

The advisory is SIZE-RELATIVE, not a flat wall-clock window: each active
wave is banded against its own pessimistic time budget (the effort-bucket
EU default, or an explicit estimate row), so an XS wave flags at a far
smaller elapsed than an XL wave. The two band boundaries are the SAME
constants the TUI effort gauge reads (``OK_BAND_CEILING`` / 0.8x and
``OVER_BUDGET_CEILING`` / 1.0x), so the gauge band and this modal cannot
drift apart. Each band fires ONCE per wave (escalating one-shot: the 0.8x
warn fires once, then the 1.0x error fires once), and a single generous
absolute backstop catches a genuinely abandoned wave that has no
projectable budget to band against.

The elapsed clock anchors on ``Wave.claimed_at`` (work-start). A wave
that has not been claimed (``claimed_at is None``) has no work-start fact
to elapse from, so it never goes stale.

True idle detection via a no-progress heartbeat is deferred to the I04
AgentSession work and is intentionally NOT built here.
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
from eawf.workflow.estimation.thresholds import (
    OK_BAND_CEILING,
    OVER_BUDGET_CEILING,
    OverBudgetBand,
    classify_band,
    wave_budget_minutes,
)
from eawf.workflow.evidence._io import load_state
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import PAUSE_EVENT_TYPE, build_pause_urn

logger = logging.getLogger(__name__)


#: Generous absolute backstop, in seconds, for a genuinely abandoned wave.
#: A wave that has no projectable pessimistic budget (no estimate row and
#: no effort bucket) cannot be banded size-relatively, so it flags once it
#: has been claimed for this long. The window is deliberately wide -- it is
#: a safety net for orphaned waves, not the primary signal -- so a normal
#: budgeted wave reaches its 0.8x / 1.0x bands long before this.
DEFAULT_ABSOLUTE_BACKSTOP_SECONDS: Final[int] = 8 * 60 * 60
DEFAULT_SWEEP_SECONDS: Final[int] = 60

_ACTIVE_WAVE_STATUSES: Final[frozenset[WaveStatus]] = frozenset(
    {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
)

#: Over-budget bands that raise an advisory, escalating order. ``ok`` is
#: intentionally absent -- an in-budget wave raises nothing. The
#: ``backstop`` band is the abandoned-wave safety net for a wave with no
#: projectable budget; it shares the ``err`` severity but is tracked as a
#: distinct one-shot so it fires independently of the size-relative bands.
_ADVISORY_BANDS: Final[tuple[str, ...]] = ("warn", "err", "backstop")

_PAUSE_URN_KEY: Final[str] = "pause_urn"
_QUESTION_KEY: Final[str] = "user_question"
_SESSION_KEY: Final[str] = "session"
_STALE_WAVE_ID_KEY: Final[str] = "wave_id"
_ADVISORY_BAND_KEY: Final[str] = "advisory_band"


@dataclass(frozen=True)
class StaleWave:
    """One active wave that crossed a size-relative over-budget band.

    ``advisory_band`` is the band this detection raises (``"warn"`` at the
    0.8x soft-over boundary, ``"err"`` at the 1.0x hard-over boundary, or
    ``"backstop"`` for an abandoned wave with no projectable budget).
    ``budget_minutes`` is the wave's pessimistic time budget, or ``None``
    when the detection is a pure absolute-backstop flag.
    """

    wave_id: str
    scope_id: str
    session: str
    anchor: datetime
    elapsed_seconds: float
    advisory_band: str
    budget_minutes: float | None

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


def _notified_bands(events_path: Path) -> dict[str, set[str]]:
    """Return the over-budget bands already advised per wave id.

    Scans prior ``stale_wave_detected`` events and groups the recorded
    :data:`_ADVISORY_BAND_KEY` per wave, so each band fires once: a wave
    that already raised ``warn`` skips a second ``warn`` but still raises
    ``err`` when it later crosses the 1.0x boundary. Rows written before
    the banded model (no ``advisory_band`` extra) are treated as having
    advised every band -- the legacy flat alarm was a terminal one-shot,
    so honouring it as fully-advised preserves the no-duplicate contract.

    Args:
        events_path: Event store path to scan.

    Returns:
        Mapping of wave id to the set of bands already advised.
    """
    out: dict[str, set[str]] = {}
    for scope_id, payload in _iter_event_payloads(events_path):
        if payload.event_kind != "stale_wave_detected":
            continue
        wave_id = payload.extras.get(_STALE_WAVE_ID_KEY)
        if not isinstance(wave_id, str):
            wave_id = scope_id if isinstance(scope_id, str) else None
        if wave_id is None:
            continue
        band = payload.extras.get(_ADVISORY_BAND_KEY)
        bands = out.setdefault(wave_id, set())
        if isinstance(band, str):
            bands.add(band)
        else:
            bands.update(_ADVISORY_BANDS)
    return out


def _elapsed_seconds(anchor: datetime, reference: datetime) -> float:
    """Return non-negative elapsed seconds from *anchor* to *reference*."""
    return max((reference - anchor).total_seconds(), 0.0)


def _pending_band(
    *,
    elapsed_seconds: float,
    budget_minutes: float | None,
    absolute_backstop_seconds: int,
    already: set[str],
) -> str | None:
    """Return the highest not-yet-advised band for one wave, or ``None``.

    Bands the wave already advised are skipped (escalating one-shot). When
    a budget is projectable, the size-relative ``warn`` / ``err`` bands are
    considered first; the absolute ``backstop`` is the safety net that
    flags an abandoned wave with no projectable budget (or a budgeted wave
    parked far past any band). The highest pending band wins so a wave that
    has been idle past 1.0x raises ``err`` directly rather than re-raising
    ``warn`` first.

    Args:
        elapsed_seconds: Non-negative elapsed work-clock seconds.
        budget_minutes: Pessimistic budget in minutes, or ``None`` when no
            budget can be projected.
        absolute_backstop_seconds: Abandoned-wave absolute window.
        already: Bands this wave has already advised.

    Returns:
        ``"err"`` / ``"warn"`` / ``"backstop"`` for the band to raise, or
        ``None`` when no new band has been crossed.
    """
    over_backstop = elapsed_seconds >= absolute_backstop_seconds
    if budget_minutes is not None and budget_minutes > 0:
        fraction = (elapsed_seconds / 60.0) / budget_minutes
        band = classify_band(fraction)
        if band == "err" and "err" not in already:
            return "err"
        if band == "warn" and "warn" not in already:
            return "warn"
        if over_backstop and "backstop" not in already:
            return "backstop"
        return None
    if over_backstop and "backstop" not in already:
        return "backstop"
    return None


def plan_stale_waves(
    state: State,
    *,
    events_path: Path,
    absolute_backstop_seconds: int = DEFAULT_ABSOLUTE_BACKSTOP_SECONDS,
    now: datetime | None = None,
    notified_bands: dict[str, set[str]] | None = None,
) -> list[StaleWave]:
    """Return active waves that crossed a new over-budget band.

    Each active, claimed wave is banded against its own pessimistic time
    budget: an advisory is raised the first time it crosses the 0.8x
    (``warn``) and 1.0x (``err``) boundaries, plus a generous absolute
    backstop for an abandoned wave with no projectable budget. A wave with
    ``claimed_at is None`` has no work-start clock and is never stale.

    Args:
        state: Validated state document.
        events_path: Event store path used to read the bands already
            advised per wave.
        absolute_backstop_seconds: Abandoned-wave absolute window. Defaults
            to :data:`DEFAULT_ABSOLUTE_BACKSTOP_SECONDS`.
        now: Reference time; defaults to wall-clock UTC.
        notified_bands: Optional daemon-local cache of already advised
            bands per wave, merged with the on-disk scan.

    Returns:
        Stale-wave rows in state iteration order, one per newly crossed
        band.

    Raises:
        ValueError: When ``absolute_backstop_seconds`` is non-positive.
    """
    if absolute_backstop_seconds <= 0:
        raise ValueError(
            f"absolute_backstop_seconds must be positive: {absolute_backstop_seconds!r}"
        )
    reference = now or _now()
    advised: dict[str, set[str]] = {k: set(v) for k, v in (notified_bands or {}).items()}
    for wave_id, bands in _notified_bands(events_path).items():
        advised.setdefault(wave_id, set()).update(bands)
    stale: list[StaleWave] = []
    for wave in state.waves.values():
        if wave.status not in _ACTIVE_WAVE_STATUSES or wave.claimed_at is None:
            continue
        anchor = wave.claimed_at
        elapsed_seconds = _elapsed_seconds(anchor, reference)
        budget_minutes = wave_budget_minutes(state, wave.id)
        band = _pending_band(
            elapsed_seconds=elapsed_seconds,
            budget_minutes=budget_minutes,
            absolute_backstop_seconds=absolute_backstop_seconds,
            already=advised.get(wave.id, set()),
        )
        if band is None:
            continue
        stale.append(
            StaleWave(
                wave_id=wave.id,
                scope_id=state.urn,
                session=wave.claim_session_id or "",
                anchor=anchor,
                elapsed_seconds=elapsed_seconds,
                advisory_band=band,
                budget_minutes=budget_minutes,
            )
        )
    return stale


def _band_clause(plan: StaleWave) -> str:
    """Return the human-readable over-budget clause for *plan*'s band."""
    if plan.advisory_band == "backstop":
        return "parked well past any time budget"
    if plan.budget_minutes is None:
        return "over its time budget"
    if plan.advisory_band == "err":
        pct = int(OVER_BUDGET_CEILING * 100)
        return f"past {pct}% of its ~{plan.budget_minutes:g}-minute budget"
    pct = int(OK_BAND_CEILING * 100)
    return f"past {pct}% of its ~{plan.budget_minutes:g}-minute budget"


def _stale_wave_question(plan: StaleWave) -> UserQuestion:
    elapsed_minutes = round(plan.elapsed_minutes, 1)
    return UserQuestion(
        question=(
            f"over-budget advisory: Wave {plan.wave_id} has been active for {elapsed_minutes:g} "
            f"minutes ({_band_clause(plan)})."
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
    now: datetime | None = None,
) -> Envelope:
    """Build the needs_user pause envelope for one over-budget wave.

    The envelope scope is the state URN so the existing TUI needs_user
    overlay discovers it with its active-scope filter. The wave id and the
    raised band stay in scalar extras for consumers that need to route the
    operator's choice or dedup the band.
    """
    timestamp = now or _now()
    pause_urn = build_pause_urn(plan.scope_id)
    question = _stale_wave_question(plan)
    elapsed_minutes = round(plan.elapsed_minutes, 4)
    args_key = f"{plan.wave_id}:{plan.advisory_band}:{int(plan.anchor.timestamp())}"
    extras: dict[str, str | int | float | bool] = {
        _PAUSE_URN_KEY: pause_urn,
        _SESSION_KEY: plan.session,
        _QUESTION_KEY: question.model_dump_json(),
        _STALE_WAVE_ID_KEY: plan.wave_id,
        _ADVISORY_BAND_KEY: plan.advisory_band,
        "elapsed_minutes": elapsed_minutes,
    }
    if plan.budget_minutes is not None:
        extras["budget_minutes"] = round(plan.budget_minutes, 4)
        extras["elapsed_ratio"] = round(plan.elapsed_minutes / plan.budget_minutes, 4)
    payload = EventPayload(
        timestamp=timestamp,
        event_type=PAUSE_EVENT_TYPE,
        event_kind="stale_wave_detected",
        actor="daemon",
        command="stale_wave.sweep",
        args_hash=uuid.uuid5(uuid.NAMESPACE_URL, args_key).hex[:16],
        status="needs_user",
        message=question.question,
        extras=extras,
    ).model_dump(mode="json")
    return Envelope(
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=plan.scope_id,
        created_at=timestamp,
        updated_at=None,
        summary=(
            f"stale_wave_detected wave={plan.wave_id} band={plan.advisory_band} "
            f"elapsed_minutes={elapsed_minutes}"
        ),
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


async def sweep_once(
    *,
    state_path: Path,
    event_path: Path | None = None,
    absolute_backstop_seconds: int = DEFAULT_ABSOLUTE_BACKSTOP_SECONDS,
    publish: Callable[[Envelope], None] | None = None,
    now: datetime | None = None,
    notified_bands: dict[str, set[str]] | None = None,
) -> list[StaleWave]:
    """Run one over-budget sweep and append prompts for new band crossings."""
    if not state_path.exists():
        logger.debug(f"sweep_once skip state-missing path={state_path!s}")
        return []
    events_path = event_path or store_path(state_path, StoreKind.EVENT)
    state = load_state(state_path)
    plans = plan_stale_waves(
        state,
        events_path=events_path,
        absolute_backstop_seconds=absolute_backstop_seconds,
        now=now,
        notified_bands=notified_bands,
    )
    emitted: list[StaleWave] = []
    for plan in plans:
        envelope = build_stale_wave_envelope(plan, now=now)
        append_envelope(events_path, envelope)
        if notified_bands is not None:
            notified_bands.setdefault(plan.wave_id, set()).add(plan.advisory_band)
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
    absolute_backstop_seconds: int = DEFAULT_ABSOLUTE_BACKSTOP_SECONDS,
    interval_seconds: int = DEFAULT_SWEEP_SECONDS,
    publish: Callable[[Envelope], None] | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run over-budget sweeps until *stop_event* is set.

    Raises:
        ValueError: When ``interval_seconds`` is non-positive.
    """
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be positive: {interval_seconds!r}")
    stop = stop_event or asyncio.Event()
    notified_bands: dict[str, set[str]] = {}
    while not stop.is_set():
        try:
            await sweep_once(
                state_path=state_path,
                event_path=event_path,
                absolute_backstop_seconds=absolute_backstop_seconds,
                publish=publish,
                notified_bands=notified_bands,
            )
        except Exception:
            logger.exception("run_sweep_loop over-budget sweep failed; will retry next tick")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
        else:
            return


__all__ = [
    "DEFAULT_ABSOLUTE_BACKSTOP_SECONDS",
    "DEFAULT_SWEEP_SECONDS",
    "OverBudgetBand",
    "StaleWave",
    "build_stale_wave_envelope",
    "plan_stale_waves",
    "run_sweep_loop",
    "sweep_once",
]
