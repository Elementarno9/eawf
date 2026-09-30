"""Pause/UserQuestion urgency field.

The balanced-autonomy interrupt surfaces only a genuine fork above the
routine prompts, so a needs_user pause/question carries a closed
:class:`~eawf.kernel.state.enums.Urgency` ladder.

These tests pin:

* the closed enum members the :attr:`UserQuestion.urgency` field accepts;
* the field's back-compat default (``NORMAL`` -- routine); and
* the error path (an out-of-ladder urgency token fails validation).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import Urgency
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption


def _question(text: str, *, urgency: Urgency = Urgency.NORMAL) -> UserQuestion:
    return UserQuestion(
        question=text,
        options=[UserQuestionOption(label="apply"), UserQuestionOption(label="cancel")],
        urgency=urgency,
    )


def test_urgency_enum_is_the_closed_ladder() -> None:
    """The field accepts exactly the four closed Urgency rungs."""
    assert {u.value for u in Urgency} == {"low", "normal", "high", "urgent"}
    assert tuple(Urgency) == (Urgency.LOW, Urgency.NORMAL, Urgency.HIGH, Urgency.URGENT)


def test_user_question_urgency_defaults_to_normal() -> None:
    """A question omitting urgency ranks routine (back-compat default)."""
    question = UserQuestion(
        question="apply the migration?",
        options=[UserQuestionOption(label="yes"), UserQuestionOption(label="no")],
    )
    assert question.urgency is Urgency.NORMAL


def test_user_question_urgency_round_trips_blocking() -> None:
    """An explicit blocking urgency is carried on the schema."""
    question = _question("fork the plan?", urgency=Urgency.URGENT)
    assert question.urgency is Urgency.URGENT


@pytest.mark.parametrize("value", [Urgency.LOW, Urgency.NORMAL, Urgency.HIGH, Urgency.URGENT])
def test_user_question_accepts_every_ladder_rung(value: Urgency) -> None:
    """Every closed-ladder member is a valid urgency."""
    assert _question("ranked?", urgency=value).urgency is value


def test_user_question_rejects_out_of_ladder_urgency() -> None:
    """An urgency token outside the closed ladder fails validation."""
    with pytest.raises(ValidationError):
        UserQuestion(
            question="bad urgency?",
            options=[UserQuestionOption(label="a"), UserQuestionOption(label="b")],
            urgency="blocking",  # type: ignore[arg-type]
        )


def test_user_question_still_forbids_extra_fields() -> None:
    """Adding urgency does not relax the strict extra-forbid config."""
    with pytest.raises(ValidationError):
        UserQuestion(
            question="strict?",
            options=[UserQuestionOption(label="a"), UserQuestionOption(label="b")],
            unexpected="x",  # type: ignore[call-arg]
        )
