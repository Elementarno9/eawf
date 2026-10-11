"""Answering a converged Campaign's questions from its synthesis.

A Campaign files each of its questions as an open question scoped to it. When it
converges on its own -- every step done, the synthesis among them -- what it learned
is the answer to each, so the close answers every question still open with a reply
in the name of the principal who closed it, citing the synthesis artifact and the
artifacts of each step that worked that question, each with its digest. A Campaign
that is cancelled, or that a budget stop starved, answers nothing: its questions
stay open for another Campaign or an operator.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime

from eawf.kernel.projection.campaign import stored_revisions
from eawf.kernel.state.enums import OpenQuestionStatus
from eawf.kernel.state.epoch2.artifact_revision import StoredArtifactRevision
from eawf.kernel.state.epoch2.campaign import Campaign, CampaignMethod, StepState
from eawf.kernel.state.epoch2.question import OpenQuestion, QuestionReply
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_row_write

logger = logging.getLogger(__name__)


def _cited(refs: tuple[object, ...], artifacts: Mapping[str, StoredArtifactRevision]) -> list[str]:
    """Return each artifact revision among *refs* as ``<ref> (<digest>)``."""
    return [
        f"{ref} ({artifacts[ref].revision.digest})"
        for ref in refs
        if isinstance(ref, str) and ref in artifacts
    ]


def answered_questions(
    campaign: Campaign,
    questions: Mapping[str, OpenQuestion],
    artifacts: Mapping[str, StoredArtifactRevision],
    *,
    actor: str,
    now: datetime,
) -> tuple[OpenQuestion, ...]:
    """Return *campaign*'s open questions answered from its synthesis.

    Args:
        campaign: The Campaign converging.
        questions: Every question the document holds, by key.
        artifacts: Every stored artifact revision, by reference.
        actor: The principal closing the Campaign, who the reply is given by.
        now: When it converged.

    Returns:
        The answered questions; none when no synthesis step is done with an
        artifact to cite.
    """
    synthesis = next(
        (
            step
            for step in campaign.plan_steps
            if step.method is CampaignMethod.SYNTHESIS and step.state is StepState.DONE
        ),
        None,
    )
    if synthesis is None:
        return ()
    cited = _cited(synthesis.produced, artifacts)
    if not cited:
        return ()
    answered: list[OpenQuestion] = []
    for question in questions.values():
        if question.scope_ref != campaign.urn or question.status is not OpenQuestionStatus.OPEN:
            continue
        steps = [
            f"Step {step.ordinal}: {ref}"
            for step in campaign.plan_steps
            if step is not synthesis and step.question_ref == question.urn
            for ref in _cited(step.produced, artifacts)
        ]
        text = "\n".join(
            [
                f"Answered when {campaign.key} converged: {synthesis.outcome}",
                f"Synthesis: {cited[-1]}",
                *steps,
            ]
        )
        reply = QuestionReply(
            question_ref=question.urn, text=text, principal=actor, submitted_at=now
        )
        answered.append(
            question.model_copy(
                update={
                    "status": OpenQuestionStatus.ANSWERED,
                    "resolution_actor": actor,
                    "reply": reply,
                    "resolved_at": now,
                    "revision": question.revision + 1,
                    "updated_at": now,
                }
            )
        )
    return tuple(answered)


def answer_converged_questions(
    session: RootSession, campaign: Campaign, *, actor: str, now: datetime
) -> tuple[Envelope, ...]:
    """Answer *campaign*'s open questions from its synthesis, inside *session*.

    Args:
        session: The session the Campaign is being closed under.
        campaign: The Campaign converging on its own.
        actor: The principal closing it.
        now: When it converged.

    Returns:
        One firehose row per answered question.

    Raises:
        TransactionRefusedError: An answer carries a leak shape; nothing more is written.
    """
    rows = document_rows(session.read_document(), Epoch2Collection.OPEN_QUESTION)
    # a row the cutover imported from epoch 1 carries no urn and is no native question
    questions = {key: OpenQuestion.model_validate(row) for key, row in rows.items() if "urn" in row}
    lines = effective_records(read_ledger_records(session.ledger_path(Epoch2Collection.ARTIFACT)))
    artifacts = stored_revisions(line.payload for line in lines)
    answered = answered_questions(campaign, questions, artifacts, actor=actor, now=now)
    envelopes = tuple(
        commit_row_write(
            session,
            collection=Epoch2Collection.OPEN_QUESTION,
            record_key=question.key,
            row=question.model_dump(mode="json"),
            event_name="campaign.question.answered",
            event_fields={
                "entity_ref": str(question.urn),
                "to_status": question.status.value,
                "revision_after": question.revision,
                "actor": actor,
            },
            compaction=None,
            now=now,
        )
        for question in answered
    )
    logger.info(f"answer_converged_questions campaign={campaign.key} answered={len(answered)}")
    return envelopes


__all__ = ["answer_converged_questions", "answered_questions"]
