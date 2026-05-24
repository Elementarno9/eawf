"""Find sessions whose heartbeat is older than a threshold and mark them stale.

The "heartbeat" used here is the latest of:

- ``session.started_at`` from ``state.agent_sessions``,
- the timestamp of the most recent ``session.checkpoint`` event for the session
  in ``events.jsonl``.

A session is recoverable to ``stale`` when its status is currently ``ACTIVE``
or ``CHECKPOINTED`` and the inferred heartbeat is older than
``age_minutes`` (default 30 — the v0.1 spec contract).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eawf.kernel.state.enums import AgentSessionStatus, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.session.store import append_event

logger = logging.getLogger(__name__)

DEFAULT_AGE_MINUTES: int = 30


@dataclass(frozen=True)
class RecoveryReport:
    """Outcome of a single recovery sweep."""

    marked_session_ids: list[str]
    skipped_session_ids: list[str]
    age_minutes: int


def _last_checkpoint(events_path: Path, session_id: str) -> datetime | None:
    """Return the latest ``session.checkpoint`` event timestamp for the session."""
    if not events_path.exists():
        return None
    latest: datetime | None = None
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        env = Envelope.model_validate_json(line)
        if env.kind != StoreKind.EVENT:
            continue
        payload = env.payload
        if payload.get("event_type") != "session.checkpoint":
            continue
        if payload.get("actor") != session_id:
            continue
        if latest is None or env.created_at > latest:
            latest = env.created_at
    return latest


def recover_sessions(
    *,
    state: State,
    events_path: Path,
    age_minutes: int = DEFAULT_AGE_MINUTES,
    now: datetime | None = None,
) -> RecoveryReport:
    """Mark every active/checkpointed session whose heartbeat is older than the threshold.

    Caller MUST hold ``portalock(state_path)`` — typically via
    :func:`eawf.cli._mutation.state_transaction`. *state* is mutated in
    place; recovery events are appended to *events_path* under
    ``events_path``'s own sibling lock (acquired inside this function via
    :func:`eawf.runtime.session.store.append_event`).
    """
    moment = now if now is not None else datetime.now(UTC)
    threshold = timedelta(minutes=age_minutes)
    marked: list[str] = []
    skipped: list[str] = []
    for sid, session in list(state.agent_sessions.items()):
        if session.status not in {
            AgentSessionStatus.ACTIVE,
            AgentSessionStatus.CHECKPOINTED,
        }:
            skipped.append(sid)
            continue
        last_checkpoint = _last_checkpoint(events_path, sid)
        anchor = max(session.started_at, last_checkpoint) if last_checkpoint else session.started_at
        age = moment - anchor
        if age >= threshold:
            session = session.model_copy(
                update={"status": AgentSessionStatus.STALE, "ended_at": moment},
            )
            state.agent_sessions[sid] = session
            if sid in state.current.active_session_ids:
                state.current.active_session_ids = [
                    s for s in state.current.active_session_ids if s != sid
                ]
            append_event(
                events_path=events_path,
                event_id=f"{sid}-recover-{moment.strftime('%Y%m%dT%H%M%SZ')}",
                event_type="session.recover",
                actor="cli",
                command="session recover",
                args_hash="",
                status="stale",
                message=(
                    f"session {sid} marked stale "
                    f"(heartbeat age {age.total_seconds() / 60:.1f}m >= {age_minutes}m)"
                ),
                scope_id=session.scope_id,
                occurred_at=moment,
            )
            marked.append(sid)
        else:
            skipped.append(sid)
    logger.info(
        f"recover_sessions threshold={age_minutes}m marked={len(marked)} skipped={len(skipped)}"
    )
    return RecoveryReport(
        marked_session_ids=marked,
        skipped_session_ids=skipped,
        age_minutes=age_minutes,
    )
