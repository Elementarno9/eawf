"""Moving the epoch-1 ``needs_user`` pauses onto the native question and pause records.

Epoch 1 kept a skill's unanswered question as a pause row in the legacy event store,
answered by a resume row naming the same ``pause_urn``. Epoch 2 keeps the same wait as
two records: the question as an :class:`~eawf.kernel.state.epoch2.question.OpenQuestion`
and the wait on it as a user :class:`~eawf.kernel.state.epoch2.pause.OpenPause` over the
workspace, since the session that raised it is gone. The first time the daemon attaches a
tree, every legacy pause still open there is moved once: its question and pause are
written, then a resume row with the choice :data:`MIGRATED_CHOICE` closes the legacy row,
so a restart finds nothing left to move. The question's uid is derived from the legacy
``pause_urn``, so a move interrupted between the two writes is finished rather than
written twice.

Rows the budget-notice import already reinterpreted are notices now and are left to it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import orjson
from pydantic import ValidationError

from eawf.kernel.identity import EntityKind, QualifiedUrn, parse_qualified_urn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.enums import OpenQuestionStatus, StoreKind
from eawf.kernel.state.epoch2.question import MAX_OPTIONS, MIN_OPTIONS, OpenQuestion, QuestionOption
from eawf.kernel.state.epoch2.values import EntityOrigin
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import store_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.budget.legacy_notices import PAUSE_EVENT_TYPE, RESUME_EVENT_TYPE
from eawf.runtime.budget.notices import imported_pause_urns, load_notice_ledger, notices_path
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusedError, commit_row_write
from eawf.runtime.daemon.methods.host_question import held_questions, question_keys
from eawf.runtime.daemon.methods.pause import open_person_pause
from eawf.workflow.skills.bodies.user_question import UserQuestion

logger = logging.getLogger(__name__)

#: The resume choice that closes a legacy pause once its native records are written.
MIGRATED_CHOICE: Final = "migrated"

#: A moved question is written by the daemon's native path, like any other it files.
_NATIVE_ORIGIN: Final = EntityOrigin(kind="native", mapping_basis="native", confidence="exact")

#: The longest option label a question record holds.
_LABEL_LIMIT: Final = 120


@dataclass(frozen=True, slots=True)
class LegacyPause:
    """One legacy pause still open in the event store.

    Attributes:
        pause_urn: The key its resume row names.
        scope_id: The epoch-1 scope it was raised under.
        question: What it asked.
        raised_at: When it was raised.
    """

    pause_urn: str
    scope_id: str
    question: UserQuestion
    raised_at: datetime


def _payloads(events: Path) -> Iterator[tuple[str | None, EventPayload]]:
    """Yield ``(scope_id, payload)`` for every legacy event row that decodes."""
    if not events.is_file():
        return
    with events.open("rb") as handle:
        for raw in handle:
            try:
                envelope = Envelope.model_validate(orjson.loads(raw))
                if envelope.kind is StoreKind.EVENT:
                    yield envelope.scope_id, EventPayload.model_validate(envelope.payload)
            except (orjson.JSONDecodeError, ValueError) as exc:
                logger.debug(f"legacy pause scan skipped a row cause={exc!r}")


def open_legacy_pauses(state_path: Path) -> list[LegacyPause]:
    """Return every legacy pause no resume row answers, oldest first.

    A row whose question does not decode and a row the notice import took are passed
    over: neither is a question a person can still answer.

    Args:
        state_path: The tree's ``state.json``; the legacy event store is beside it.
    """
    try:
        closed = set(imported_pause_urns(load_notice_ledger(notices_path(state_path))))
    except (OSError, ValidationError) as exc:
        logger.warning(f"open_legacy_pauses unreadable-notice-ledger error={exc!r}")
        closed = set()
    raised: list[LegacyPause] = []
    for scope_id, payload in _payloads(store_path(state_path, StoreKind.EVENT)):
        urn = payload.extras.get("pause_urn")
        if not isinstance(urn, str):
            continue
        if payload.event_type == RESUME_EVENT_TYPE:
            closed.add(urn)
        elif payload.event_type == PAUSE_EVENT_TYPE:
            try:
                question = UserQuestion.model_validate_json(
                    str(payload.extras.get("user_question"))
                )
            except ValidationError:
                continue
            raised.append(LegacyPause(urn, scope_id or "", question, payload.timestamp))
    return [pause for pause in raised if pause.pause_urn not in closed]


def _close_legacy(state_path: Path, pause: LegacyPause, *, now: datetime) -> None:
    """Append the resume row that closes *pause* as migrated."""
    payload = EventPayload(
        timestamp=now,
        event_type=RESUME_EVENT_TYPE,
        actor="daemon",
        command="pause migration",
        args_hash="",
        status="ok",
        message=f"moved {pause.pause_urn} onto the native question and pause",
        extras={
            "pause_urn": pause.pause_urn,
            "scope_id": pause.scope_id,
            "choice": MIGRATED_CHOICE,
        },
    )
    append_envelope(
        store_path(state_path, StoreKind.EVENT),
        Envelope(
            id=f"EV-{uuid.uuid4().hex[:12]}",
            kind=StoreKind.EVENT,
            scope_id=pause.scope_id or None,
            created_at=now,
            updated_at=None,
            summary=f"needs_user resume {pause.pause_urn} choice={MIGRATED_CHOICE}",
            payload=payload.model_dump(mode="json"),
        ),
    )


def _question(pause: LegacyPause, workspace: QualifiedUrn, key: str, now: datetime) -> OpenQuestion:
    """Return *pause*'s question as a native open question under *key*."""
    urn = QualifiedUrn(
        workspace_key=workspace.workspace_key,
        project_key=workspace.project_key,
        repository_key=None,
        kind=EntityKind.QUESTION,
        entity_key=key,
    )
    labels = [option.label[:_LABEL_LIMIT] for option in pause.question.options]
    options = (
        tuple(
            QuestionOption(key=f"option_{index}", label=label)
            for index, label in enumerate(labels, start=1)
        )
        if MIN_OPTIONS <= len(labels) <= MAX_OPTIONS
        else ()
    )
    return OpenQuestion(
        uid=uuid.uuid5(uuid.NAMESPACE_URL, pause.pause_urn),
        key=key,
        urn=urn,
        origin=_NATIVE_ORIGIN,
        revision=1,
        created_at=pause.raised_at,
        updated_at=now,
        scope_ref=workspace,
        question=pause.question.question,
        rationale=f"asked under {pause.scope_id} before the move to epoch 2",
        options=options,
        urgency=pause.question.urgency,
        status=OpenQuestionStatus.OPEN,
    )


def _workspace(document: dict[str, Any]) -> QualifiedUrn | None:
    """Return the workspace the tree's document names, or ``None`` when it names none.

    A tree created natively states its workspace row; a tree imported from epoch 1 states
    its one project, whose code is also the workspace's key.
    """
    workspaces = document_rows(document, Epoch2Collection.WORKSPACE)
    if len(workspaces) == 1:
        return parse_qualified_urn(str(next(iter(workspaces.values()))["urn"]))
    projects = sorted(document_rows(document, Epoch2Collection.PROJECT))
    if len(projects) != 1:
        return None
    code = projects[0]
    return QualifiedUrn(
        workspace_key=code,
        project_key=code,
        repository_key=None,
        kind=EntityKind.WORKSPACE,
        entity_key=code,
    )


def _move(context: Epoch2RootContext, pause: LegacyPause, *, now: datetime) -> str | None:
    """Write *pause*'s native question and pause, returning the question's key.

    Returns:
        The key, or ``None`` when the tree states no one workspace to hold the wait.
    """
    uid = uuid.uuid5(uuid.NAMESPACE_URL, pause.pause_urn)
    authority = context.require_selected_generation()
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    workspace = _workspace(read_document(path))
    if workspace is None:
        return None
    with context.session([workspace]) as session:
        document = session.read_document()
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
        question = next(
            (q for q in held_questions(document, records).values() if q.uid == uid), None
        )
        if question is None:
            taken = question_keys(document, records)
            ordinal = max((int(key.split("-", 1)[1]) for key in taken), default=0) + 1
            question = _question(pause, workspace, f"QST-{ordinal:04d}", now)
            commit_row_write(
                session,
                collection=Epoch2Collection.OPEN_QUESTION,
                record_key=question.key,
                row=question.model_dump(mode="json"),
                event_name="question.migrated",
                event_fields={
                    "entity_ref": str(question.urn),
                    "to_status": question.status.value,
                    "revision_after": question.revision,
                    "actor": "daemon",
                },
                compaction=None,
                now=now,
            )
        open_person_pause(session, scope_ref=workspace, waiting_on_ref=question.urn, now=now)
    return question.key


def migrate_needs_user_pauses(context: Epoch2RootContext) -> tuple[str, ...]:
    """Move every legacy pause still open in the tree onto the native records, once.

    A pause whose move is refused -- its question carries a leak shape, say -- is left
    open and logged, so the others still move and a later start tries it again.

    Args:
        context: The tree the daemon just attached.

    Returns:
        The keys of the questions the legacy pauses moved onto.
    """
    state_path = context.identity.tree_root / "state.json"
    moved: list[str] = []
    for pause in open_legacy_pauses(state_path):
        now = datetime.now(UTC)
        try:
            key = _move(context, pause, now=now)
        except (TransactionRefusedError, ValidationError) as exc:
            logger.warning(f"migrate_needs_user_pauses kept pause={pause.pause_urn} error={exc!r}")
            continue
        if key is None:
            logger.warning(f"migrate_needs_user_pauses no workspace pause={pause.pause_urn}")
            return tuple(moved)
        _close_legacy(state_path, pause, now=now)
        moved.append(key)
    if moved:
        logger.info(f"migrate_needs_user_pauses root={context.identity.root_id} moved={len(moved)}")
    return tuple(moved)


__all__ = [
    "MIGRATED_CHOICE",
    "LegacyPause",
    "migrate_needs_user_pauses",
    "open_legacy_pauses",
]
