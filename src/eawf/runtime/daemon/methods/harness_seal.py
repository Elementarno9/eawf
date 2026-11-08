"""Sealing a marked pending action from the host operator's answer to its question.

A pending action shown to the operator through the host's own question tool opens
with the line ``[eawf act=ACT-NNNN rev=R]``. When the host reports the operator's
answer to such a question, :func:`seal_from_host_answer` seals the action with it,
under the operator this machine's user config claims, so the operator answers where
the question was asked rather than at their terminal.

The harness's report is the only witness, and the same user who runs the harness can
write it, so this is a habit guard and not a security boundary. Every check below
refuses a report that does not look like a person answering the question the action
asks: no raise before the answer, answers supplied in the tool input it was raised
with, a multi-select call, option labels that are not the action's in its order, a
question asked in other words than the action presents as, an answer that is not one
of the labels, one action marked twice, an action not held or not waiting at the
marked revision, no operator claim, an action addressed to another principal, or a
transcript that contradicts the report.

The words are those :func:`~eawf.workflow.host_question.present_pending_action`
renders for the action at the marked revision: the question with its marker line,
and each option's label and description, and its preview when the call shows one.
They are compared as the call's own input carried them, with each run of whitespace
read as one space and the ends trimmed, so re-wrapping a line still seals and any
other change of wording does not. The presentation names no header, so the short
header the host shows above the question is the asker's and is not compared.

What the checks could not establish -- a transcript that holds only the question yet,
or none at all, or a transcript path that names no regular file -- and how long the
question stood are recorded on the seal's attestation rather than refused.

A refusal never blocks the host and never undoes the recorded answer: the question
stays answered on the run ledger, the action stays waiting, and the refusal is
reported with its code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.config.operator_claim import claimed_operator
from eawf.kernel.identity import QualifiedUrn, parse_qualified_urn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import OPERATOR_PRINCIPAL_PREFIX
from eawf.kernel.state.epoch2.pending_action import (
    HarnessAttestation,
    HumanPrincipal,
    PendingAction,
    PendingActionStatus,
    SealingTranscriptCheck,
)
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusedError
from eawf.runtime.daemon.methods.delivery_acceptance import RecordEvidenceParams, record_evidence
from eawf.runtime.daemon.methods.delivery_approval import (
    ActionCommitRequest,
    _commit_seal,
    _SealAnswer,
    action_urn,
    held_actions,
)
from eawf.runtime.runtimes.host_transcript import question_exchange
from eawf.workflow.delivery.acceptance_approval import ApprovalRefusal
from eawf.workflow.host_question import (
    QuestionPresentationError,
    action_marker,
    present_pending_action,
)

if TYPE_CHECKING:
    from eawf.runtime.daemon.methods.host_question import (
        HostAskedQuestion,
        HostQuestionCall,
        HostQuestionLine,
    )

logger = logging.getLogger(__name__)

#: The principal a harness seal is committed under: the harness that reported it.
HARNESS_SEAL_ACTOR: Final = "HARNESS-CLAUDE-CODE"

#: How many hex characters of a digest name one seal's evidence and commit.
_KEY_DIGEST_CHARS: Final = 24

#: The seal refusals that mean the action moved before the seal could land.
_MOVED: Final = frozenset({ApprovalRefusal.ACTION_NOT_WAITING, ApprovalRefusal.ACTION_STALE})


class HarnessSealRefusal(StrEnum):
    """Why an answer reported by the harness did not seal its marked action."""

    RAISE_MISSING = "harness_raise_missing"
    PREANSWERED = "harness_preanswered"
    MULTI_SELECT = "harness_multi_select"
    OPTIONS_MISMATCH = "harness_options_mismatch"
    QUESTION_MISMATCH = "harness_question_mismatch"
    ANSWER_UNOFFERED = "harness_answer_unoffered"
    MARKER_DUPLICATE = "harness_marker_duplicate"
    ACTION_UNHELD = "harness_action_unheld"
    ACTION_STALE = "harness_action_stale"
    OPERATOR_UNCLAIMED = "harness_operator_unclaimed"
    ASSIGNEE_MISMATCH = "harness_assignee_mismatch"
    TRANSCRIPT_MISMATCH = "harness_transcript_mismatch"


class SealRefusal(BaseModel):
    """One marked action an answer did not seal, and why.

    Attributes:
        action_ref: The action the question was marked with.
        code: The check that refused it.
        detail: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_ref: str
    code: HarnessSealRefusal
    detail: str


@dataclass(frozen=True, slots=True)
class HarnessSeal:
    """What sealing from one host answer achieved.

    Attributes:
        sealed_action_refs: The actions sealed, in question order.
        seal_refusals: The marked actions not sealed, each with its code.
        envelopes: The firehose rows the seals appended, to publish once the
            locks are released.
    """

    sealed_action_refs: tuple[str, ...] = ()
    seal_refusals: tuple[SealRefusal, ...] = ()
    envelopes: tuple[Envelope, ...] = ()


class _SealRefusedError(Exception):
    """One check refused the seal."""

    def __init__(self, code: HarnessSealRefusal, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _digest(value: object) -> str:
    """Return the ``sha256:`` digest of *value* as sorted-key JSON."""
    body = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(body.encode()).hexdigest()}"


def _held(authority: RootAuthority) -> dict[str, PendingAction]:
    """Return every pending action the tree holds, read without a lock."""
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    return held_actions(read_document(document))


def marked_action(authority: RootAuthority, call: HostQuestionCall) -> PendingAction | None:
    """Return the first held action a question of *call* is marked with, if any.

    Args:
        authority: The fence-cleared tree.
        call: The host's question call.

    Returns:
        The action; ``None`` when no question is marked with one the tree holds.
    """
    held = _held(authority)
    for asked in call.questions:
        marker = action_marker(asked.question)
        if marker is not None and marker[0] in held:
            return held[marker[0]]
    return None


def _operator() -> str:
    """Return the operator this machine's user claims to be.

    Raises:
        _SealRefusedError: ``harness_operator_unclaimed`` when no operator principal is
            claimed, or the claim cannot be read.
    """
    try:
        claimed = claimed_operator()
    except ValueError as error:
        raise _SealRefusedError(HarnessSealRefusal.OPERATOR_UNCLAIMED, str(error)) from error
    if claimed is None or not claimed.startswith(OPERATOR_PRINCIPAL_PREFIX):
        raise _SealRefusedError(
            HarnessSealRefusal.OPERATOR_UNCLAIMED,
            f"the user config claims {claimed or 'no principal'}, not an "
            f"{OPERATOR_PRINCIPAL_PREFIX} operator; claim one with "
            "eawf config set operator.principal OP-NNNN --scope global",
        )
    return claimed


@dataclass(frozen=True, slots=True)
class _Admitted:
    """An answer every check admitted, with what the seal records of it."""

    action: PendingAction
    option_id: str
    operator: str
    transcript_check: SealingTranscriptCheck
    raise_gap_ms: int


def _require_asked_once(
    call: HostQuestionCall,
    asked: HostAskedQuestion,
    *,
    key: str,
    raised: HostQuestionLine | None,
    marked: Sequence[str],
) -> HostQuestionLine:
    """Return the raise that stood before the answer, once the call reads as asked once.

    Raises:
        _SealRefusedError: ``harness_raise_missing``, ``harness_preanswered``,
            ``harness_multi_select`` or ``harness_marker_duplicate``.
    """
    if raised is None:
        raise _SealRefusedError(
            HarnessSealRefusal.RAISE_MISSING,
            "the question was never reported before it was answered",
        )
    # The answer report's own input holds the operator's answers, so only the raise
    # tells a question put to nobody from one the operator answered.
    if raised.preanswered:
        raise _SealRefusedError(
            HarnessSealRefusal.PREANSWERED,
            "the call carried its answers in its input before it was asked, so nobody was asked",
        )
    if asked.multi_select:
        raise _SealRefusedError(
            HarnessSealRefusal.MULTI_SELECT, "a multi-select question chooses no one option"
        )
    if marked.count(key) > 1:
        raise _SealRefusedError(
            HarnessSealRefusal.MARKER_DUPLICATE, f"{key} is marked on more than one question"
        )
    return raised


def _waiting_action(held: Mapping[str, PendingAction], key: str, revision: int) -> PendingAction:
    """Return the marked action, once it is held and waiting at the marked revision.

    Raises:
        _SealRefusedError: ``harness_action_unheld`` or ``harness_action_stale``.
    """
    action = held.get(key)
    if action is None:
        raise _SealRefusedError(HarnessSealRefusal.ACTION_UNHELD, f"the tree holds no {key}")
    if action.status is not PendingActionStatus.WAITING or action.revision != revision:
        raise _SealRefusedError(
            HarnessSealRefusal.ACTION_STALE,
            f"{key} is {action.status.value} at revision {action.revision}, not "
            f"{PendingActionStatus.WAITING.value} at the marked revision {revision}",
        )
    return action


def _transcript_check(call: HostQuestionCall) -> SealingTranscriptCheck:
    """Return how the host transcript bears the report out.

    Raises:
        _SealRefusedError: ``harness_transcript_mismatch`` when it contradicts it.
    """
    if call.transcript_path is None or call.tool_input_questions is None:
        return "absent"
    path = Path(call.transcript_path)
    # Opening a pipe or a device for reading can block the daemon thread, so only a
    # regular file is read; anything else is no transcript at all.
    try:
        regular = stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        regular = False
    if not regular:
        return "absent"
    check = question_exchange(path, call.tool_use_id, call.tool_input_questions, call.answers)
    if check == "mismatch":
        raise _SealRefusedError(
            HarnessSealRefusal.TRANSCRIPT_MISMATCH,
            "the host transcript records this call otherwise than the hook reported it",
        )
    return check


def _plain(text: object) -> str | None:
    """Return *text* with each whitespace run read as one space, or ``None`` if not text."""
    return " ".join(text.split()) if isinstance(text, str) else None


def _asked_words(
    item: Mapping[str, object],
) -> tuple[str | None, tuple[tuple[str | None, ...], ...]]:
    """Return the words one question of the call's input shows: its question and options."""
    raw = item.get("options")
    options = raw if isinstance(raw, list) else []
    shown: list[tuple[str | None, ...]] = []
    for option in options:
        if not isinstance(option, dict):
            shown.append((None,))
            continue
        words: tuple[str | None, ...] = (
            _plain(option.get("label")),
            _plain(option.get("description")),
        )
        if "preview" in option:
            words = (*words, _plain(option["preview"]))
        shown.append(words)
    return _plain(item.get("question")), tuple(shown)


def _require_presented_words(
    call: HostQuestionCall, asked: HostAskedQuestion, action: PendingAction, *, key: str
) -> None:
    """Refuse a question asked in other words than *action* presents as.

    Raises:
        _SealRefusedError: ``harness_question_mismatch`` when the hook did not forward
            the question as the call's input carried it, the action cannot be
            presented, or the question, a label, a description or a shown preview
            differs from the presentation.
    """
    items = [
        item for item in call.tool_input_questions or () if item.get("question") == asked.question
    ]
    if len(items) != 1:
        raise _SealRefusedError(
            HarnessSealRefusal.QUESTION_MISMATCH,
            "the hook did not report the question as the call's input carried it",
        )
    try:
        presented = present_pending_action(action)
    except QuestionPresentationError as error:
        raise _SealRefusedError(
            HarnessSealRefusal.QUESTION_MISMATCH, f"{key} cannot be presented: {error}"
        ) from error
    question, shown = _asked_words(items[0])
    # A host that shows no preview is not asked to carry one; a preview shown is compared.
    expected = tuple(
        (
            _plain(option.label),
            _plain(option.description),
            *((_plain(option.preview),) if len(words) == 3 else ()),
        )
        for option, words in zip(presented.options, shown, strict=False)
    )
    if (
        question != _plain(presented.question)
        or len(shown) != len(presented.options)
        or expected != shown
    ):
        raise _SealRefusedError(
            HarnessSealRefusal.QUESTION_MISMATCH,
            f"the question was asked in other words than {key} presents at revision "
            f"{action.revision}",
        )


def _admit(
    call: HostQuestionCall,
    line: HostQuestionLine,
    *,
    key: str,
    revision: int,
    raised: HostQuestionLine | None,
    marked: Sequence[str],
    held: Mapping[str, PendingAction],
    now: datetime,
) -> _Admitted:
    """Return the answer to one marked question once every check admits it.

    Raises:
        _SealRefusedError: A check refused the seal; its code names which.
    """
    asked = call.questions[line.position]
    words = call.answers[asked.question]
    standing = _require_asked_once(call, asked, key=key, raised=raised, marked=marked)
    action = _waiting_action(held, key, revision)
    labels = [option.label for option in action.options]
    if list(asked.options) != labels:
        raise _SealRefusedError(
            HarnessSealRefusal.OPTIONS_MISMATCH,
            f"the question offered other options than {key}, or in another order",
        )
    _require_presented_words(call, asked, action, key=key)
    chosen = [option.option_id for option in action.options if option.label == words]
    if len(chosen) != 1:
        raise _SealRefusedError(
            HarnessSealRefusal.ANSWER_UNOFFERED,
            f"the answer is not exactly one of the options {key} offers",
        )
    operator = _operator()
    if action.assignee_ref is not None and action.assignee_ref != operator:
        raise _SealRefusedError(
            HarnessSealRefusal.ASSIGNEE_MISMATCH,
            f"{key} is addressed to {action.assignee_ref}, not the claimed {operator}",
        )
    check = _transcript_check(call)
    gap = max(0, int((now - standing.question.created_at).total_seconds() * 1000))
    return _Admitted(
        action=action,
        option_id=chosen[0],
        operator=operator,
        transcript_check=check,
        raise_gap_ms=gap,
    )


def _seal(
    context: Epoch2RootContext,
    call: HostQuestionCall,
    line: HostQuestionLine,
    admitted: _Admitted,
    *,
    run: QualifiedUrn | None,
    now: datetime,
) -> tuple[Envelope, ...]:
    """File the answer as evidence and seal the action with it.

    Raises:
        _SealRefusedError: ``harness_action_stale`` when the action moved before the
            seal landed.
    """
    action = admitted.action
    questions: Any = (
        call.tool_input_questions
        if call.tool_input_questions is not None
        else [asked.model_dump(mode="json") for asked in call.questions]
    )
    attestation = HarnessAttestation(
        harness="claude-code",
        host_session_digest=_digest(call.host_session_id),
        tool_use_id=call.tool_use_id,
        question_ref=line.question.urn,
        run_ref=run,
        payload_digest=_digest({"questions": questions, "answers": call.answers}),
        transcript_check=admitted.transcript_check,
        raise_gap_ms=admitted.raise_gap_ms,
        permission_mode=None,
    )
    name = _digest([call.host_session_id, call.tool_use_id, line.position]).removeprefix("sha256:")
    name = name[:_KEY_DIGEST_CHARS]
    evidence = record_evidence(
        context,
        RecordEvidenceParams(
            urn=action.urn,
            actor=HARNESS_SEAL_ACTOR,
            idempotency_key=f"harness-evidence-{name}",
            kind="store_record",
            summary=(
                f"{action.id} answered {admitted.option_id} through {line.question.key} "
                f"by {admitted.operator}; transcript_check={admitted.transcript_check}"
            ),
        ),
        now=now,
    )
    resolver = HumanPrincipal(principal_kind="human", principal_id=admitted.operator)
    receipt = parse_qualified_urn(evidence.evidence_ref)
    request = ActionCommitRequest(
        urn=action.urn,
        idempotency_key=f"harness-seal-{name}",
        actor=HARNESS_SEAL_ACTOR,
        operation="seal",
        detail={
            "expected_revision": action.revision,
            "resolver": resolver.model_dump(mode="json"),
            "option_id": admitted.option_id,
            "receipt_ref": str(receipt),
            "harness_attestation": attestation.model_dump(mode="json"),
        },
    )
    answer = _SealAnswer(
        urn=action.urn,
        expected_revision=action.revision,
        resolver=resolver,
        option_id=admitted.option_id,
        receipt_ref=receipt,
        harness_attestation=attestation,
    )
    try:
        commit = _commit_seal(context, request=request, args=answer, now=now)
    except TransactionRefusedError as error:
        if error.guard not in _MOVED:
            raise
        raise _SealRefusedError(HarnessSealRefusal.ACTION_STALE, str(error)) from error
    if commit.answer.receipt_ref != str(receipt):
        raise _SealRefusedError(
            HarnessSealRefusal.ACTION_STALE, f"{action.id} was sealed by another answer first"
        )
    return commit.envelopes


def seal_from_host_answer(
    context: Epoch2RootContext,
    authority: RootAuthority,
    call: HostQuestionCall,
    *,
    answered: Sequence[HostQuestionLine],
    raised: Mapping[int, HostQuestionLine],
    scope: QualifiedUrn,
    run: QualifiedUrn | None,
    now: datetime,
) -> HarnessSeal:
    """Seal each action a question answered by this call is marked with.

    Args:
        context: The native context of the tree.
        authority: The fence-cleared tree, read for the marked actions.
        call: The host's report of the call, with its answers.
        answered: The questions this call answered, as just recorded.
        raised: The questions of this call already recorded before this report,
            by position.
        scope: What the questions are scoped to, which addresses an action the
            tree does not hold.
        run: The Run the host session is on, if any.
        now: When the answer was reported.

    Returns:
        Each action sealed and each marked action refused, with the rows to
        publish.
    """
    marks = {
        line.position: action_marker(call.questions[line.position].question) for line in answered
    }
    if call.harness != "claude-code" or not any(marks.values()):
        return HarnessSeal()
    marked = [
        found[0]
        for found in (action_marker(asked.question) for asked in call.questions)
        if found is not None
    ]
    held = _held(authority)
    sealed: list[str] = []
    refusals: list[SealRefusal] = []
    envelopes: list[Envelope] = []
    for line in answered:
        mark = marks[line.position]
        if mark is None:
            continue
        key, revision = mark
        ref = str(held[key].urn) if key in held else str(action_urn(scope, key))
        try:
            admitted = _admit(
                call,
                line,
                key=key,
                revision=revision,
                raised=raised.get(line.position),
                marked=marked,
                held=held,
                now=now,
            )
            envelopes.extend(_seal(context, call, line, admitted, run=run, now=now))
        except _SealRefusedError as refused:
            logger.info(f"seal_from_host_answer refused action={key} code={refused.code.value}")
            refusals.append(SealRefusal(action_ref=ref, code=refused.code, detail=refused.detail))
            continue
        logger.info(f"seal_from_host_answer sealed action={key} question={line.question.key}")
        sealed.append(ref)
    return HarnessSeal(
        sealed_action_refs=tuple(sealed),
        seal_refusals=tuple(refusals),
        envelopes=tuple(envelopes),
    )


__all__ = [
    "HARNESS_SEAL_ACTOR",
    "HarnessSeal",
    "HarnessSealRefusal",
    "SealRefusal",
    "marked_action",
    "seal_from_host_answer",
]
