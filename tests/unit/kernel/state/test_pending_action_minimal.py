"""The minimal PendingAction record, and the two rules it makes unbreakable.

The suite is built around one honest question: can a payload that says an
agent approved something be persisted? Every test here answers it from a
different direction -- the agent discriminator, an agent wearing the human
discriminator, the sealing method, and a hand-built row -- because a rule
that only holds on the path somebody remembered to check is not a rule.

The second rule is the timeout default. A protected approval that lapses
into a yes is the approval nobody gave, so the field is refused for that
kind and admitted for the operator's routing question, which is what
shows the check has teeth rather than being always-on.

Nothing here reads a clock, opens a socket, or touches the filesystem.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.pending_action import (
    MAX_OPTIONS,
    MIN_OPTIONS,
    PENDING_ACTION_EDGES,
    AgentPrincipal,
    HumanPrincipal,
    OptionEffect,
    PendingAction,
    PendingActionKind,
    PendingActionStatus,
)

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
MILESTONE: Final = f"{CONTAINER}/milestone/MLS-0030"
RUN: Final = f"{CONTAINER}/run/RUN-00000010"
RECEIPT: Final = f"{CONTAINER}/evidence/EVD-0001"

AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LATER: Final = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)
DIGEST: Final = f"sha256:{'a' * 64}"

OPERATOR: Final[dict[str, Any]] = {"principal_kind": "human", "principal_id": "OP-0001"}
AGENT: Final[dict[str, Any]] = {
    "principal_kind": "agent",
    "principal_id": "AG-0001",
    "run_ref": RUN,
}

#: The three answers a protected approval offers, which is also the
#: minimum that makes approve, decline and repair separately expressible.
OPTIONS: Final[list[dict[str, Any]]] = [
    {"option_id": "approve", "label": "Accept the Milestone", "effect": "approve"},
    {"option_id": "decline", "label": "Do not accept it", "effect": "decline"},
    {"option_id": "repair", "label": "Ask for changes", "effect": "request_repair"},
]


def row(**overrides: Any) -> dict[str, Any]:
    """Return one pending-action payload, with *overrides* applied."""
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
    return payload


def sealed_row(**overrides: Any) -> dict[str, Any]:
    """Return one payload sealed by the operator, with *overrides* applied."""
    sealed: dict[str, Any] = {
        "status": PendingActionStatus.SEALED.value,
        "resolution_actor": dict(OPERATOR),
        "selected_option_id": "approve",
        "receipt_ref": RECEIPT,
        "updated_at": LATER.isoformat(),
    }
    sealed.update(overrides)
    return row(**sealed)


# ---------- the key, the kind and the shape ----------


def test_pending_action_accepts_the_canonical_act_key() -> None:
    """The record is keyed ``ACT-####`` and nothing else."""
    assert PendingAction.model_validate(row()).id == "ACT-0001"


@pytest.mark.parametrize("key", ["ACT-1", "ACT-00001", "act-0001", "TSK-0001", "ACT-0001 "])
def test_pending_action_refuses_a_key_outside_the_act_grammar(key: str) -> None:
    """A near-miss key is a defect, not a spelling to normalise."""
    with pytest.raises(ValidationError):
        PendingAction.model_validate(row(id=key))


def test_pending_action_refuses_an_unknown_key() -> None:
    """The record is strict: a misspelled field is a rejection, not a silent drop."""
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        PendingAction.model_validate(row(resolution_principal=dict(OPERATOR)))


def test_pending_action_is_frozen() -> None:
    """A sealed answer edited in place would be a forged answer."""
    action = PendingAction.model_validate(sealed_row())
    with pytest.raises(ValidationError):
        action.status = PendingActionStatus.WAITING  # type: ignore[misc]


def test_pending_action_refuses_a_non_string_idempotency_key() -> None:
    """Strict scalars: the integer one does not become the string ``"1"``."""
    with pytest.raises(ValidationError):
        PendingAction.model_validate(row(idempotency_key=1))


def test_pending_action_refuses_a_missing_idempotency_key() -> None:
    """The client's name for the request is required, never defaulted."""
    payload = row()
    del payload["idempotency_key"]
    with pytest.raises(ValidationError, match="idempotency_key"):
        PendingAction.model_validate(payload)


# ---------- two to four options ----------


#: A fourth answer, so the upper bound is reachable: declining has more
#: than one shape, and only the approving answer is unique.
DEFER: Final[dict[str, Any]] = {
    "option_id": "defer",
    "label": "Decline and decide later",
    "effect": "decline",
}


@pytest.mark.parametrize("count", [MIN_OPTIONS, 3, MAX_OPTIONS])
def test_pending_action_admits_two_to_four_options(count: int) -> None:
    """Both bounds and the interior are admitted."""
    offered = [dict(item) for item in (*OPTIONS, DEFER)][:count]
    action = PendingAction.model_validate(row(options=offered))
    assert len(action.options) == count


@pytest.mark.parametrize("count", [0, 1])
def test_pending_action_refuses_fewer_than_two_options(count: int) -> None:
    """One option is not a question, and none is not an offer."""
    with pytest.raises(ValidationError, match=r"at least 2|too_short"):
        PendingAction.model_validate(row(options=[dict(item) for item in OPTIONS[:count]]))


def test_pending_action_refuses_more_than_four_options() -> None:
    """Past four a surface summarises, and an operator answers the paraphrase."""
    offered = [dict(item) for item in (*OPTIONS, DEFER)]
    offered.append({"option_id": "extra", "label": "One answer too many", "effect": "decline"})
    assert len(offered) == MAX_OPTIONS + 1
    with pytest.raises(ValidationError, match=r"at most 4|too_long"):
        PendingAction.model_validate(row(options=offered))


def test_pending_action_refuses_a_repeated_option_id() -> None:
    """Two answers with one id make the chosen answer ambiguous."""
    offered = [dict(OPTIONS[0]), {**OPTIONS[1], "option_id": "approve"}]
    with pytest.raises(ValidationError, match="the same option id twice"):
        PendingAction.model_validate(row(options=offered))


def test_pending_action_refuses_two_approving_answers() -> None:
    """Two ways to say yes make "was this approved" depend on which was read."""
    offered = [dict(OPTIONS[0]), {**OPTIONS[1], "effect": "approve"}]
    with pytest.raises(ValidationError, match="as approving answers"):
        PendingAction.model_validate(row(options=offered))


def test_pending_action_admits_two_declining_answers() -> None:
    """The approving answer is unique; declining has more than one shape."""
    offered = [dict(OPTIONS[0]), dict(OPTIONS[1]), dict(DEFER)]
    action = PendingAction.model_validate(row(options=offered))
    declining = [item.option_id for item in action.options if item.effect is OptionEffect.DECLINE]
    assert declining == ["decline", "defer"]


# ---------- a protected approval admits no timeout default ----------


def test_protected_approval_refuses_a_timeout_default() -> None:
    """An approval that lapses into a yes is the approval nobody gave."""
    with pytest.raises(ValidationError, match="cannot carry default_on_timeout"):
        PendingAction.model_validate(row(default_on_timeout="approve"))


def test_the_timeout_refusal_has_teeth_because_another_kind_admits_one() -> None:
    """The guard can pass: an operator's routing question may default."""
    action = PendingAction.model_validate(
        row(
            kind=PendingActionKind.OPERATOR_DECISION.value,
            bundle_digest=None,
            default_on_timeout="decline",
        )
    )
    assert action.default_on_timeout == "decline"


def test_a_timeout_default_must_name_an_offered_option() -> None:
    """Defaulting to an answer the question never offered answers nothing."""
    with pytest.raises(ValidationError, match="is not one of"):
        PendingAction.model_validate(
            row(
                kind=PendingActionKind.OPERATOR_DECISION.value,
                bundle_digest=None,
                default_on_timeout="abstain",
            )
        )


def test_a_protected_approval_requires_the_digest_it_approves() -> None:
    """A yes bound to nothing is a yes to whatever the tree says next."""
    with pytest.raises(ValidationError, match="requires the bundle_digest"):
        PendingAction.model_validate(row(bundle_digest=None))


# ---------- an agent cannot be the resolver ----------


def test_the_resolver_slot_refuses_an_agent_principal() -> None:
    """The agent discriminator is not a shape the resolver slot can hold."""
    with pytest.raises(ValidationError) as caught:
        PendingAction.model_validate(sealed_row(resolution_actor=dict(AGENT)))
    assert "resolution_actor" in str(caught.value)


def test_the_resolver_slot_refuses_an_agent_wearing_the_human_discriminator() -> None:
    """Relabelling does not help: the human shape forbids the Run an agent carries."""
    disguised = {"principal_kind": "human", "principal_id": "AG-0001", "run_ref": RUN}
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        PendingAction.model_validate(sealed_row(resolution_actor=disguised))


def test_the_agent_refusal_has_teeth_because_an_operator_seals_fine() -> None:
    """The guard can pass: the same row with a person in the slot validates."""
    action = PendingAction.model_validate(sealed_row())
    assert action.resolution_actor == HumanPrincipal.model_validate(OPERATOR)


def test_an_agent_may_ask_the_question_it_may_not_answer() -> None:
    """Asking and answering are different slots, and only one admits an agent."""
    action = PendingAction.model_validate(sealed_row())
    assert action.requested_by == AgentPrincipal.model_validate(AGENT)
    assert isinstance(action.resolution_actor, HumanPrincipal)


def test_seal_cannot_be_handed_an_agent_principal() -> None:
    """The sealing method is typed to a person, so an agent never reaches it."""
    action = PendingAction.model_validate(row())
    agent = AgentPrincipal.model_validate(AGENT)
    with pytest.raises(ValidationError):
        action.seal(
            resolver=agent,  # type: ignore[arg-type]
            option_id="approve",
            receipt_ref=RECEIPT,
            at=LATER,
        )


# ---------- CREATED then WAITING then SEALED ----------


def test_the_status_machine_runs_created_waiting_sealed() -> None:
    """The declared edges are exactly the three-step walk and nothing else."""
    assert PENDING_ACTION_EDGES[PendingActionStatus.CREATED] == frozenset(
        {PendingActionStatus.WAITING}
    )
    assert PENDING_ACTION_EDGES[PendingActionStatus.WAITING] == frozenset(
        {PendingActionStatus.SEALED}
    )
    assert PENDING_ACTION_EDGES[PendingActionStatus.SEALED] == frozenset()


def test_advance_moves_a_created_action_to_waiting() -> None:
    """A question reaches a surface before anybody can answer it."""
    created = PendingAction.model_validate(row(status=PendingActionStatus.CREATED.value))
    assert created.advance(PendingActionStatus.WAITING, at=LATER).status is (
        PendingActionStatus.WAITING
    )


def test_advance_refuses_to_skip_the_waiting_state() -> None:
    """A question nobody was shown cannot have been answered."""
    created = PendingAction.model_validate(row(status=PendingActionStatus.CREATED.value))
    with pytest.raises(ValueError, match="does not move to SEALED"):
        created.advance(PendingActionStatus.SEALED, at=LATER)


def test_advance_refuses_to_reach_the_seal_without_an_answer() -> None:
    """Sealing needs the answer that was given, which advancing does not carry."""
    waiting = PendingAction.model_validate(row())
    with pytest.raises(ValueError, match="needs the answer that was given"):
        waiting.advance(PendingActionStatus.SEALED, at=LATER)


def test_a_sealed_action_never_moves_again() -> None:
    """A changed mind is a new question, not an edit to the old one."""
    sealed = PendingAction.model_validate(sealed_row())
    with pytest.raises(ValueError, match="does not move to"):
        sealed.advance(PendingActionStatus.WAITING, at=LATER)


def test_seal_records_who_answered_which_option_and_its_receipt() -> None:
    """All three seal fields arrive together or not at all."""
    waiting = PendingAction.model_validate(row())
    sealed = waiting.seal(
        resolver=HumanPrincipal.model_validate(OPERATOR),
        option_id="approve",
        receipt_ref=RECEIPT,
        at=LATER,
    )
    assert sealed.status is PendingActionStatus.SEALED
    assert sealed.resolution_actor is not None
    assert sealed.resolution_actor.principal_id == "OP-0001"
    assert sealed.receipt_ref is not None
    assert sealed.receipt_ref.entity_key == "EVD-0001"
    selected = sealed.selected_option
    assert selected is not None
    assert selected.effect is OptionEffect.APPROVE


def test_seal_refuses_an_action_nobody_was_shown() -> None:
    """Only a waiting question is answered."""
    created = PendingAction.model_validate(row(status=PendingActionStatus.CREATED.value))
    with pytest.raises(ValueError, match="only a WAITING action is sealed"):
        created.seal(
            resolver=HumanPrincipal.model_validate(OPERATOR),
            option_id="approve",
            receipt_ref=RECEIPT,
            at=LATER,
        )


def test_seal_refuses_an_option_the_question_does_not_offer() -> None:
    """An answer outside the offered set is not an answer to this question."""
    waiting = PendingAction.model_validate(row())
    with pytest.raises(ValidationError, match="is not one of"):
        waiting.seal(
            resolver=HumanPrincipal.model_validate(OPERATOR),
            option_id="abstain",
            receipt_ref=RECEIPT,
            at=LATER,
        )


@pytest.mark.parametrize("missing", ["resolution_actor", "selected_option_id", "receipt_ref"])
def test_a_sealed_row_missing_any_seal_field_is_refused(missing: str) -> None:
    """A partial seal claims an answer that is not fully on file."""
    payload = sealed_row()
    del payload[missing]
    with pytest.raises(ValidationError, match=missing):
        PendingAction.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resolution_actor", dict(OPERATOR)),
        ("selected_option_id", "approve"),
        ("receipt_ref", RECEIPT),
    ],
)
def test_an_unsealed_row_carrying_a_seal_field_is_refused(field: str, value: Any) -> None:
    """A question nobody answered cannot record who answered it."""
    with pytest.raises(ValidationError, match=field):
        PendingAction.model_validate(row(**{field: value}))


def test_a_sealed_row_refuses_a_clock_that_runs_backwards() -> None:
    """An answer given before the question existed is not an answer."""
    earlier = datetime(2026, 9, 18, 11, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="updated_at cannot precede created_at"):
        PendingAction.model_validate(sealed_row(updated_at=earlier.isoformat()))


def test_advance_raises_a_key_error_for_a_status_outside_the_machine() -> None:
    """The edge table is total over the enum, so a foreign status has no row."""
    action = PendingAction.model_validate(row())
    with pytest.raises(KeyError):
        PENDING_ACTION_EDGES["RESOLVED"]  # type: ignore[index]
    assert action.status is PendingActionStatus.WAITING
