"""PLAN-045 and PLAN-046: what an OpenQuestion projects to, and a reply in the operator's words.

PLAN-045: one case per row of the ten-situation projection table, including
the two-principal ``answered elsewhere`` case; defaulted never projects as
answered and ``expired`` is not a situation. PLAN-046: the zero-option
reply, the decline-then-reply path, the 2,000 and 2,001 length boundary, a
second reply refused with the existing answer, and the pending-action
refusal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import OpenQuestionDropReason, OpenQuestionStatus
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.kernel.state.epoch2.question import (
    ENDS_WHEN,
    MAX_REPLY_LENGTH,
    POLICY_ACTOR,
    OpenQuestion,
    QuestionRefusedError,
    QuestionReply,
    QuestionSituation,
    answer_with_reply,
    project_question,
)

pytestmark = pytest.mark.unit

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
PROJECT: Final = "eawf://WSP-MAIN/PRJ-EAWF/_"
QUESTION: Final = f"{PROJECT}/question/QST-0001"
SUCCESSOR: Final = f"{PROJECT}/question/QST-0002"
CAMPAIGN: Final = f"{CONTAINER}/campaign/CAM-0001"
CLAIM: Final = f"{PROJECT}/claim/CLM-0001"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
YOU: Final = "OP-0001"
THEM: Final = "OP-0002"

OPTIONS: Final[list[dict[str, Any]]] = [
    {"key": "keep", "label": "Keep the lease", "recommended": True},
    {"key": "release", "label": "Release the lease"},
]


def question(status: str = "OPEN", **overrides: Any) -> OpenQuestion:
    """Return a question in *status*, overridden field by field."""
    fields: dict[str, Any] = {
        "uid": "3b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "key": "QST-0001",
        "urn": QUESTION,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": AT,
        "updated_at": AT,
        "scope_ref": CAMPAIGN,
        "question": "Keep the lease while the provider recovers?",
        "options": OPTIONS,
        "status": OpenQuestionStatus(status.lower()),
    }
    if status in ("AUTO_RESOLVED", "SEALED"):
        fields |= {
            "default_key": "keep",
            "override_until": AT + timedelta(hours=1),
            "resolution_actor": POLICY_ACTOR,
            "chosen_option_key": "keep",
            "resolved_at": AT,
        }
    if status in ("ANSWERED", "DROPPED"):
        fields |= {"resolution_actor": THEM, "resolved_at": AT}
    if status == "ANSWERED":
        fields["chosen_option_key"] = "release"
    if status == "DROPPED":
        fields["drop_reason"] = "moot"
    fields.update(overrides)
    return OpenQuestion.model_validate(fields)


def reply(text: str = "Keep it for an hour, then release.", **overrides: Any) -> QuestionReply:
    fields: dict[str, Any] = {
        "question_ref": QUESTION,
        "text": text,
        "principal": YOU,
        "submitted_at": AT + timedelta(minutes=5),
    }
    fields.update(overrides)
    return QuestionReply.model_validate(fields)


def situation(q: OpenQuestion, *, unreachable: bool = False) -> QuestionSituation:
    return project_question(q, principal=YOU, asking_run_unreachable=unreachable).situation


# ---- PLAN-045: one case per row of the projection table -------------------------


def test_plan_045_open_nonblocking() -> None:
    assert situation(question()) is QuestionSituation.OPEN


def test_plan_045_blocked() -> None:
    assert situation(question("BLOCKED", blocking=True)) is QuestionSituation.OPEN_BLOCKING


def test_plan_045_blocked_after_the_urgency_self_edge() -> None:
    q = question("BLOCKED", blocking=True, escalated_by_deadline=AT)
    assert situation(q) is QuestionSituation.OPEN_ESCALATED


def test_plan_045_answered_by_you() -> None:
    q = question("ANSWERED", resolution_actor=YOU)
    p = project_question(q, principal=YOU, asking_run_unreachable=False)
    assert p.situation is QuestionSituation.ANSWERED_BY_YOU
    assert p.chosen_option_key == "release"


def test_plan_045_answered_elsewhere_names_the_winner_and_supersedes_your_late_answer() -> None:
    late = {"principal": YOU, "chosen_option_key": "keep", "submitted_at": AT + timedelta(1)}
    q = question("ANSWERED", late_answers=[late])
    p = project_question(q, principal=YOU, asking_run_unreachable=False)
    assert p.situation is QuestionSituation.ANSWERED_ELSEWHERE
    assert (p.resolution_actor, p.chosen_option_key) == (THEM, "release")
    assert p.own_late_answer_superseded


def test_plan_045_answered_elsewhere_without_a_late_answer() -> None:
    p = project_question(question("ANSWERED"), principal=YOU, asking_run_unreachable=False)
    assert not p.own_late_answer_superseded


def test_plan_045_auto_resolved_is_defaulted_never_answered() -> None:
    p = project_question(question("AUTO_RESOLVED"), principal=YOU, asking_run_unreachable=False)
    assert p.situation is QuestionSituation.DEFAULTED_OVERRIDE_OPEN
    assert (p.resolution_actor, p.chosen_option_key) == (POLICY_ACTOR, "keep")


def test_plan_045_sealed_is_defaulted_never_answered() -> None:
    assert situation(question("SEALED")) is QuestionSituation.DEFAULTED_SEALED


@pytest.mark.parametrize("reason", ["moot", "out_of_scope"])
def test_plan_045_withdrawn_keeps_its_actor_and_its_own_drop_word(reason: str) -> None:
    p = project_question(
        question("DROPPED", drop_reason=reason), principal=YOU, asking_run_unreachable=False
    )
    assert p.situation is QuestionSituation.WITHDRAWN
    assert (p.resolution_actor, p.drop_reason) == (THEM, OpenQuestionDropReason(reason))


def test_plan_045_replaced_names_its_successor() -> None:
    q = question("DROPPED", drop_reason="superseded", superseded_by_question_ref=SUCCESSOR)
    p = project_question(q, principal=YOU, asking_run_unreachable=False)
    assert p.situation is QuestionSituation.REPLACED
    assert p.successor_ref == SUCCESSOR


@pytest.mark.parametrize("status", ["OPEN", "BLOCKED"])
def test_plan_045_unanswerable_is_derived_from_the_asking_run(status: str) -> None:
    q = question(status, blocking=status == "BLOCKED")
    assert situation(q, unreachable=True) is QuestionSituation.UNANSWERABLE
    assert situation(q) is not QuestionSituation.UNANSWERABLE


def test_plan_045_an_unreachable_run_does_not_touch_a_resolved_question() -> None:
    assert situation(question("SEALED"), unreachable=True) is QuestionSituation.DEFAULTED_SEALED


def test_plan_045_the_ten_situations_are_distinct_and_name_their_ender() -> None:
    assert len(QuestionSituation) == 10
    assert set(ENDS_WHEN) == set(QuestionSituation)
    assert all(ENDS_WHEN.values())


def test_plan_045_no_defaulted_situation_says_answered_and_none_expires() -> None:
    defaulted = (QuestionSituation.DEFAULTED_OVERRIDE_OPEN, QuestionSituation.DEFAULTED_SEALED)
    assert not any("answered" in s.value for s in defaulted)
    assert not any("expired" in s.value for s in QuestionSituation)


def test_plan_045_the_projection_is_not_stored_on_the_record() -> None:
    with pytest.raises(ValidationError):
        question(situation="open")


# ---- the persisted fields the projection reads ----------------------------------


@pytest.mark.parametrize(
    ("status", "overrides"),
    [
        ("OPEN", {"resolution_actor": YOU}),
        ("ANSWERED", {"resolution_actor": POLICY_ACTOR}),
        ("AUTO_RESOLVED", {"resolution_actor": YOU}),
        ("AUTO_RESOLVED", {"chosen_option_key": "release"}),
        ("SEALED", {"override_until": None}),
        ("BLOCKED", {"blocking": True, "default_key": "keep"}),
        ("OPEN", {"escalated_by_deadline": AT}),
        ("DROPPED", {"drop_reason": None}),
        ("DROPPED", {"drop_reason": "superseded"}),
        ("DROPPED", {"superseded_by_question_ref": SUCCESSOR}),
        ("DROPPED", {"drop_reason": "superseded", "superseded_by_question_ref": QUESTION}),
        ("OPEN", {"options": OPTIONS[:1]}),
        ("OPEN", {"options": [OPTIONS[0], {**OPTIONS[1], "recommended": True}]}),
        ("OPEN", {"options": [OPTIONS[0], OPTIONS[0]]}),
        ("OPEN", {"options": [*OPTIONS, *[{"key": f"o{i}", "label": "x"} for i in range(3)]]}),
        ("ANSWERED", {"chosen_option_key": None}),
        ("ANSWERED", {"chosen_option_key": "nope"}),
        ("ANSWERED", {"answer_refs": [CLAIM], "resolved_at": None}),
    ],
)
def test_plan_045_a_field_its_state_does_not_admit_is_refused(
    status: str, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        question(status, **overrides)


def test_plan_045_an_answer_may_rest_on_claim_evidence() -> None:
    q = question("ANSWERED", chosen_option_key=None, answer_refs=[CLAIM])
    assert q.status is OpenQuestionStatus.ANSWERED


# ---- PLAN-046: a reply in the operator's own words ------------------------------


def test_plan_046_zero_option_question_is_answered_by_reply() -> None:
    answered = answer_with_reply(question(options=[]), reply())
    assert answered.status is OpenQuestionStatus.ANSWERED
    assert answered.reply is not None
    assert answered.reply.text == "Keep it for an hour, then release."
    assert answered.resolution_actor == YOU
    assert answered.chosen_option_key is None
    assert situation(answered) is QuestionSituation.ANSWERED_BY_YOU


def test_plan_046_decline_every_option_then_reply() -> None:
    answered = answer_with_reply(question(), reply())
    assert answered.chosen_option_key is None
    assert answered.option_keys == ("keep", "release")


def test_plan_046_override_of_a_default_by_reply_keeps_the_default() -> None:
    answered = answer_with_reply(question("AUTO_RESOLVED"), reply())
    assert answered.status is OpenQuestionStatus.ANSWERED
    assert answered.default_key == "keep"


def test_plan_046_reply_after_the_override_window_is_refused() -> None:
    late = reply(submitted_at=AT + timedelta(hours=2))
    with pytest.raises(QuestionRefusedError) as caught:
        answer_with_reply(question("AUTO_RESOLVED"), late)
    assert caught.value.code == "override_window_closed"


def test_plan_046_reply_at_the_length_bound_validates() -> None:
    assert len(reply("x" * MAX_REPLY_LENGTH).text) == 2000


@pytest.mark.parametrize("text", ["", "x" * (MAX_REPLY_LENGTH + 1)])
def test_plan_046_empty_or_over_long_reply_fails_before_persistence(text: str) -> None:
    with pytest.raises(ValidationError):
        reply(text)


def test_plan_046_a_reply_is_immutable_once_submitted() -> None:
    sent = reply()
    with pytest.raises(ValidationError):
        sent.text = "changed my mind"


def test_plan_046_a_second_reply_is_refused_with_the_existing_answer() -> None:
    answered = answer_with_reply(question(options=[]), reply())
    with pytest.raises(QuestionRefusedError) as caught:
        answer_with_reply(answered, reply("second thoughts", principal=THEM))
    assert caught.value.code == "question_already_resolved"
    assert "Keep it for an hour, then release." in str(caught.value)


def test_plan_046_a_reply_to_a_replaced_question_names_the_successor() -> None:
    q = question("DROPPED", drop_reason="superseded", superseded_by_question_ref=SUCCESSOR)
    with pytest.raises(QuestionRefusedError) as caught:
        answer_with_reply(q, reply())
    assert caught.value.code == "question_kind_mismatch"
    assert SUCCESSOR in caught.value.remediation


def test_plan_046_a_reply_naming_another_question_is_refused() -> None:
    with pytest.raises(QuestionRefusedError) as caught:
        answer_with_reply(question(), reply(question_ref=SUCCESSOR))
    assert caught.value.code == "question_ref_mismatch"


def test_plan_046_a_reply_to_a_pending_action_is_refused_naming_its_options() -> None:
    action = PendingAction.model_validate(
        {
            "id": "ACT-0001",
            "kind": "operator_decision",
            "subject_ref": f"{CONTAINER}/milestone/MLS-0030",
            "question": "Which lease path?",
            "options": [
                {"option_id": "keep", "label": "Keep", "effect": "approve"},
                {"option_id": "drop", "label": "Drop", "effect": "decline"},
            ],
            "idempotency_key": "req-lease-0001",
            "status": "WAITING",
            "requested_by": {
                "principal_kind": "agent",
                "principal_id": "AG-0001",
                "run_ref": f"{CONTAINER}/run/RUN-00000010",
            },
            "created_at": AT.isoformat(),
            "updated_at": AT.isoformat(),
        }
    )
    with pytest.raises(QuestionRefusedError) as caught:
        answer_with_reply(action, reply())
    assert caught.value.code == "question_kind_mismatch"
    assert caught.value.remediation == "answer with one of: keep, drop"


def test_plan_046_the_reply_belongs_to_its_resolver() -> None:
    with pytest.raises(ValidationError, match="principal who resolved"):
        question("ANSWERED", chosen_option_key=None, reply=reply().model_dump())


def test_plan_046_a_reply_cannot_ride_beside_a_chosen_option() -> None:
    with pytest.raises(ValidationError, match="chooses none"):
        question("ANSWERED", resolution_actor=YOU, reply=reply().model_dump())
