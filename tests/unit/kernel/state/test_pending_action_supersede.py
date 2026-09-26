"""A conflicting answer to an already-sealed action, and the trace it leaves.

Two rules held by :meth:`PendingAction.answer` are proven here. The
first: a second, conflicting answer to an already-sealed action reports
:attr:`AnswerOutcome.SUPERSEDED` and the winning choice, and is never
raised as a rejection -- whether the loser named a different option or
the same one, because the race is about who resolved the question, not
which answer they happened to pick. The second: the identical winning
answer retried is idempotent and returns the standing receipt unchanged.
Every answer, win or lose, leaves its own disposition row on the record,
keyed by principal.

Nothing here reads a clock, opens a socket, or reaches the daemon: the
model's own :meth:`PendingAction.answer` is exercised directly, against
records built in memory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.pending_action import (
    AgentPrincipal,
    AnswerOutcome,
    HumanPrincipal,
    PendingAction,
    PendingActionKind,
    PendingActionStatus,
)

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
MILESTONE: Final = f"{CONTAINER}/milestone/MLS-0030"
RUN: Final = f"{CONTAINER}/run/RUN-00000010"
FIRST_RECEIPT: Final = f"{CONTAINER}/evidence/EVD-0001"
SECOND_RECEIPT: Final = f"{CONTAINER}/evidence/EVD-0002"

AT: Final = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
LATER: Final = datetime(2026, 9, 25, 13, 0, tzinfo=UTC)
DIGEST: Final = f"sha256:{'a' * 64}"

FIRST: Final[dict[str, Any]] = {"principal_kind": "human", "principal_id": "OP-0001"}
SECOND: Final[dict[str, Any]] = {"principal_kind": "human", "principal_id": "OP-0002"}
AGENT: Final[dict[str, Any]] = {
    "principal_kind": "agent",
    "principal_id": "AG-0001",
    "run_ref": RUN,
}

#: Two answers are enough to race over; a third option would only repeat
#: the same proof against a bigger question.
OPTIONS: Final[list[dict[str, Any]]] = [
    {"option_id": "approve", "label": "Accept the Milestone", "effect": "approve"},
    {"option_id": "decline", "label": "Do not accept it", "effect": "decline"},
]


def waiting_action(**overrides: Any) -> PendingAction:
    """Return one protected approval waiting to be answered, with *overrides* applied."""
    payload: dict[str, Any] = {
        "id": "ACT-0001",
        "kind": PendingActionKind.PROTECTED_APPROVAL.value,
        "subject_ref": MILESTONE,
        "question": "Accept MLS-0030 on the bundle you just read?",
        "bundle_digest": DIGEST,
        "options": [dict(item) for item in OPTIONS],
        "idempotency_key": "req-accept-0001",
        "status": PendingActionStatus.WAITING.value,
        "requested_by": dict(AGENT),
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
    }
    payload.update(overrides)
    return PendingAction.model_validate(payload)


def resolver(principal: dict[str, Any]) -> HumanPrincipal:
    """Return *principal* as the typed resolver :meth:`PendingAction.answer` requires."""
    return HumanPrincipal.model_validate(principal)


def sealed_by_first() -> PendingAction:
    """Return the action once OP-0001's answer has sealed it."""
    return (
        waiting_action()
        .answer(
            expected_revision=1,
            resolver=resolver(FIRST),
            option_id="approve",
            receipt_ref=FIRST_RECEIPT,
            at=LATER,
        )
        .action
    )


# ---------- a fresh action carries no disposition yet ----------


def test_a_fresh_waiting_action_carries_no_disposition() -> None:
    """The empty boundary: nobody has answered, so nothing is on file yet."""
    assert waiting_action().dispositions == ()


# ---------- the winning answer seals and records its own disposition ----------


def test_the_first_answer_seals_and_records_its_own_disposition() -> None:
    """The winning answer seals the action and is recorded, not just applied."""
    result = waiting_action().answer(
        expected_revision=1,
        resolver=resolver(FIRST),
        option_id="approve",
        receipt_ref=FIRST_RECEIPT,
        at=LATER,
    )

    assert result.outcome is AnswerOutcome.SEALED
    assert result.option_id == "approve"
    assert result.resolution_actor.principal_id == "OP-0001"
    assert result.receipt_ref.entity_key == "EVD-0001"
    assert result.action.status is PendingActionStatus.SEALED
    assert [row.principal_id for row in result.action.dispositions] == ["OP-0001"]
    assert result.action.dispositions[0].outcome is AnswerOutcome.SEALED
    assert result.action.dispositions[0].option_id == "approve"


# ---------- gate-fire proof: a conflicting answer after the seal ----------


def test_a_conflicting_answer_after_the_seal_is_superseded_not_rejected() -> None:
    """A second, conflicting answer never raises: it reports the loss."""
    sealed = sealed_by_first()

    result = sealed.answer(
        expected_revision=sealed.revision,
        resolver=resolver(SECOND),
        option_id="decline",
        receipt_ref=SECOND_RECEIPT,
        at=LATER,
    )

    assert result.outcome is AnswerOutcome.SUPERSEDED
    assert result.option_id == "approve"
    assert result.resolution_actor.principal_id == "OP-0001"
    assert result.receipt_ref.entity_key == "EVD-0001"
    assert result.action.status is PendingActionStatus.SEALED
    assert result.action.selected_option_id == "approve"


def test_a_same_option_conflicting_answer_from_another_principal_is_also_superseded() -> None:
    """Superseded is about who resolved it, not which option they happened to pick."""
    sealed = sealed_by_first()

    result = sealed.answer(
        expected_revision=sealed.revision,
        resolver=resolver(SECOND),
        option_id="approve",
        receipt_ref=SECOND_RECEIPT,
        at=LATER,
    )

    assert result.outcome is AnswerOutcome.SUPERSEDED
    assert result.resolution_actor.principal_id == "OP-0001"


def test_the_superseded_answer_leaves_its_own_disposition_row() -> None:
    """The losing principal's own disposition is on file, beside the winner's seal."""
    sealed = sealed_by_first()

    result = sealed.answer(
        expected_revision=sealed.revision,
        resolver=resolver(SECOND),
        option_id="decline",
        receipt_ref=SECOND_RECEIPT,
        at=LATER,
    )

    rows = {row.principal_id: row for row in result.action.dispositions}
    assert rows["OP-0001"].outcome is AnswerOutcome.SEALED
    assert rows["OP-0002"].outcome is AnswerOutcome.SUPERSEDED
    assert rows["OP-0002"].option_id == "decline"


# ---------- idempotent: the identical winning answer retried ----------


def test_a_duplicate_identical_answer_returns_the_first_receipt() -> None:
    """The exact same answer retried changes nothing and reports the original receipt."""
    sealed = sealed_by_first()

    result = sealed.answer(
        expected_revision=sealed.revision,
        resolver=resolver(FIRST),
        option_id="approve",
        receipt_ref=SECOND_RECEIPT,
        at=LATER,
    )

    assert result.outcome is AnswerOutcome.SEALED
    assert result.receipt_ref.entity_key == "EVD-0001"
    assert result.action == sealed
    assert result.action.revision == sealed.revision


# ---------- assignee_ref is cleared once the question is resolved ----------


def test_a_resolution_clears_the_assignee() -> None:
    """Assignment transfers no authority, and any resolution clears it."""
    assigned = waiting_action(assignee_ref="OP-0001")
    assert assigned.assignee_ref == "OP-0001"

    sealed = assigned.answer(
        expected_revision=1,
        resolver=resolver(FIRST),
        option_id="approve",
        receipt_ref=FIRST_RECEIPT,
        at=LATER,
    ).action

    assert sealed.assignee_ref is None


# ---------- boundary and error paths ----------


def test_answer_refuses_an_agent_resolver() -> None:
    """The resolver slot is a person's, win or lose, exactly as :meth:`seal` requires."""
    with pytest.raises(ValueError, match="answered by a person"):
        waiting_action().answer(
            expected_revision=1,
            resolver=AgentPrincipal.model_validate(AGENT),  # type: ignore[arg-type]
            option_id="approve",
            receipt_ref=FIRST_RECEIPT,
            at=LATER,
        )


def test_answering_an_action_nobody_was_shown_is_refused() -> None:
    """A ``CREATED`` action has never been asked, so it cannot be answered."""
    created = waiting_action(status=PendingActionStatus.CREATED.value)
    with pytest.raises(ValueError, match="can be answered"):
        created.answer(
            expected_revision=1,
            resolver=resolver(FIRST),
            option_id="approve",
            receipt_ref=FIRST_RECEIPT,
            at=LATER,
        )


def test_answering_against_a_stale_revision_is_refused_while_still_waiting() -> None:
    """A still-waiting action answered against a revision it is not at is refused."""
    waiting = waiting_action(revision=2)
    with pytest.raises(ValueError, match="not the 1 the"):
        waiting.answer(
            expected_revision=1,
            resolver=resolver(FIRST),
            option_id="approve",
            receipt_ref=FIRST_RECEIPT,
            at=LATER,
        )


def test_answering_with_an_option_the_question_does_not_offer_is_refused() -> None:
    """An answer outside the offered set is refused before anything else is checked."""
    with pytest.raises(ValueError, match="offers"):
        waiting_action().answer(
            expected_revision=1,
            resolver=resolver(FIRST),
            option_id="abstain",
            receipt_ref=FIRST_RECEIPT,
            at=LATER,
        )


def test_answering_an_already_sealed_action_with_an_unoffered_option_is_refused() -> None:
    """The offered-option check applies to the losing path too."""
    sealed = sealed_by_first()
    with pytest.raises(ValueError, match="offers"):
        sealed.answer(
            expected_revision=sealed.revision,
            resolver=resolver(SECOND),
            option_id="abstain",
            receipt_ref=SECOND_RECEIPT,
            at=LATER,
        )


def test_a_row_with_two_dispositions_for_one_principal_is_refused() -> None:
    """One principal, one current disposition: a repeated key is a schema defect."""
    payload = sealed_by_first().model_dump()
    payload["dispositions"] = [
        {"principal_id": "OP-0001", "outcome": "sealed", "option_id": "approve"},
        {"principal_id": "OP-0001", "outcome": "sealed", "option_id": "approve"},
    ]
    with pytest.raises(ValidationError, match="more than one row for the same principal"):
        PendingAction.model_validate(payload)
