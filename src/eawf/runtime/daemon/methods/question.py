"""The ``runtime.question.*`` verbs: every question of a tree, read and answered in one place.

A question a Campaign files is a row of the document and one a host asks is a line of the
run ledger; both are one :class:`~eawf.kernel.state.epoch2.question.OpenQuestion`. ``read``
reports them together, each with the situation it projects to for the principal reading,
computed at every read and never stored, and the state of each asking Run. ``answer``
records an operator's answer -- one of the offered options, or a reply in their own words
-- as the ``ANSWERED`` transition in one transaction with the resolution of every pause
that waited on the question.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.identity import EntityKind
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.question import (
    MAX_REPLY_LENGTH,
    OpenQuestion,
    QuestionRefusedError,
    QuestionReply,
    QuestionSituation,
    answer_with_option,
    answer_with_reply,
    project_question,
)
from eawf.kernel.state.epoch2.urns import QuestionUrn
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusalCode,
    TransactionRefusedError,
    commit_row_write,
)
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.methods.delivery_approval import publish_commits
from eawf.runtime.daemon.methods.host_question import (
    HostQuestionLine,
    append_host_question,
    held_questions,
    host_question_lines,
)
from eawf.runtime.daemon.methods.pause import resolve_person_pauses
from eawf.runtime.daemon.native_guard import native_mutator, native_params, require_native_call

logger = logging.getLogger(__name__)

#: The verb every question of a tree is read through.
QUESTION_READ_METHOD: Final = "runtime.question.read"

#: The verb an operator's answer to one question is recorded through.
QUESTION_ANSWER_METHOD: Final = "runtime.question.answer"

#: The Run state an asking Run is reported in when it can no longer hear an answer.
_UNREACHABLE: Final = frozenset({"LOST"})


class _ReadParams(BaseModel):
    """Params of :data:`QUESTION_READ_METHOD`.

    Attributes:
        principal: Who reads, which tells an answer they gave from one given elsewhere.
    """

    model_config = ConfigDict(extra="forbid")

    principal: PrincipalKey | None = None


class QuestionItem(BaseModel):
    """One question as the read reports it.

    Attributes:
        question: The record, at its latest revision.
        asked_by_run: The key of the Run that asked it; ``None`` for a question no Run asked.
        situation: The situation it projects to for the reading principal.
        ends_when: What would end it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: OpenQuestion
    asked_by_run: str | None = None
    situation: QuestionSituation
    ends_when: str


class QuestionsAnswer(BaseModel):
    """What :data:`QUESTION_READ_METHOD` answers.

    Attributes:
        questions: Every question of the tree, by key.
        run_states: The state of each asking Run, by Run key; a Run the document no
            longer holds is absent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    questions: tuple[QuestionItem, ...] = ()
    run_states: dict[str, str] = {}


class AnswerParams(BaseModel):
    """Params of :data:`QUESTION_ANSWER_METHOD`: exactly one of an option or a reply.

    Attributes:
        urn: The question answered.
        expected_revision: The revision the operator was shown.
        actor: Who answers.
        option_key: The option chosen, for an answer by option.
        reply: The operator's own words, for an answer by reply.
    """

    model_config = ConfigDict(extra="forbid")

    urn: QuestionUrn
    expected_revision: int
    actor: PrincipalKey
    option_key: str | None = None
    reply: Annotated[str, Field(min_length=1, max_length=MAX_REPLY_LENGTH)] | None = None

    @model_validator(mode="after")
    def _one_answer(self) -> Self:
        if (self.option_key is None) == (self.reply is None):
            raise ValueError("an answer is exactly one of an option or a reply")
        return self


class AnswerResult(BaseModel):
    """What :data:`QUESTION_ANSWER_METHOD` answers.

    Attributes:
        question: The answered question.
        resolved_pauses: The pauses the answer resolved.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: OpenQuestion
    resolved_pauses: tuple[str, ...] = ()


def read_questions(
    document: dict[str, Any], records: tuple[LedgerRecord, ...], *, principal: str | None
) -> QuestionsAnswer:
    """Report every question of the tree with the situation it projects to.

    Args:
        document: The tree's document.
        records: The run ledger's lines.
        principal: Who reads; ``None`` projects every answer as given elsewhere.
    """
    runs = document_rows(document, Epoch2Collection.RUN)
    items: list[QuestionItem] = []
    run_states: dict[str, str] = {}
    for key, question in sorted(held_questions(document, records).items()):
        asker = question.scope_ref.entity_key if question.scope_ref.kind is EntityKind.RUN else None
        if asker is not None and asker in runs:
            run_states[asker] = str(runs[asker].get("status"))
        projection = project_question(
            question,
            principal=principal or "",
            asking_run_unreachable=run_states.get(asker or "") in _UNREACHABLE,
        )
        items.append(
            QuestionItem(
                question=question,
                asked_by_run=asker,
                situation=projection.situation,
                ends_when=projection.ends_when,
            )
        )
        logger.debug(f"read_questions key={key} situation={projection.situation.value}")
    return QuestionsAnswer(questions=tuple(items), run_states=run_states)


def _answered(question: OpenQuestion, args: AnswerParams, now: datetime) -> OpenQuestion:
    """Return *question* answered as *args* asks, or refuse the answer.

    Raises:
        TransactionRefusedError: The answer is refused; the refusal's guard is the
            question's own code.
    """
    try:
        if args.reply is not None:
            reply = QuestionReply(
                question_ref=question.urn, text=args.reply, principal=args.actor, submitted_at=now
            )
            answered = answer_with_reply(question, reply)
        else:
            assert args.option_key is not None, "validated as exactly one answer"
            answered = answer_with_option(
                question, args.option_key, principal=args.actor, submitted_at=now
            )
    except QuestionRefusedError as error:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.TRANSITION_GUARD_FAILED,
            detail=f"{error.code}: {error}",
            entity_ref=str(question.urn),
            guard=error.code,
            remediation=error.remediation,
            revision=question.revision,
        ) from error
    return answered.model_copy(update={"revision": question.revision + 1, "updated_at": now})


def _record(
    session: RootSession, question: OpenQuestion, line: HostQuestionLine | None, *, now: datetime
) -> Envelope | None:
    """Write the answered *question* back to the store that holds it.

    Returns:
        The firehose row a document write appended; ``None`` for a ledger line.
    """
    if line is not None:
        append_host_question(session, line.model_copy(update={"question": question}), now=now)
        return None
    return commit_row_write(
        session,
        collection=Epoch2Collection.OPEN_QUESTION,
        record_key=question.key,
        row=question.model_dump(mode="json"),
        event_name="question.answered",
        event_fields={
            "entity_ref": str(question.urn),
            "to_status": question.status.value,
            "revision_after": question.revision,
            "actor": question.resolution_actor,
        },
        compaction=None,
        now=now,
    )


def answer_question(
    context: Epoch2RootContext, args: AnswerParams, *, now: datetime
) -> tuple[AnswerResult, tuple[Envelope, ...]]:
    """Record one operator's answer to one question and resolve what waited on it.

    Returns:
        The answered question with the pauses it resolved, and the rows to publish.

    Raises:
        TransactionRefusedError: No question is under the URN, the operator was shown
            another revision, or the answer is refused.
    """
    key = args.urn.entity_key
    with context.session([args.urn]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
        question = held_questions(session.read_document(), records).get(key)
        if question is None or question.urn != args.urn:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
                detail=f"no question is recorded under {key}",
                entity_ref=str(args.urn),
                remediation="Read the questions again and answer one the tree holds.",
            )
        if question.revision != args.expected_revision:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.REVISION_CONFLICT,
                detail=f"{key} is at revision {question.revision}, not {args.expected_revision}",
                entity_ref=str(args.urn),
                remediation="Read the question again and answer what it now asks.",
                revision=question.revision,
            )
        answered = _answered(question, args, now)
        envelope = _record(session, answered, host_question_lines(records).get(key), now=now)
        resolved = resolve_person_pauses(session, (answered.urn,), now=now)
    logger.info(f"answer_question key={key} resolved_pauses={len(resolved)}")
    envelopes = (envelope,) if envelope is not None else ()
    return AnswerResult(question=answered, resolved_pauses=resolved), envelopes


def _read(path: Path, args: _ReadParams) -> QuestionsAnswer:
    """Read the tree's questions from the document at *path* and the run ledger beside it."""
    records = read_ledger_records(ledger_path(path, Epoch2Collection.RUN))
    return read_questions(read_document(path), records, principal=args.principal)


@register(QUESTION_READ_METHOD)
async def _read_questions(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Report every question of the tree with the situation it projects to."""
    authority = require_native_call(ctx, params)
    args = native_params(_ReadParams, params)
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    answer = await asyncio.to_thread(_read, path, args)
    return answer.model_dump(mode="json")


@native_mutator(QUESTION_ANSWER_METHOD)
async def _answer_question(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record an operator's answer to one question."""
    args = native_params(AnswerParams, params)
    context = ctx.native_root_context(authority.root)
    result, envelopes = await asyncio.to_thread(
        answer_question, context, args, now=datetime.now(UTC)
    )
    publish_commits(ctx, envelopes)
    return result.model_dump(mode="json")


__all__ = [
    "QUESTION_ANSWER_METHOD",
    "QUESTION_READ_METHOD",
    "AnswerParams",
    "AnswerResult",
    "QuestionItem",
    "QuestionsAnswer",
    "answer_question",
    "read_questions",
]
