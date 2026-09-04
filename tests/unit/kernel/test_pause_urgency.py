"""Pause/UserQuestion urgency field + attention-feed ordering.

The balanced-autonomy interrupt surfaces only a genuine fork above the
routine prompts, so a needs_user pause/question carries a closed
:class:`~eawf.kernel.state.enums.Urgency` ladder and the attention feed
ranks a blocking (``URGENT``) question above the routine (``NORMAL``) ones.

These tests pin the four facets of the wave criterion:

* the closed enum members the :attr:`UserQuestion.urgency` field accepts;
* the field's back-compat default (``NORMAL`` -- routine);
* the feed ordering (a blocking question outranks a routine one); and
* the error path (an out-of-ladder urgency token fails validation).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import Urgency
from eawf.surfaces.tui.attention import AttentionKind, build_attention_feed
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import OpenPause

_SCOPE = "urn:eawf:v1:phase:demo/P01"
_SESSION = "urn:eawf:v1:session:demo/S01"
_NOW = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)


def _question(text: str, *, urgency: Urgency = Urgency.NORMAL) -> UserQuestion:
    return UserQuestion(
        question=text,
        options=[UserQuestionOption(label="apply"), UserQuestionOption(label="cancel")],
        urgency=urgency,
    )


def _pause(*, question: UserQuestion, pause_urn: str) -> OpenPause:
    return OpenPause(
        pause_urn=pause_urn,
        scope_id=_SCOPE,
        session=_SESSION,
        question=question,
        occurred_at=_NOW,
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


def test_feed_orders_blocking_question_above_routine() -> None:
    """A blocking (URGENT) question outranks a routine (NORMAL) one."""
    routine = _pause(
        question=_question("routine prompt?", urgency=Urgency.NORMAL),
        pause_urn="urn:eawf:v1:event:demo/needs-user-routine",
    )
    blocking = _pause(
        question=_question("blocking fork?", urgency=Urgency.URGENT),
        pause_urn="urn:eawf:v1:event:demo/needs-user-blocking",
    )

    # Source order puts routine first; the feed must still rank blocking above.
    feed = build_attention_feed(None, (routine, blocking))

    assert [item.kind for item in feed] == [AttentionKind.NEEDS_USER, AttentionKind.NEEDS_USER]
    assert feed[0].title == "blocking fork?"
    assert feed[0].urgency is Urgency.URGENT
    assert feed[1].title == "routine prompt?"
    assert feed[1].urgency is Urgency.NORMAL


def test_feed_question_urgency_lifts_routine_pause_row() -> None:
    """A blocking question lifts a pause whose store-row urgency is routine.

    The pause row itself defaults to ``NORMAL``; the question's own ``URGENT``
    must drive the rank so a genuine fork surfaces above routine prompts.
    """
    lifted = _pause(
        question=_question("lifted fork?", urgency=Urgency.URGENT),
        pause_urn="urn:eawf:v1:event:demo/needs-user-lifted",
    )
    assert lifted.urgency is Urgency.NORMAL  # store-row default, not blocking

    other = _pause(
        question=_question("plain prompt?", urgency=Urgency.NORMAL),
        pause_urn="urn:eawf:v1:event:demo/needs-user-plain",
    )

    feed = build_attention_feed(None, (other, lifted))

    assert feed[0].title == "lifted fork?"
    assert feed[0].urgency is Urgency.URGENT


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
