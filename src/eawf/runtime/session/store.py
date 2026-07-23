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
import uuid
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
    """Return a time-sortable, collision-resistant session id.

    Shape: ``SES-<UTC-yyyymmddTHHMMSS.ffffffZ>-<role>-<runtime>-<uuid4>``.

    Two properties matter and they pull in opposite directions, so both are
    encoded explicitly:

    - **Sortable.** The leading UTC stamp is fixed-width and zero-padded down to
      microseconds, so lexicographic order over ids equals chronological order.
    - **Collision-resistant.** A stamp alone is not unique: the previous
      second-resolution stamp made two sessions started in the same second
      produce the SAME id, and ``state.agent_sessions[sid] = session`` is a
      plain dict assignment, so the second start silently OVERWROTE the first
      row (and its ``session.start`` event id collided too). Microseconds narrow
      the window but do not close it -- a frozen/coarse clock, or two daemon
      threads inside one tick, still collide -- so a full random UUID4 suffix
      carries the uniqueness and the stamp carries only the ordering.

    Args:
        now: The session-start instant (UTC).
        runtime: Runtime adapter id recorded on the session row.
        role: The session's :class:`AgentSessionRole`.

    Returns:
        The generated session id.
    """
    stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    return f"SES-{stamp}-{role.value}-{runtime}-{uuid.uuid4()}"


def build_event(
    *,
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
    """Build (but do not append) a single EVENT envelope.

    Split out of :func:`append_event` so a caller that must not leave a durable
    trace behind a FAILED transaction can construct the envelope inside the
    transaction and append it only after the state write commits. See
    :func:`stage_session`.

    Args:
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
    return Envelope(
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


def commit_event(events_path: Path, envelope: Envelope) -> Envelope:
    """Append an envelope built by :func:`build_event` to *events_path*.

    Args:
        events_path: Path to ``events.jsonl``.
        envelope: The envelope to append.

    Returns:
        The appended envelope (for call-site chaining).
    """
    _append_canonical(events_path, envelope)
    logger.info(
        f"events store appended id={envelope.id} kind={envelope.kind.value} path={events_path}"
    )
    return envelope


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
    env = build_event(
        event_id=event_id,
        event_type=event_type,
        actor=actor,
        command=command,
        args_hash=args_hash,
        status=status,
        message=message,
        scope_id=scope_id,
        occurred_at=occurred_at,
        before_state_version=before_state_version,
        after_state_version=after_state_version,
        artifact_ids=artifact_ids,
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


def stage_session(
    *,
    state: State,
    role: AgentSessionRole,
    scope_id: str,
    runtime: str,
    now: datetime | None = None,
) -> SessionResult:
    """Write a new ``ACTIVE`` session into *state* without appending its event.

    The transactional half of :func:`start_session`. The session row + the
    ``current.active_session_ids`` pointer land in the in-memory *state*, and
    the ``session.start`` envelope is BUILT but not written, so a caller that
    goes on to reject the surrounding transaction (an H02 claim guard, say)
    simply drops the state object and leaves no durable trace: no session row,
    no index entry, no start event. The caller appends the returned envelope
    with :func:`commit_event` only after its state write commits.

    Args:
        state: State mutated in place with the new session row.
        role: The session role.
        scope_id: Scope the session anchors to (wave / iter / phase / project).
        runtime: Runtime adapter id recorded on the row.
        now: Session-start instant; defaults to ``datetime.now(UTC)``.

    Returns:
        The staged session plus its un-appended ``session.start`` envelope.

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

    event = build_event(
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
    logger.info(f"stage_session id={sid} scope={scope_id} runtime={runtime}")
    return SessionResult(session=session, event=event)


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

    :func:`stage_session` followed by an immediate event append -- the
    non-transactional surface every standalone caller (``eawf session start``,
    the jury / verdict paths) wants.

    Raises:
        SessionConflict: When an active session already exists for the pair.
    """
    staged = stage_session(state=state, role=role, scope_id=scope_id, runtime=runtime, now=now)
    commit_event(events_path, staged.event)
    logger.info(f"start_session id={staged.session.id} scope={scope_id} runtime={runtime}")
    return staged


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
    staged = _stage_session_close(
        state=state,
        session_id=session_id,
        status=status,
        summary=summary,
        now=now,
    )
    commit_event(events_path, staged.event)
    logger.info(f"close_session id={session_id} status={status.value}")
    return staged


def _stage_session_close(
    *,
    state: State,
    session_id: str,
    status: AgentSessionStatus,
    summary: str | None = None,
    now: datetime | None = None,
) -> SessionResult:
    """Terminalize one session in memory and build its close event."""
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

    event = build_event(
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
    return SessionResult(session=session, event=event)


def terminalize_session(
    *,
    state: State,
    events_path: Path,
    session_id: str,
    status: AgentSessionStatus,
    summary: str | None = None,
    now: datetime | None = None,
) -> SessionResult:
    """Terminalize a session even when its close-event append fails.

    The session row and active-pointer cleanup happen before the event write.
    Event-store failure is secondary telemetry loss: it is logged and swallowed
    so an auditor's validated result or primary spawn/report error keeps its
    original outcome while callers can still persist the terminal state.
    """
    staged = _stage_session_close(
        state=state,
        session_id=session_id,
        status=status,
        summary=summary,
        now=now,
    )
    try:
        commit_event(events_path, staged.event)
    except Exception as exc:
        logger.warning(
            f"terminalize_session id={session_id} status={status.value} "
            f"event_status=failed error={exc!r}"
        )
    logger.info(f"terminalize_session id={session_id} status={status.value}")
    return staged


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
