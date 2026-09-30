"""The ``runtime.host.question.*`` verbs: a question the host put to its operator, as a record.

A host harness has a tool of its own for asking its operator a multiple-choice question
and waiting on the answer. Without these verbs that exchange is invisible to Eawf: the
Run stops, and nothing says it is waiting or on what. ``raise`` records each question
the call asks as an :class:`~eawf.kernel.state.epoch2.question.OpenQuestion` bound to
the Run on the host's session and states ``question_raised`` on the Run's stream;
``answer`` records how the operator answered and states ``approval_resolved``.

The question is blocking -- the Run cannot continue until it is answered -- so it
carries no default and no deadline: nothing in the machine expires a question. The
answer is the host operator's, reported by the harness, so it is resolved under the
harness's own principal rather than under a person Eawf never saw answer. An answer
that is one of the offered options chooses it; any other answer is the operator's own
words, recorded as a reply.

A question lives on the run ledger beside the Run it holds, one line per revision
under its own ``QST-####`` key, as a provider permission does. Its key is taken from the
one key space every question of the tree shares, the questions a Campaign files in the
document included, so two questions never answer to one address. While it waits, the
Run waits on a person: ``raise`` opens a pause over the Run naming the question, and the
answer resolves that pause in the same transaction. Both verbs are idempotent on the
host's call: a retried hook finds the questions that call raised, and an answer to a
question already answered writes nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from eawf.kernel.identity import (
    EntityKind,
    QualifiedUrn,
    format_qualified_urn,
    parse_qualified_urn,
)
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.events import QuestionActionPayload, RunEventKind
from eawf.kernel.state.enums import OpenQuestionStatus
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.question import (
    MAX_OPTIONS,
    MIN_OPTIONS,
    OpenQuestion,
    QuestionOption,
    QuestionReply,
)
from eawf.kernel.state.epoch2.values import EntityOrigin
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.host_subagent import HARNESS_ACTORS
from eawf.runtime.daemon.methods.pause import open_person_pause, resolve_person_pauses
from eawf.runtime.daemon.methods.permission import host_run
from eawf.runtime.daemon.methods.run import append_run_event
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.daemon.run_events import RunEventAppend, ledger_receipt
from eawf.runtime.runtimes.host_transcript import HostHarness, scrubbed_words

logger = logging.getLogger(__name__)

#: The verb a host's question is recorded through when the host asks it.
HOST_QUESTION_RAISE_METHOD: Final = "runtime.host.question.raise"

#: The verb the host's answer to it is recorded through.
HOST_QUESTION_ANSWER_METHOD: Final = "runtime.host.question.answer"

#: The discriminator a question line carries on the run ledger.
_PAYLOAD_KIND: Final = "host_question"

#: The longest option label a question record holds.
_LABEL_LIMIT: Final = 80

#: How many hex characters of a digest name a host call.
_CALL_DIGEST_CHARS: Final = 16

#: A question the host asks is created here, never projected from epoch 1.
_NATIVE_ORIGIN: Final = EntityOrigin(kind="native", mapping_basis="native", confidence="exact")

_HostText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
_Words = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]


class HostAskedQuestion(BaseModel):
    """One question the host's call asks, as the hook read it.

    Attributes:
        question: The question in the host's words.
        options: The labels of the answers it offers.
    """

    model_config = ConfigDict(extra="forbid")

    question: _Words
    options: tuple[_Words, ...] = Field(default=(), max_length=MAX_OPTIONS)


class HostQuestionCall(BaseModel):
    """Params of both verbs: the call that asks, and the answers once there are any.

    Attributes:
        harness: The host harness asking.
        host_session_id: The host's own id of the session, or of the subagent, asking.
        tool_use_id: The host's id of the asking call.
        questions: What the call asks, in order.
        answers: The operator's answer to each question, by its words; empty until
            the call returns.
    """

    model_config = ConfigDict(extra="forbid")

    harness: HostHarness
    host_session_id: _HostText
    tool_use_id: _HostText
    questions: tuple[HostAskedQuestion, ...] = Field(min_length=1, max_length=MAX_OPTIONS)
    answers: dict[_Words, _Words] = Field(default_factory=dict)


class HostQuestionLine(BaseModel):
    """One revision of a host question on the run ledger.

    Attributes:
        payload_kind: The line's discriminator.
        asked_by: The digest of the host call that asked it.
        position: Its position among the questions that call asked.
        question: The question record itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload_kind: Literal["host_question"] = _PAYLOAD_KIND
    asked_by: _HostText
    position: Annotated[int, Field(strict=True, ge=0)]
    question: OpenQuestion


class HostQuestionAnswer(BaseModel):
    """What both verbs answer with.

    Attributes:
        run_ref: The Run the questions are bound to.
        question_refs: The questions the call asked, in order.
        answered: How many of them this call answered.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str
    question_refs: tuple[str, ...]
    answered: int = 0


def _call_digest(args: HostQuestionCall) -> str:
    """Return the name of the asking call, derived from the host's own ids."""
    body = f"{args.host_session_id}:{args.tool_use_id}"
    return f"call-{hashlib.sha256(body.encode()).hexdigest()[:_CALL_DIGEST_CHARS]}"


def _event_ref(asked_by: str, position: int, phase: str) -> str:
    """Return the id of the stream line one phase of one question is stated as."""
    body = f"{asked_by}:{position}:{phase}"
    return f"EVT-{hashlib.sha256(body.encode()).hexdigest()[:32]}"


def host_question_lines(records: tuple[LedgerRecord, ...]) -> dict[str, HostQuestionLine]:
    """Return the latest revision of every host question on the run ledger, by key."""
    latest: dict[str, HostQuestionLine] = {}
    for item in records:
        if item.payload.get("payload_kind") != _PAYLOAD_KIND:
            continue
        line = HostQuestionLine.model_validate(item.payload)
        standing = latest.get(line.question.key)
        if standing is None or line.question.revision > standing.question.revision:
            latest[line.question.key] = line
    return latest


def held_questions(
    document: dict[str, Any], records: tuple[LedgerRecord, ...]
) -> dict[str, OpenQuestion]:
    """Return every question of the tree at its latest revision, by key.

    A question a Campaign files is a row of the document; one a host asks is a line of
    the run ledger. Both are the one record, so a reader that asks what questions the
    tree holds reads them here rather than one store. A row the cutover imported from
    epoch 1 is no native question and is left out.

    Args:
        document: The tree's document.
        records: The run ledger's lines.
    """
    held = {
        key: OpenQuestion.model_validate(row)
        for key, row in document_rows(document, Epoch2Collection.OPEN_QUESTION).items()
        if "urn" in row
    }
    held.update({key: line.question for key, line in host_question_lines(records).items()})
    return held


def open_question_rows(authority: RootAuthority) -> tuple[dict[str, Any], ...]:
    """Return every question a host asked that still waits on an answer, as register rows.

    Args:
        authority: The fence-cleared tree whose run ledger is read.

    Returns:
        Each question's latest revision in key order; empty when the tree has no run
        ledger yet.
    """
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    lines = host_question_lines(read_ledger_records(ledger_path(document, Epoch2Collection.RUN)))
    live = (OpenQuestionStatus.OPEN, OpenQuestionStatus.BLOCKED)
    return tuple(
        lines[key].question.model_dump(mode="json")
        for key in sorted(lines)
        if lines[key].question.status in live
    )


def question_keys(document: dict[str, Any], records: tuple[LedgerRecord, ...]) -> set[str]:
    """Return every ``QST`` key the tree has given out, whichever store holds its question."""
    return {*document_rows(document, Epoch2Collection.OPEN_QUESTION), *host_question_lines(records)}


def _question_urn(run: QualifiedUrn, key: str) -> QualifiedUrn:
    """Return the URN of question *key*, in the project of Run *run*."""
    return parse_qualified_urn(
        format_qualified_urn(
            workspace_key=run.workspace_key,
            project_key=run.project_key,
            repository_key=None,
            kind=EntityKind.QUESTION,
            entity_key=key,
        )
    )


def _options(asked: HostAskedQuestion) -> tuple[QuestionOption, ...]:
    """Return the offered answers as keyed options; none when fewer than two are offered."""
    if len(asked.options) < MIN_OPTIONS:
        return ()
    return tuple(
        QuestionOption(
            key=f"option_{index}", label=scrubbed_words(label, limit=_LABEL_LIMIT) or "option"
        )
        for index, label in enumerate(asked.options, start=1)
    )


def append_host_question(
    session: RootSession, line: HostQuestionLine, *, now: datetime
) -> LedgerRecord:
    """Append one revision of a host question as a line of the run ledger, and return it."""
    record = LedgerRecord(
        collection=Epoch2Collection.RUN,
        record_key=line.question.key,
        status=line.question.status.value.lower(),
        recorded_at=now,
        payload=line.model_dump(mode="json"),
    )
    commit_ledger_append(session, record)
    return record


def _raise_questions(
    session: RootSession, run: QualifiedUrn, args: HostQuestionCall, *, now: datetime
) -> tuple[HostQuestionLine, ...]:
    """Record every question of the call not already recorded, and return them all."""
    asked_by = _call_digest(args)
    records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    latest = host_question_lines(records)
    standing = {line.position: line for line in latest.values() if line.asked_by == asked_by}
    taken = question_keys(session.read_document(), records)
    ordinal = max((int(key.split("-", 1)[1]) for key in taken), default=0)
    lines: list[HostQuestionLine] = []
    for position, asked in enumerate(args.questions):
        line = standing.get(position)
        if line is None:
            ordinal += 1
            key = f"QST-{ordinal:04d}"
            line = HostQuestionLine(
                asked_by=asked_by,
                position=position,
                question=OpenQuestion(
                    uid=uuid4(),
                    key=key,
                    urn=_question_urn(run, key),
                    origin=_NATIVE_ORIGIN,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                    scope_ref=run,
                    question=scrubbed_words(asked.question) or "a question with no words",
                    options=_options(asked),
                    blocking=True,
                    status=OpenQuestionStatus.BLOCKED,
                ),
            )
            append_host_question(session, line, now=now)
            open_person_pause(session, scope_ref=run, waiting_on_ref=line.question.urn, now=now)
        lines.append(line)
    return tuple(lines)


def _answered(line: HostQuestionLine, words: str, *, actor: str, now: datetime) -> OpenQuestion:
    """Return the question answered with *words*: an offered option, or a reply."""
    question = line.question
    said = scrubbed_words(words) or "an answer with no words"
    chosen = next((o.key for o in question.options if o.label == said), None)
    reply = (
        None
        if chosen is not None
        else QuestionReply(question_ref=question.urn, text=said, principal=actor, submitted_at=now)
    )
    return question.model_copy(
        update={
            "status": OpenQuestionStatus.ANSWERED,
            "resolution_actor": actor,
            "chosen_option_key": chosen,
            "reply": reply,
            "resolved_at": now,
            "revision": question.revision + 1,
            "updated_at": now,
        }
    )


def _state(
    context: Epoch2RootContext,
    run: QualifiedUrn,
    line: HostQuestionLine,
    *,
    actor: str,
    receipt: str | None,
    now: datetime,
) -> None:
    """State one phase of one question on the Run's stream: raised, or how it was answered.

    The line takes the stream's tail, because other writers share it, and its id is
    derived from the asking call, so a retried hook is answered with the line already
    standing.
    """
    question = line.question
    resolved = receipt is not None
    phase: Literal["raised", "resolved"] = "resolved" if resolved else "raised"
    append_run_event(
        context,
        RunEventAppend(
            urn=run,
            event_ref=_event_ref(line.asked_by, line.position, phase),
            run_sequence=1,
            event_kind=(
                RunEventKind.APPROVAL_RESOLVED if resolved else RunEventKind.QUESTION_RAISED
            ),
            provenance="provider_native",
            payload=QuestionActionPayload(
                subject_ref=question.urn,
                phase=phase,
                choice_key=question.chosen_option_key if resolved else None,
                receipt_ref=receipt,
            ),
            actor=actor,
        ),
        now=now,
        at_tail=True,
    )


def raise_host_questions(
    context: Epoch2RootContext, authority: RootAuthority, args: HostQuestionCall, *, now: datetime
) -> HostQuestionAnswer:
    """Record the questions a host call asks and state each on its Run's stream.

    Raises:
        DaemonValidationError: The host session is on no one live Run.
    """
    run = host_run(authority, args.host_session_id)
    actor = HARNESS_ACTORS[args.harness]
    with context.session([run]) as session:
        lines = _raise_questions(session, run, args, now=now)
    for line in lines:
        _state(context, run, line, actor=actor, receipt=None, now=now)
    logger.info(f"raise_host_questions run={run.entity_key} questions={len(lines)}")
    return HostQuestionAnswer(
        run_ref=str(run), question_refs=tuple(str(line.question.urn) for line in lines)
    )


def answer_host_questions(
    context: Epoch2RootContext, authority: RootAuthority, args: HostQuestionCall, *, now: datetime
) -> HostQuestionAnswer:
    """Record the operator's answers to a host call's questions.

    A call whose raise never arrived is raised first, so its answer still has a
    question to resolve. A question the operator left unanswered stays waiting.

    Raises:
        DaemonValidationError: The host session is on no one live Run.
    """
    run = host_run(authority, args.host_session_id)
    actor = HARNESS_ACTORS[args.harness]
    raised: list[HostQuestionLine] = []
    answered: list[tuple[HostQuestionLine, LedgerRecord]] = []
    with context.session([run]) as session:
        lines = _raise_questions(session, run, args, now=now)
        for line, asked in zip(lines, args.questions, strict=True):
            words = args.answers.get(asked.question)
            if line.question.status is OpenQuestionStatus.ANSWERED or words is None:
                raised.append(line)
                continue
            moved = line.model_copy(
                update={"question": _answered(line, words, actor=actor, now=now)}
            )
            raised.append(line)
            answered.append((moved, append_host_question(session, moved, now=now)))
        resolve_person_pauses(session, (line.question.urn for line, _ in answered), now=now)
    for line in raised:
        _state(context, run, line, actor=actor, receipt=None, now=now)
    for line, record in answered:
        _state(context, run, line, actor=actor, receipt=ledger_receipt(record), now=now)
    logger.info(f"answer_host_questions run={run.entity_key} answered={len(answered)}")
    return HostQuestionAnswer(
        run_ref=str(run),
        question_refs=tuple(str(line.question.urn) for line in lines),
        answered=len(answered),
    )


@native_mutator(HOST_QUESTION_RAISE_METHOD)
async def _raise_host_questions(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record the questions a host call asks its operator."""
    args = native_params(HostQuestionCall, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(
        raise_host_questions, context, authority, args, now=datetime.now(UTC)
    )
    return answer.model_dump(mode="json")


@native_mutator(HOST_QUESTION_ANSWER_METHOD)
async def _answer_host_questions(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record the host operator's answers to the questions a call asked."""
    args = native_params(HostQuestionCall, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(
        answer_host_questions, context, authority, args, now=datetime.now(UTC)
    )
    return answer.model_dump(mode="json")


__all__ = [
    "HOST_QUESTION_ANSWER_METHOD",
    "HOST_QUESTION_RAISE_METHOD",
    "HostAskedQuestion",
    "HostQuestionAnswer",
    "HostQuestionCall",
    "HostQuestionLine",
    "answer_host_questions",
    "append_host_question",
    "held_questions",
    "host_question_lines",
    "open_question_rows",
    "question_keys",
    "raise_host_questions",
]
