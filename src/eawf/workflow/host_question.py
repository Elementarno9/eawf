"""Presenting a durable pending action as the host harness's multiple-choice question.

An operator choice reaches the host as a typed question bound one-to-one to
the :class:`~eawf.kernel.state.epoch2.pending_action.PendingAction` that
records it. :func:`present_pending_action` is the only way a pending action
becomes a host question, and it refuses rather than softens: a question
that cannot be answered without first asking what it means is not shown.

What the presentation may not change. The options are the persisted
options, in the persisted order, under the persisted labels; the host
cannot add, reorder or relabel one. Each option carries the persisted id
the seal verb accepts, so an answer that is not one of those ids -- a
paraphrase, a free-text "yes" -- has nothing to bind to and is not consent.

What a presentable question carries. Every option states its consequence
in plain words and shows a concrete rendering of what choosing it
produces; the options differ in what they produce, since a question whose
answers all lead to the same place costs attention and changes nothing.
Exactly one option is recommended, for a reason stated in one sentence.
Every identifier, abbreviation or screaming-case key in the view is
expanded in the same view. Sentences are short.

When it may be shown. Only a question that is filed and waiting: the
record is durable before any surface sees it, so losing the surface loses
nothing, and presenting the same waiting record again shows the same
question. A timeout default is refused, because no override window is
filed with the record and the surface could not show one.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from eawf.kernel.state.epoch2.pending_action import PendingAction, PendingActionStatus
from eawf.kernel.state.epoch2.urns import render_qualified_urn
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption

#: The longest sentence a question, label, consequence or rationale may
#: hold. Past this a reader re-reads the sentence to find its verb.
MAX_SENTENCE_WORDS: Final = 25

#: What counts as a term that needs expanding: a hyphenated or underscored
#: capitalised key (``MLS-0030``, ``ACCEPTANCE_REVIEW``), an abbreviation of
#: two or more capitals (``CI``), or a capital followed by digits (``P31``).
_TERM: Final = re.compile(
    r"\b(?:[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+|[A-Z]{2,}[0-9]*|[A-Z][0-9]{2,})\b"
)

#: Where one sentence ends and the next begins.
_SENTENCE_END: Final = re.compile(r"(?<=[.!?])\s+")


class PresentationRefusal(StrEnum):
    """Why a pending action was not presented."""

    NOT_WAITING = "question_not_waiting"
    TIMEOUT_UNSHOWN = "timeout_default_unshown"
    OPTION_UNSHOWN = "option_not_shown"
    TERM_UNEXPANDED = "term_unexpanded"
    PROSE_NOT_PLAIN = "prose_not_plain"
    RECOMMENDATION_UNSTATED = "recommendation_unstated"
    NO_CONSEQUENCE = "question_without_consequence"


class QuestionPresentationError(ValueError):
    """A pending action cannot be shown to an operator as it stands.

    Attributes:
        code: The stable refusal a caller branches on.
    """

    def __init__(self, code: PresentationRefusal, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


def _sentences(text: str) -> list[str]:
    """Return the sentences of *text*."""
    return [part for part in _SENTENCE_END.split(text.strip()) if part]


def _require_answerable(action: PendingAction) -> None:
    """Refuse a record that is not filed and waiting, or that could lapse into an answer.

    Raises:
        QuestionPresentationError: ``question_not_waiting`` or
            ``timeout_default_unshown``.
    """
    if action.status is not PendingActionStatus.WAITING:
        raise QuestionPresentationError(
            PresentationRefusal.NOT_WAITING,
            f"{action.id} is {action.status.value}; only a filed "
            f"{PendingActionStatus.WAITING.value} question is shown",
        )
    if action.default_on_timeout is not None:
        raise QuestionPresentationError(
            PresentationRefusal.TIMEOUT_UNSHOWN,
            f"{action.id} would default to {action.default_on_timeout!r} but files no override "
            "window, so the surface cannot show when the default can still be changed",
        )


def _require_shown_options(action: PendingAction) -> None:
    """Refuse an option told only in prose, and options that all lead to the same place.

    Raises:
        QuestionPresentationError: ``option_not_shown`` or
            ``question_without_consequence``.
    """
    for option in action.options:
        missing = [name for name in ("consequence", "preview") if getattr(option, name) is None]
        if missing:
            raise QuestionPresentationError(
                PresentationRefusal.OPTION_UNSHOWN,
                f"{action.id} option {option.option_id!r} has no {' or '.join(missing)}",
            )
    for name in ("consequence", "preview"):
        values = [getattr(option, name) for option in action.options]
        if len(set(values)) != len(values):
            raise QuestionPresentationError(
                PresentationRefusal.NO_CONSEQUENCE,
                f"two options of {action.id} share one {name}, so the answer would not change "
                "what happens",
            )


def _require_recommendation(action: PendingAction) -> str:
    """Return the one-sentence rationale of the one recommended option.

    Raises:
        QuestionPresentationError: ``recommendation_unstated``.
    """
    rationale = action.recommendation_rationale
    if action.recommended_option_id is None or rationale is None:
        raise QuestionPresentationError(
            PresentationRefusal.RECOMMENDATION_UNSTATED,
            f"{action.id} recommends no option",
        )
    if len(_sentences(rationale)) != 1:
        raise QuestionPresentationError(
            PresentationRefusal.RECOMMENDATION_UNSTATED,
            f"{action.id} gives its recommendation in more than one sentence",
        )
    return rationale


def _require_plain_prose(action: PendingAction, rationale: str) -> None:
    """Refuse a sentence too long to read once.

    Raises:
        QuestionPresentationError: ``prose_not_plain``.
    """
    texts = [action.question, rationale]
    for option in action.options:
        texts.append(option.label)
        texts.append(option.consequence or "")
    for text in texts:
        for sentence in _sentences(text):
            if len(sentence.split()) > MAX_SENTENCE_WORDS:
                raise QuestionPresentationError(
                    PresentationRefusal.PROSE_NOT_PLAIN,
                    f"{action.id} has a sentence of {len(sentence.split())} words, over "
                    f"{MAX_SENTENCE_WORDS}: {sentence!r}",
                )


def _require_expanded_terms(action: PendingAction, rationale: str) -> None:
    """Refuse a view that shows a term without its expansion.

    Raises:
        QuestionPresentationError: ``term_unexpanded``.
    """
    view = [action.question, rationale]
    for option in action.options:
        view.extend((option.label, option.consequence or "", option.preview or ""))
    expanded = {item.term for item in action.terms}
    unexpanded = sorted({term for text in view for term in _TERM.findall(text)} - expanded)
    if unexpanded:
        raise QuestionPresentationError(
            PresentationRefusal.TERM_UNEXPANDED,
            f"{action.id} shows {', '.join(unexpanded)} without saying what it stands for",
        )


def present_pending_action(action: PendingAction) -> UserQuestion:
    """Return the host question that presents *action*, bound to it.

    Args:
        action: The pending action as the tree holds it.

    Returns:
        The multiple-choice question: the persisted options in their order
        and under their labels, each with its consequence, its rendering
        and its option id, the recommendation stated on the recommended
        option, and the term expansions shown with the question. Bound to
        the action's URN and the revision it was read at.

    Raises:
        QuestionPresentationError: The action is not a filed waiting
            question, carries a timeout default it cannot show, or is not
            presentable as it stands -- an option without a consequence or
            rendering, options that do not differ, no single one-sentence
            recommendation, a sentence too long, or an unexpanded term.
    """
    _require_answerable(action)
    _require_shown_options(action)
    rationale = _require_recommendation(action)
    _require_plain_prose(action, rationale)
    _require_expanded_terms(action, rationale)
    question = action.question
    if action.terms:
        glossary = "; ".join(f"{item.term} is {item.expansion}" for item in action.terms)
        question = f"{question}\n\nTerms: {glossary}."
    options = [
        UserQuestionOption(
            label=option.label,
            description=(
                f"Recommended. {rationale} {option.consequence}"
                if option.option_id == action.recommended_option_id
                else option.consequence
            ),
            preview=option.preview,
            option_id=option.option_id,
        )
        for option in action.options
    ]
    return UserQuestion(
        question=question,
        options=options,
        action_ref=render_qualified_urn(action.urn),
        action_revision=action.revision,
    )


__all__ = [
    "MAX_SENTENCE_WORDS",
    "PresentationRefusal",
    "QuestionPresentationError",
    "present_pending_action",
]
