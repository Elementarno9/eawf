"""needs_user pause store: record, list-open, and resolve a paused question.

A skill that terminates with ``header.status == "needs_user"`` carries a
:class:`~eawf.workflow.skills.bodies.user_question.UserQuestion` in
``body.user_question``. To let an operator answer that question *later*
(out of band of the original run) the question is persisted as a **pause
record** keyed by a stable ``pause-urn``; answering it appends a matching
**resume record** carrying the chosen option label.

Persistence rides the canonical event store
(``<state>/store/event.jsonl``) through
:func:`eawf.kernel.store.append.append_envelope` — the same daemon-owned append
path the lifecycle CLI uses. No new ``StoreKind`` / ``MutationKind`` is
introduced: a pause is an :class:`~eawf.kernel.store.envelope.Envelope` of kind
:attr:`~eawf.kernel.state.enums.StoreKind.EVENT` whose
:class:`~eawf.kernel.store.kinds.event.EventPayload` carries
``event_type="needs_user_pause"`` plus the serialised question in
``extras``; a resume carries ``event_type="needs_user_resume"`` plus the
chosen label. An *open* pause is a pause row whose ``pause-urn`` has no
matching resume row.

This module is the single source of truth shared by the
``eawf skill resume`` CLI verb and the TUI auto-open/pick path so the
record shape, the open-pause scan, and the label validation live in one
place (DRY).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.urn import build as build_urn
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.paths import store_path

if TYPE_CHECKING:
    from eawf.workflow.skills.bodies.user_question import UserQuestion

logger = logging.getLogger(__name__)

#: ``event_type`` value marking a pause row in ``event.jsonl``.
PAUSE_EVENT_TYPE = "needs_user_pause"

#: ``event_type`` value marking a resume row in ``event.jsonl``.
RESUME_EVENT_TYPE = "needs_user_resume"

#: ``extras`` key holding the pause-urn on both pause and resume rows.
_PAUSE_URN_KEY = "pause_urn"

#: ``extras`` key holding the serialised :class:`UserQuestion` JSON.
_QUESTION_KEY = "user_question"

#: ``extras`` key holding the originating session URN.
_SESSION_KEY = "session"

#: ``extras`` key holding the scope id on newer rows. Older rows rely on
#: ``Envelope.scope_id`` only; the scanner accepts both.
_SCOPE_ID_KEY = "scope_id"

#: ``extras`` key holding the chosen option label on a resume row.
_CHOICE_KEY = "choice"


class PauseError(ValueError):
    """Raised when a pause cannot be resolved.

    Carries a single human-readable message; callers map it onto their
    surface-specific error (the CLI onto :class:`~eawf.surfaces.cli.errors.UserError`
    with ``kind="NotFound"`` / ``kind="InvalidInput"``, the TUI onto an
    error toast).
    """


class OpenPause:
    """An unresolved needs_user pause discovered in the event store.

    Attributes:
        pause_urn: Stable ``urn:eawf:v1:event:...`` identifier for the
            pause; the key the resume row references.
        scope_id: Scope the pause belongs to (the ``Envelope.scope_id``).
        session: Originating session URN (``""`` when the pause row
            omitted it).
        question: The decoded :class:`UserQuestion` the operator answers.
    """

    __slots__ = ("pause_urn", "question", "scope_id", "session")

    def __init__(
        self,
        *,
        pause_urn: str,
        scope_id: str,
        session: str,
        question: UserQuestion,
    ) -> None:
        self.pause_urn = pause_urn
        self.scope_id = scope_id
        self.session = session
        self.question = question


def build_pause_urn(scope_id: str) -> str:
    """Return a fresh ``pause-urn`` for *scope_id*.

    Args:
        scope_id: Scope the pause belongs to (used as the URN owner).

    Returns:
        A ``urn:eawf:v1:event:<scope_id>/needs-user-<hex>`` string.
    """
    return build_urn("event", owner=scope_id, id=f"needs-user-{uuid.uuid4().hex[:12]}")


def _pause_envelope(
    *,
    pause_urn: str,
    scope_id: str,
    session: str,
    question: UserQuestion,
) -> Envelope:
    """Build the EVENT envelope that records a pause."""
    now = datetime.now(UTC)
    payload = EventPayload(
        timestamp=now,
        event_type=PAUSE_EVENT_TYPE,
        actor="skill",
        command="skill pause",
        args_hash="",
        status="needs_user",
        message=question.question,
        extras={
            _PAUSE_URN_KEY: pause_urn,
            _SCOPE_ID_KEY: scope_id,
            _SESSION_KEY: session,
            _QUESTION_KEY: question.model_dump_json(),
        },
    )
    return Envelope(
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=f"needs_user pause {pause_urn}",
        payload=payload.model_dump(mode="json"),
    )


def _resume_envelope(*, pause_urn: str, scope_id: str, choice: str) -> Envelope:
    """Build the EVENT envelope that records a resume (answer)."""
    now = datetime.now(UTC)
    payload = EventPayload(
        timestamp=now,
        event_type=RESUME_EVENT_TYPE,
        actor="skill",
        command="skill resume",
        args_hash="",
        status="ok",
        message=f"resumed {pause_urn} with choice {choice!r}",
        extras={_PAUSE_URN_KEY: pause_urn, _SCOPE_ID_KEY: scope_id, _CHOICE_KEY: choice},
    )
    return Envelope(
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=f"needs_user resume {pause_urn} choice={choice}",
        payload=payload.model_dump(mode="json"),
    )


def record_pause(
    state_path: Path,
    *,
    scope_id: str,
    session: str,
    question: UserQuestion,
    publish: Callable[[Envelope], None] | None = None,
) -> str:
    """Persist a needs_user pause and return its ``pause-urn``.

    Appends one ``needs_user_pause`` EVENT row to ``event.jsonl`` through
    the daemon-owned append path. The returned urn is the key a later
    :func:`resolve_pause` references.

    Args:
        state_path: Absolute path to ``state.json`` (the store lives at
            its sibling ``store/event.jsonl``).
        scope_id: Scope the pause belongs to.
        session: Originating session URN.
        question: The validated :class:`UserQuestion` to persist.
        publish: Optional daemon bus publisher invoked after the durable
            append succeeds.

    Returns:
        The fresh ``pause-urn`` recorded for the pause.
    """
    pause_urn = build_pause_urn(scope_id)
    envelope = _pause_envelope(
        pause_urn=pause_urn,
        scope_id=scope_id,
        session=session,
        question=question,
    )
    append_envelope(store_path(state_path, StoreKind.EVENT), envelope)
    if publish is not None:
        publish(envelope)
    logger.info(f"record_pause scope={scope_id!r} pause_urn={pause_urn!r}")
    return pause_urn


def _iter_event_payloads(events_path: Path) -> list[tuple[str | None, EventPayload]]:
    """Return ``(scope_id, EventPayload)`` for every EVENT row in *events_path*.

    Non-EVENT rows and rows that fail to decode are skipped — the scan is
    a read-only best-effort over an append-only log, so a single malformed
    line never blocks resolving an otherwise-valid pause.
    """
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
                logger.debug(f"_iter_event_payloads skip line cause={exc!r}")
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


def _payload_scope_id(env_scope: str | None, extras: dict[str, Any]) -> str:
    """Return the scope id for a pause/resume row, accepting legacy shapes."""
    if env_scope:
        return env_scope
    extra_scope = extras.get(_SCOPE_ID_KEY)
    if isinstance(extra_scope, str):
        return extra_scope
    legacy_scope = extras.get("scope")
    if isinstance(legacy_scope, str):
        return legacy_scope
    return ""


def _decode_question(raw_question: object) -> UserQuestion | None:
    """Decode a question from current JSON-string or legacy object shape."""
    from eawf.workflow.skills.bodies.user_question import UserQuestion

    try:
        if isinstance(raw_question, str):
            return UserQuestion.model_validate_json(raw_question)
        if isinstance(raw_question, dict):
            return UserQuestion.model_validate(raw_question)
    except ValueError as exc:
        logger.debug(f"_decode_question skip error={exc!r}")
    return None


def list_open_pauses(state_path: Path, *, scope_id: str | None = None) -> list[OpenPause]:
    """Return unresolved pauses, newest last, optionally filtered by scope.

    Walks ``event.jsonl`` once, pairs every ``needs_user_pause`` row with
    its matching ``needs_user_resume`` row (by ``pause-urn``), and returns
    the pauses that have no resume. Rows whose stored question fails to
    decode are skipped.

    Args:
        state_path: Absolute path to ``state.json``.
        scope_id: When set, only pauses for this scope are returned.

    Returns:
        Open :class:`OpenPause` records in append order (oldest first).
    """
    resolved: set[str] = set()
    pending: list[OpenPause] = []
    for env_scope, payload in _iter_event_payloads(store_path(state_path, StoreKind.EVENT)):
        urn = payload.extras.get(_PAUSE_URN_KEY)
        if not isinstance(urn, str):
            continue
        row_scope_id = _payload_scope_id(env_scope, payload.extras)
        if payload.event_type == RESUME_EVENT_TYPE:
            resolved.add(urn)
            continue
        if payload.event_type != PAUSE_EVENT_TYPE:
            continue
        if scope_id is not None and row_scope_id != scope_id:
            continue
        question = _decode_question(payload.extras.get(_QUESTION_KEY))
        if question is None:
            logger.debug(f"list_open_pauses outcome=skip_undecodable pause_urn={urn!r}")
            continue
        session = payload.extras.get(_SESSION_KEY)
        pending.append(
            OpenPause(
                pause_urn=urn,
                scope_id=row_scope_id,
                session=session if isinstance(session, str) else "",
                question=question,
            )
        )
    return [p for p in pending if p.pause_urn not in resolved]


def find_open_pause(state_path: Path, pause_urn: str) -> OpenPause:
    """Return the open pause identified by *pause_urn*.

    Args:
        state_path: Absolute path to ``state.json``.
        pause_urn: The ``pause-urn`` to resolve.

    Returns:
        The matching open :class:`OpenPause`.

    Raises:
        PauseError: When no open pause carries *pause_urn* (it never
            existed or it was already resolved).
    """
    for pause in list_open_pauses(state_path):
        if pause.pause_urn == pause_urn:
            return pause
    raise PauseError(f"unknown or already-resolved pause: {pause_urn!r}")


def validate_choice(pause: OpenPause, choice: str) -> str:
    """Return *choice* when it names one of *pause*'s option labels.

    Args:
        pause: The open pause whose question constrains the answer.
        choice: The operator-supplied option label.

    Returns:
        The validated *choice* (returned verbatim for caller convenience).

    Raises:
        PauseError: When *choice* is not one of the question's labels.
    """
    labels = [opt.label for opt in pause.question.options]
    if choice not in labels:
        raise PauseError(f"invalid choice {choice!r}; expected one of {sorted(labels)}")
    return choice


def resolve_pause(
    state_path: Path,
    *,
    pause_urn: str,
    choice: str,
    publish: Callable[[Envelope], None] | None = None,
) -> OpenPause:
    """Resolve the pause *pause_urn* with *choice* and persist the answer.

    Looks up the open pause, validates *choice* against its question's
    options, then appends a ``needs_user_resume`` EVENT row recording the
    answer. The append rides the daemon-owned event-store path.

    Args:
        state_path: Absolute path to ``state.json``.
        pause_urn: The ``pause-urn`` to resolve.
        choice: The chosen option label.
        publish: Optional daemon bus publisher invoked after the durable
            append succeeds.

    Returns:
        The :class:`OpenPause` that was resolved.

    Raises:
        PauseError: When *pause_urn* is unknown / already resolved, or
            *choice* is not one of the question's labels.
    """
    pause = find_open_pause(state_path, pause_urn)
    validate_choice(pause, choice)
    envelope = _resume_envelope(pause_urn=pause_urn, scope_id=pause.scope_id, choice=choice)
    append_envelope(store_path(state_path, StoreKind.EVENT), envelope)
    if publish is not None:
        publish(envelope)
    logger.info(f"resolve_pause pause_urn={pause_urn!r} choice={choice!r}")
    return pause


__all__ = [
    "PAUSE_EVENT_TYPE",
    "RESUME_EVENT_TYPE",
    "OpenPause",
    "PauseError",
    "build_pause_urn",
    "find_open_pause",
    "list_open_pauses",
    "record_pause",
    "resolve_pause",
    "validate_choice",
]
