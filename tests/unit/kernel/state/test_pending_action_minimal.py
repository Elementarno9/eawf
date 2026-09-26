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

The last section drives the record's producer: a question opened and
sealed through the native transaction on a disposable canary under
``tmp_path``, where a second, conflicting answer records its own
disposition beside the standing seal and a stale-revision seal is
refused with nothing written.

Nothing here reads a clock or opens a socket, and nothing writes outside
``tmp_path``.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import IdentityError
from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.state.epoch2.milestone import Milestone
from eawf.kernel.state.epoch2.pending_action import (
    MAX_OPTIONS,
    MIN_OPTIONS,
    PENDING_ACTION_EDGES,
    AgentPrincipal,
    AnswerOutcome,
    HumanPrincipal,
    OptionEffect,
    PendingAction,
    PendingActionKind,
    PendingActionStatus,
)
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusalCode, TransactionRefusedError
from eawf.runtime.daemon.methods.delivery_approval import (
    ApprovalOpenParams,
    ApprovalSealParams,
    open_acceptance_approval,
    seal_acceptance_approval,
)
from eawf.workflow.delivery.acceptance_approval import (
    ApprovalRefusal,
    ApprovalRefusedError,
    next_action_key,
    require_verified_batches,
    seal_question,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
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


# ---------- the revision a seal is decided against ----------


def test_a_row_nothing_moved_stands_at_revision_one() -> None:
    """A stored row without a revision reads as its first revision."""
    assert PendingAction.model_validate(row()).revision == 1


def test_a_revision_below_one_is_refused() -> None:
    """Revision zero names no state the row was ever in."""
    with pytest.raises(ValidationError, match="revision"):
        PendingAction.model_validate(row(revision=0))


def test_advance_and_seal_each_move_the_revision_on_by_one() -> None:
    """Every move is one revision, so a stale answer is detectable."""
    created = PendingAction.model_validate(row(status=PendingActionStatus.CREATED.value))
    waiting = created.advance(PendingActionStatus.WAITING, at=LATER)
    sealed = waiting.seal(
        resolver=HumanPrincipal.model_validate(OPERATOR),
        option_id="approve",
        receipt_ref=RECEIPT,
        at=LATER,
    )
    assert (created.revision, waiting.revision, sealed.revision) == (1, 2, 3)


# ---------- the producer's rules, held directly ----------


def milestone(**overrides: Any) -> Milestone:
    """Return the seeded Milestone in acceptance review, with *overrides* applied."""
    return Milestone.model_validate({**seed_row("milestone", "ACCEPTANCE_REVIEW"), **overrides})


def batch(status: str = "READY_TO_MERGE", *, key: str = "BAT-0007") -> DeliveryBatch:
    """Return one seeded Batch of MLS-0030 in *status*."""
    return DeliveryBatch.model_validate(rekeyed(seed_row("batch", status), key=key))


@pytest.mark.parametrize("status", ["READY_TO_MERGE", "MERGING", "COMPLETED"])
def test_verified_batches_clear_on_every_head_bound_status(status: str) -> None:
    """A Batch past its verification cycle is verified however far it merged."""
    refs = require_verified_batches(milestone(), [batch(status)])
    assert [ref.entity_key for ref in refs] == ["BAT-0007"]


@pytest.mark.parametrize("status", ["PLANNED", "ACTIVE", "FAILED"])
def test_an_unverified_batch_keeps_the_question_closed(status: str) -> None:
    """One Batch still in work is enough to refuse."""
    with pytest.raises(ApprovalRefusedError, match="BAT-0008") as caught:
        require_verified_batches(milestone(), [batch(), batch(status, key="BAT-0008")])
    assert caught.value.code is ApprovalRefusal.BATCHES_UNVERIFIED


def test_a_milestone_with_no_batch_is_not_asked_about() -> None:
    """The empty boundary: nothing delivered is nothing verified."""
    with pytest.raises(ApprovalRefusedError, match="owns no batch"):
        require_verified_batches(milestone(), [])


def test_a_cancelled_batch_is_left_out_unless_it_is_required() -> None:
    """A cancelled optional Batch delivers nothing; a cancelled required one is missing."""
    cancelled = batch("CANCELLED", key="BAT-0008")
    assert len(require_verified_batches(milestone(), [batch(), cancelled])) == 1
    required = milestone(required_batch_refs=[str(cancelled.urn)])
    with pytest.raises(ApprovalRefusedError, match="requires BAT-0008"):
        require_verified_batches(required, [batch(), cancelled])


def test_a_milestone_outside_review_is_not_asked_about() -> None:
    """The question is asked in acceptance review and nowhere else."""
    active = milestone(status="ACTIVE", acceptance_bundle_revision=None)
    with pytest.raises(ApprovalRefusedError) as caught:
        require_verified_batches(active, [batch()])
    assert caught.value.code is ApprovalRefusal.MILESTONE_NOT_IN_REVIEW


@pytest.mark.parametrize(
    ("taken", "expected"),
    [((), "ACT-0001"), (("ACT-0001",), "ACT-0002"), (("ACT-0009", "ACT-0002"), "ACT-0010")],
)
def test_the_next_action_key_follows_the_highest_taken(
    taken: tuple[str, ...], expected: str
) -> None:
    """Keys are never reused, so the next one follows the highest, not the count."""
    assert next_action_key(taken) == expected


def test_the_next_action_key_refuses_a_saturated_key_space() -> None:
    """The max-length boundary: ``ACT-9999`` has no successor."""
    with pytest.raises(IdentityError):
        next_action_key(("ACT-9999",))


def test_seal_question_refuses_an_answer_given_to_an_older_revision() -> None:
    """A question that moved since is not the question that was answered."""
    with pytest.raises(ApprovalRefusedError) as caught:
        seal_question(
            PendingAction.model_validate(row(revision=2)),
            expected_revision=1,
            resolver=HumanPrincipal.model_validate(OPERATOR),
            option_id="approve",
            receipt_ref=RECEIPT,
            at=LATER,
        )
    assert caught.value.code is ApprovalRefusal.ACTION_STALE


# ---------- created and sealed through the transaction ----------

TREE_MILESTONE: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"
TREE_ACTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001"
EVIDENCE: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0002"


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Epoch2RootContext, Path]:
    """A canary holding MLS-0030 in review, its verified Batch and two evidence rows."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    provisioned = provision(tmp_path / "tree", code="SEAL")
    seed(
        provisioned,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")},
            "batch": {"BAT-0007": seed_row("batch", "READY_TO_MERGE")},
        },
    )
    context = root_context(provisioned, tmp_path / "runtime")
    with context.session([TREE_MILESTONE]) as session:
        for key, kind in (("EVD-0001", "decision"), ("EVD-0002", "artifact")):
            append_ledger_record(
                session.ledger_path(Epoch2Collection.EVIDENCE),
                LedgerRecord(
                    collection=Epoch2Collection.EVIDENCE,
                    record_key=key,
                    status="recorded",
                    recorded_at=AT,
                    payload={
                        "id": key,
                        "kind": kind,
                        "summary": "an observation",
                        "recorded_at": AT.isoformat(),
                    },
                ),
            )
    return context, document_path(provisioned)


def open_params() -> ApprovalOpenParams:
    """Return the request verify opens the question with."""
    return ApprovalOpenParams.model_validate(
        {
            "urn": TREE_MILESTONE,
            "actor": "SKILL-VERIFY",
            "requested_by": dict(OPERATOR),
            "steps": [
                {
                    "step_id": "AS-01",
                    "passed": True,
                    "observation": "the install completed and reported the version",
                    "evidence_kinds": ["artifact"],
                    "evidence_refs": [EVIDENCE],
                }
            ],
            "accepted_binding": seed_row("milestone", "COMPLETED")["accepted_binding"],
        }
    )


def seal_params(**overrides: Any) -> ApprovalSealParams:
    """Return the operator's answer, with *overrides* applied."""
    params: dict[str, Any] = {
        "urn": TREE_ACTION,
        "expected_revision": 1,
        "idempotency_key": "req-seal-0001",
        "actor": "OP-0001",
        "resolver": dict(OPERATOR),
        "option_id": "approve",
        "receipt_ref": RECEIPT,
    }
    params.update(overrides)
    return ApprovalSealParams.model_validate(params)


def stored(path: Path) -> PendingAction:
    """Return ACT-0001 as the document holds it."""
    return PendingAction.model_validate(
        document_rows(read_document(path), Epoch2Collection.PENDING_ACTION)["ACT-0001"]
    )


def test_the_question_is_created_waiting_through_the_transaction(
    tree: tuple[Epoch2RootContext, Path],
) -> None:
    """Opening writes one waiting protected approval bound to the bundle digest."""
    context, path = tree
    commit = open_acceptance_approval(context, open_params(), now=AT)

    action = stored(path)
    assert commit.answer.created is True
    assert commit.answer.action_ref == TREE_ACTION
    assert action.status is PendingActionStatus.WAITING
    assert action.kind is PendingActionKind.PROTECTED_APPROVAL
    assert action.bundle_digest == commit.answer.bundle_digest
    assert [item.payload["name"] for item in commit.envelopes] == [
        "ledger.milestone.appended",
        "admission.pending_action.created",
    ]


def test_the_question_is_sealed_through_the_transaction(
    tree: tuple[Epoch2RootContext, Path],
) -> None:
    """The answer lands as one sealed row, one revision on, resolved by a person."""
    context, path = tree
    open_acceptance_approval(context, open_params(), now=AT)

    commit = seal_acceptance_approval(context, seal_params(), now=LATER)

    action = stored(path)
    assert commit.answer.status == PendingActionStatus.SEALED.value
    assert action.status is PendingActionStatus.SEALED
    assert action.revision == 2
    assert action.resolution_actor is not None
    assert action.resolution_actor.principal_id == "OP-0001"
    assert [item.payload["name"] for item in commit.envelopes] == [
        "resolution.pending_action.sealed"
    ]


def test_a_second_answer_is_superseded_with_its_disposition_recorded(
    tree: tuple[Epoch2RootContext, Path],
) -> None:
    """The seal never moves, but a conflicting second answer records its own disposition."""
    context, path = tree
    open_acceptance_approval(context, open_params(), now=AT)
    seal_acceptance_approval(context, seal_params(), now=LATER)
    before = path.read_bytes()

    commit = seal_acceptance_approval(
        context,
        seal_params(idempotency_key="req-seal-0002", expected_revision=2, option_id="decline"),
        now=LATER,
    )

    assert commit.answer.outcome == AnswerOutcome.SUPERSEDED.value
    assert commit.answer.status == PendingActionStatus.SEALED.value
    assert [item.payload["name"] for item in commit.envelopes] == [
        "resolution.pending_action.answer_recorded"
    ]
    assert path.read_bytes() != before
    resolved = stored(path)
    assert resolved.selected_option_id == "approve"
    assert resolved.resolution_actor is not None
    assert resolved.resolution_actor.principal_id == "OP-0001"
    row = {item.principal_id: item for item in resolved.dispositions}["OP-0001"]
    assert row.outcome is AnswerOutcome.SUPERSEDED
    assert row.option_id == "decline"
    answer_row = {item.principal_id: item for item in commit.answer.dispositions}["OP-0001"]
    assert answer_row.outcome is AnswerOutcome.SUPERSEDED


def test_a_different_principals_answer_after_seal_records_its_own_disposition(
    tree: tuple[Epoch2RootContext, Path],
) -> None:
    """A losing principal's disposition rides beside the winner's, not over it."""
    context, path = tree
    open_acceptance_approval(context, open_params(), now=AT)
    seal_acceptance_approval(context, seal_params(), now=LATER)

    commit = seal_acceptance_approval(
        context,
        seal_params(
            idempotency_key="req-seal-0002",
            expected_revision=2,
            actor="OP-0002",
            resolver={"principal_kind": "human", "principal_id": "OP-0002"},
            option_id="decline",
        ),
        now=LATER,
    )

    assert commit.answer.outcome == AnswerOutcome.SUPERSEDED.value
    assert len(commit.envelopes) == 1
    rows = {item.principal_id: item for item in stored(path).dispositions}
    assert rows["OP-0001"].outcome is AnswerOutcome.SEALED
    assert rows["OP-0001"].option_id == "approve"
    assert rows["OP-0002"].outcome is AnswerOutcome.SUPERSEDED
    assert rows["OP-0002"].option_id == "decline"


def test_a_superseded_answer_retried_under_its_own_key_writes_nothing_twice(
    tree: tuple[Epoch2RootContext, Path],
) -> None:
    """A losing answer retried under the idempotency key it first lost under is a replay."""
    context, path = tree
    open_acceptance_approval(context, open_params(), now=AT)
    seal_acceptance_approval(context, seal_params(), now=LATER)
    loser = seal_params(idempotency_key="req-seal-0002", expected_revision=2, option_id="decline")
    first = seal_acceptance_approval(context, loser, now=LATER)
    before = path.read_bytes()

    replayed = seal_acceptance_approval(context, loser, now=LATER)

    assert replayed.answer.outcome == AnswerOutcome.SUPERSEDED.value
    assert replayed.answer.outcome == first.answer.outcome
    assert replayed.envelopes == ()
    assert path.read_bytes() == before


def test_a_winning_answer_retried_under_a_fresh_key_returns_the_first_receipt(
    tree: tuple[Epoch2RootContext, Path],
) -> None:
    """The identical winning answer under a new idempotency key is not a second seal."""
    context, path = tree
    open_acceptance_approval(context, open_params(), now=AT)
    seal_acceptance_approval(context, seal_params(), now=LATER)
    before = path.read_bytes()

    commit = seal_acceptance_approval(
        context,
        seal_params(idempotency_key="req-seal-0002", expected_revision=2, receipt_ref=EVIDENCE),
        now=LATER,
    )

    assert commit.answer.outcome == AnswerOutcome.SEALED.value
    assert commit.answer.status == PendingActionStatus.SEALED.value
    assert commit.answer.receipt_ref == RECEIPT
    assert commit.envelopes == ()
    assert path.read_bytes() == before
    assert stored(path).receipt_ref is not None
    assert stored(path).receipt_ref.entity_key == "EVD-0001"


def test_a_retried_seal_returns_the_standing_answer_and_writes_nothing(
    tree: tuple[Epoch2RootContext, Path],
) -> None:
    """The same request under the same key is a retry, not a second seal."""
    context, path = tree
    open_acceptance_approval(context, open_params(), now=AT)
    seal_acceptance_approval(context, seal_params(), now=LATER)
    before = path.read_bytes()

    commit = seal_acceptance_approval(context, seal_params(), now=LATER)

    assert commit.answer.created is False
    assert commit.envelopes == ()
    assert path.read_bytes() == before


def test_a_seal_against_a_stale_revision_is_refused(
    tree: tuple[Epoch2RootContext, Path],
) -> None:
    """An answer given to a revision the question is no longer at is a conflict."""
    context, path = tree
    open_acceptance_approval(context, open_params(), now=AT)
    before = path.read_bytes()

    with pytest.raises(TransactionRefusedError) as caught:
        seal_acceptance_approval(context, seal_params(expected_revision=3), now=LATER)

    assert caught.value.code is TransactionRefusalCode.REVISION_CONFLICT
    assert path.read_bytes() == before


def test_a_seal_citing_unrecorded_evidence_is_refused(
    tree: tuple[Epoch2RootContext, Path],
) -> None:
    """The receipt of an answer must be a row the tree holds."""
    context, path = tree
    open_acceptance_approval(context, open_params(), now=AT)
    before = path.read_bytes()
    unheld = RECEIPT.replace("EVD-0001", "EVD-0404")

    with pytest.raises(TransactionRefusedError, match="EVD-0404"):
        seal_acceptance_approval(context, seal_params(receipt_ref=unheld), now=LATER)

    assert path.read_bytes() == before


def test_a_seal_of_a_question_the_tree_does_not_hold_is_refused(
    tree: tuple[Epoch2RootContext, Path],
) -> None:
    """Nothing was asked, so nothing can be answered."""
    context, _ = tree
    with pytest.raises(TransactionRefusedError) as caught:
        seal_acceptance_approval(context, seal_params(), now=LATER)
    assert caught.value.code is TransactionRefusalCode.IDENTITY_NOT_FOUND


def test_a_seal_naming_an_agent_resolver_does_not_parse() -> None:
    """The resolver slot of the request is typed a person too."""
    with pytest.raises(ValidationError, match="resolver"):
        seal_params(resolver=dict(AGENT))


def test_a_seal_in_somebody_elses_name_does_not_parse() -> None:
    """The actor answers for itself, never on another person's behalf."""
    with pytest.raises(ValidationError, match="cannot seal an answer"):
        seal_params(actor="OP-0002")


def test_a_seal_addressed_at_another_kind_does_not_parse() -> None:
    """Only a pending action is sealed."""
    with pytest.raises(ValidationError, match="not a pending action"):
        seal_params(urn=TREE_MILESTONE)
