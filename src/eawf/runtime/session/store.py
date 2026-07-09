"""Agent-session lifecycle: ``start`` / ``checkpoint`` / ``close``.

The session record itself lives in ``state.agent_sessions``. Long-form events
(checkpoints, recovers, finishes) are appended to ``events.jsonl`` so the
session timeline survives state-cache rewrites.

Per-(scope, runtime) uniqueness is enforced at ``start`` time: starting a new
session whose ``(scope_id, runtime)`` pair already has an ``ACTIVE`` entry in
``state.agent_sessions`` raises :class:`SessionConflict`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus, StoreKind
from eawf.kernel.state.models import AgentSession, State
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope as _append_canonical
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.runtime.lock import portalock

logger = logging.getLogger(__name__)


class SessionConflict(ValueError):  # noqa: N818 — domain-specific name preferred over -Error suffix
    """Raised when a session start would collide with an existing active session."""


class SessionNotFound(LookupError):  # noqa: N818 — domain-specific name preferred over -Error suffix
    """Raised when a session ID is missing from ``state.agent_sessions``."""


def _next_session_id(now: datetime, runtime: str, role: AgentSessionRole) -> str:
    """Return ``SES-<UTC-yyyymmddTHHMMSSZ>-<role>`` suffixed with runtime tag."""
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"SES-{stamp}-{role.value}-{runtime}"


def append_event(
    *,
    events_path: Path,
    event_id: str,
    event_type: str,
    actor: str,
    command: str,
    args_hash: str,
    status: str,
    message: str,
    scope_id: str | None,
    occurred_at: datetime,
    before_state_version: str | None = None,
    after_state_version: str | None = None,
    artifact_ids: list[str] | None = None,
) -> Envelope:
    """Append a single EVENT envelope to *events_path* under the sibling lock.

    Args:
        events_path: Path to ``events.jsonl``.
        event_id: Stable ID for the event row.
        event_type: Free-string event category (``session.start``, …).
        actor: Identity emitting the event (session ID or ``"cli"``).
        command: CLI command that produced this event.
        args_hash: Hash of the CLI args (for replay).
        status: Free-string status (``"ok"``, ``"error"``, …).
        message: Human-readable message body.
        scope_id: Optional scope ID anchor.
        occurred_at: Event timestamp (also envelope ``created_at``).
        before_state_version / after_state_version: Optional state pointers.
        artifact_ids: Optional list of related artifact IDs.

    Returns:
        The constructed :class:`Envelope`.
    """
    payload = EventPayload(
        timestamp=occurred_at,
        event_type=event_type,
        actor=actor,
        command=command,
        args_hash=args_hash,
        before_state_version=before_state_version,
        after_state_version=after_state_version,
        status=status,
        message=message,
    )
    env = Envelope(
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=occurred_at,
        updated_at=None,
        summary=message[:480],
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=list(artifact_ids or []),
    )
    _append_canonical(events_path, env)
    logger.info(f"events store appended id={event_id} type={event_type} path={events_path}")
    return env


def _find_active(state: State, scope_id: str, runtime: str) -> AgentSession | None:
    """Return the active session matching (scope, runtime) or None."""
    for session in state.agent_sessions.values():
        if (
            session.scope_id == scope_id
            and session.runtime == runtime
            and session.status == AgentSessionStatus.ACTIVE
        ):
            return session
    return None


@dataclass(frozen=True)
class SessionResult:
    """Outcome of a session-store operation."""

    session: AgentSession
    event: Envelope


def start_session(
    *,
    state: State,
    events_path: Path,
    role: AgentSessionRole,
    scope_id: str,
    runtime: str,
    now: datetime | None = None,
) -> SessionResult:
    """Open a new ``ACTIVE`` session under *(scope_id, runtime)*.

    Raises:
        SessionConflict: When an active session already exists for the pair.
    """
    moment = now if now is not None else datetime.now(UTC)
    if _find_active(state, scope_id, runtime) is not None:
        raise SessionConflict(
            f"active session already exists for scope={scope_id!r} runtime={runtime!r}"
        )
    sid = _next_session_id(moment, runtime, role)
    session = AgentSession(
        id=sid,
        role=role,
        runtime=runtime,
        scope_id=scope_id,
        status=AgentSessionStatus.ACTIVE,
        claimed_wave_ids=[],
        worktree_ids=[],
        artifact_ids=[],
        started_at=moment,
        ended_at=None,
        summary=None,
    )
    state.agent_sessions[sid] = session
    if sid not in state.current.active_session_ids:
        state.current.active_session_ids = [*state.current.active_session_ids, sid]

    event = append_event(
        events_path=events_path,
        event_id=f"{sid}-start",
        event_type="session.start",
        actor=sid,
        command="session start",
        args_hash="",
        status="ok",
        message=f"session {sid} started; scope={scope_id} runtime={runtime} role={role.value}",
        scope_id=scope_id,
        occurred_at=moment,
    )
    logger.info(f"start_session id={sid} scope={scope_id} runtime={runtime}")
    return SessionResult(session=session, event=event)


def checkpoint(
    *,
    state: State,
    events_path: Path,
    session_id: str,
    artifact_ids: list[str] | None = None,
    file_globs: list[str] | None = None,
    now: datetime | None = None,
) -> SessionResult:
    """Append a ``session.checkpoint`` event and update the session record."""
    moment = now if now is not None else datetime.now(UTC)
    session = state.agent_sessions.get(session_id)
    if session is None:
        raise SessionNotFound(f"unknown session_id {session_id!r}")
    arts = list(artifact_ids or [])
    if arts:
        merged = [*session.artifact_ids, *(a for a in arts if a not in session.artifact_ids)]
        session = session.model_copy(
            update={"status": AgentSessionStatus.CHECKPOINTED, "artifact_ids": merged}
        )
    else:
        session = session.model_copy(update={"status": AgentSessionStatus.CHECKPOINTED})
    state.agent_sessions[session_id] = session

    files_msg = f" files={','.join(file_globs)}" if file_globs else ""
    arts_msg = f" artifacts={','.join(arts)}" if arts else ""
    event_id = f"{session_id}-ckpt-{moment.strftime('%Y%m%dT%H%M%SZ')}"
    event = append_event(
        events_path=events_path,
        event_id=event_id,
        event_type="session.checkpoint",
        actor=session_id,
        command="session checkpoint",
        args_hash="",
        status="ok",
        message=f"session {session_id} checkpoint{arts_msg}{files_msg}",
        scope_id=session.scope_id,
        occurred_at=moment,
        artifact_ids=arts,
    )
    logger.info(f"checkpoint id={session_id} artifacts={len(arts)} files={len(file_globs or [])}")
    return SessionResult(session=session, event=event)


def close_session(
    *,
    state: State,
    events_path: Path,
    session_id: str,
    status: AgentSessionStatus,
    summary: str | None = None,
    now: datetime | None = None,
) -> SessionResult:
    """Move *session_id* to a terminal status and emit a ``session.close`` event."""
    if status not in {
        AgentSessionStatus.CLOSED,
        AgentSessionStatus.STALE,
        AgentSessionStatus.FAILED,
    }:
        raise ValueError(
            f"close_session requires terminal status; got {status.value!r}",
        )
    moment = now if now is not None else datetime.now(UTC)
    session = state.agent_sessions.get(session_id)
    if session is None:
        raise SessionNotFound(f"unknown session_id {session_id!r}")
    session = session.model_copy(
        update={"status": status, "ended_at": moment, "summary": summary},
    )
    state.agent_sessions[session_id] = session
    if session_id in state.current.active_session_ids:
        state.current.active_session_ids = [
            sid for sid in state.current.active_session_ids if sid != session_id
        ]

    event = append_event(
        events_path=events_path,
        event_id=f"{session_id}-close",
        event_type="session.close",
        actor=session_id,
        command="session close",
        args_hash="",
        status=status.value,
        message=summary or f"session {session_id} closed with status {status.value}",
        scope_id=session.scope_id,
        occurred_at=moment,
    )
    logger.info(f"close_session id={session_id} status={status.value}")
    return SessionResult(session=session, event=event)


def reconcile_orphaned_sessions(state_path: Path, event_path: Path) -> int:
    """Flip every ``ACTIVE`` agent session to ``STALE`` at daemon boot.

    A daemon's spawned children (researcher / executor agents) die with the
    daemon, but their :class:`~eawf.kernel.state.models.AgentSession` rows stay
    ``ACTIVE`` in ``state.json``, so the Watch surface renders a zombie live
    agent that never dies. A fresh daemon owns no live children by definition,
    so every ``ACTIVE`` session at startup is orphaned -- this is a blanket
    flip, not a per-pid probe (``AgentSession`` carries no subprocess pid).

    Each flip routes through :func:`close_session`, which stamps the terminal
    status + ``ended_at``, drops the sid from ``state.current.active_session_ids``,
    and appends a ``session.close`` event to *event_path*. The whole reconcile
    runs under the canonical ``portalock(state.json)`` + one locked atomic
    write, mirroring the daemon canonical-writer path. When no session is
    ``ACTIVE`` the write is skipped (no-op) and no event is appended.

    Boot-safe: an absent or unloadable ``state.json`` (a fresh project whose
    state is not yet initialised, or a minimal stub) has no sessions to
    reconcile, so it is a no-op that returns 0 rather than crashing the daemon
    boot -- the reconcile runs before the listener starts, so a hard failure
    here would abort startup.

    Args:
        state_path: Path to ``state.json``.
        event_path: Path to ``event.jsonl`` (the ``session.close`` event sink).

    Returns:
        The count of sessions flipped from ``ACTIVE`` to ``STALE`` (0 when the
        state is absent / unloadable / has no ACTIVE session).
    """
    # Imported lazily: the ``eawf.workflow.evidence`` package __init__ pulls in
    # the verdict path, which imports this module -- a module-level import here
    # is a boot-time circular import.
    from eawf.surfaces.cli.errors import UserError, ValidationError
    from eawf.workflow.evidence._io import load_state

    if not state_path.exists():
        return 0
    with portalock.acquire(state_path, timeout=5.0):
        try:
            state = load_state(state_path)
        except (UserError, ValidationError) as exc:
            logger.debug(f"reconcile_orphaned_sessions skip reason=state-unloadable cause={exc!r}")
            return 0
        orphan_ids = [
            sid
            for sid, session in state.agent_sessions.items()
            if session.status is AgentSessionStatus.ACTIVE
        ]
        for sid in orphan_ids:
            close_session(
                state=state,
                events_path=event_path,
                session_id=sid,
                status=AgentSessionStatus.STALE,
                summary="orphaned by daemon restart",
            )
        if orphan_ids:
            atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    count = len(orphan_ids)
    logger.info(f"reconcile_orphaned_sessions flipped={count}")
    return count
