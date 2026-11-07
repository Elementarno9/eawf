"""A plan approval is consent only when the PendingAction it cites says so.

Driven through the daemon's own plan verbs and the question, evidence and
seal verbs an operator answers through. Each refusal cites an action that
fails exactly one of the checks the approve verb makes under its locks --
held, an operator decision, sealed, answered approve, sealed by an
operator, approved by that operator, asked about this plan's Milestone,
not already spent on another revision, bound to this revision's content
-- and asserts the typed refusal names
that check and that nothing was written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.epoch2.plan_revision import validate_plan_approver
from eawf.runtime.daemon.methods.planning import (
    PLAN_APPROVE_METHOD,
    PLAN_SUBMIT_METHOD,
    ApprovePlanParams,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    MILESTONE_URN,
    document_path,
    seed,
)
from tests.integration.workflow.planning._plan_approval import (
    DECLINE,
    OPERATOR_ID,
    answer_plan_approval,
    open_plan_approval,
    seal_plan_approval,
)
from tests.integration.workflow.planning.test_plan_revision_apply import (
    ACTOR,
    CHILD_BODY,
    CHILD_KEY,
    OPERATOR,
    OTHER_MILESTONE_URN,
    PARENT_KEY,
    REVISION_KEY,
    SLOT,
    applied_parent,
    canary_runtime_under_tmp,
    collection,
    dispatch,
    planned_canary,
    submit,
)

pytestmark = pytest.mark.integration

__all__ = ["canary_runtime_under_tmp"]


#: An agent principal: a valid key that is not an operator's.
AGENT_ID: Final = "EAWF-PLANNER"


def _approve_citing(
    canary: Any,
    tmp_path: Path,
    *,
    key: str,
    action_ref: str,
    approved_by: dict[str, str] | None = OPERATOR,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "key": key,
        "expected_revision": 2,
        "action_ref": action_ref,
        "actor": ACTOR if approved_by is not None else AGENT_ID,
        "idempotency_key": f"approve-{key}",
    }
    if approved_by is not None:
        params["approved_by"] = approved_by
    return dispatch(PLAN_APPROVE_METHOD, canary, tmp_path, params=params)


def _assert_refused(answer: dict[str, Any], *, guard: str) -> None:
    assert answer["status"] == "error", answer
    error = answer["errors"][0]
    assert error["code"] == "protected_approval_required"
    assert error["guard"] == guard


def _submitted(tmp_path: Path, *, code: str) -> Any:
    canary = planned_canary(tmp_path, code=code)
    assert submit(canary, tmp_path)["status"] == "ok"
    return canary


def test_an_approval_citing_an_action_the_tree_does_not_hold_is_refused(tmp_path: Path) -> None:
    """Empty boundary: a well-formed reference nobody filed is not consent."""
    canary = _submitted(tmp_path, code="NOACT")
    before = document_path(canary).read_bytes()

    answer = _approve_citing(
        canary, tmp_path, key=REVISION_KEY, action_ref=f"{SLOT}/pending-action/ACT-0999"
    )

    _assert_refused(answer, guard="approval_action_held")
    assert document_path(canary).read_bytes() == before
    assert collection(canary, "plan_revision")[REVISION_KEY]["status"] == "VALIDATED"


def test_an_approval_citing_an_unanswered_action_is_refused(tmp_path: Path) -> None:
    """A question asked but not yet answered approves nothing."""
    canary = _submitted(tmp_path, code="WAITING")
    action_ref = open_plan_approval(
        canary.root, tmp_path / "runtime", subject=MILESTONE_URN, tag="waiting"
    )
    before = document_path(canary).read_bytes()

    answer = _approve_citing(canary, tmp_path, key=REVISION_KEY, action_ref=action_ref)

    _assert_refused(answer, guard="approval_action_sealed")
    assert document_path(canary).read_bytes() == before


def test_an_approval_citing_a_declined_answer_is_refused(tmp_path: Path) -> None:
    """A sealed answer that chose the declining option is the operator saying no."""
    canary = _submitted(tmp_path, code="DECLINED")
    runtime = tmp_path / "runtime"
    action_ref = open_plan_approval(canary.root, runtime, subject=MILESTONE_URN, tag="no")
    answer_plan_approval(canary.root, runtime, action_ref=action_ref, option_id=DECLINE, tag="no")
    before = document_path(canary).read_bytes()

    answer = _approve_citing(canary, tmp_path, key=REVISION_KEY, action_ref=action_ref)

    _assert_refused(answer, guard="approval_action_approves")
    assert document_path(canary).read_bytes() == before


def test_an_approval_citing_an_answer_about_another_milestone_is_refused(tmp_path: Path) -> None:
    """A yes about one Milestone's plan is not a yes about another's."""
    canary = _submitted(tmp_path, code="OTHERMS")
    action_ref = seal_plan_approval(
        canary.root, tmp_path / "runtime", subject=OTHER_MILESTONE_URN, tag="other"
    )
    before = document_path(canary).read_bytes()

    answer = _approve_citing(canary, tmp_path, key=REVISION_KEY, action_ref=action_ref)

    _assert_refused(answer, guard="approval_action_subject")
    assert document_path(canary).read_bytes() == before


def test_an_answer_already_spent_on_another_revision_is_refused(tmp_path: Path) -> None:
    """Off-by-one: the parent's approval cannot be replayed onto its re-planned child."""
    canary = applied_parent(tmp_path, code="SPENT", project=None)
    spent = collection(canary, "plan_revision")[PARENT_KEY]["approval"]["action_ref"]
    submitted = dispatch(
        PLAN_SUBMIT_METHOD,
        canary,
        tmp_path,
        params={
            "proposal": {
                "key": CHILD_KEY,
                "author": OPERATOR,
                "body": CHILD_BODY,
                "parent_key": PARENT_KEY,
            },
            "actor": ACTOR,
            "idempotency_key": f"submit-{CHILD_KEY}",
        },
    )
    assert submitted["status"] == "ok", submitted["errors"]
    before = document_path(canary).read_bytes()

    answer = _approve_citing(canary, tmp_path, key=CHILD_KEY, action_ref=spent)

    _assert_refused(answer, guard="approval_action_unspent")
    assert PARENT_KEY in answer["errors"][0]["message"]
    assert document_path(canary).read_bytes() == before


def _submitted_child(tmp_path: Path, *, code: str) -> Any:
    """Return a canary whose applied parent plan has a validated child in the same Milestone."""
    canary = applied_parent(tmp_path, code=code, project=None)
    submitted = dispatch(
        PLAN_SUBMIT_METHOD,
        canary,
        tmp_path,
        params={
            "proposal": {
                "key": CHILD_KEY,
                "author": OPERATOR,
                "body": CHILD_BODY,
                "parent_key": PARENT_KEY,
            },
            "actor": ACTOR,
            "idempotency_key": f"submit-{CHILD_KEY}",
        },
    )
    assert submitted["status"] == "ok", submitted["errors"]
    return canary


@pytest.mark.parametrize("bound_to", [PARENT_KEY, None], ids=["another-revision", "no-revision"])
def test_an_answer_not_bound_to_this_revision_is_refused(
    tmp_path: Path, bound_to: str | None
) -> None:
    """A yes to one plan of a Milestone, or to no plan at all, is not a yes to its re-plan."""
    canary = _submitted_child(tmp_path, code="UNBOUND")
    action_ref = seal_plan_approval(
        canary.root,
        tmp_path / "runtime",
        subject=MILESTONE_URN,
        tag="unbound",
        plan_revision=bound_to,
    )
    before = document_path(canary).read_bytes()

    answer = _approve_citing(canary, tmp_path, key=CHILD_KEY, action_ref=action_ref)

    _assert_refused(answer, guard="approval_action_digest")
    assert document_path(canary).read_bytes() == before
    assert collection(canary, "plan_revision")[CHILD_KEY]["status"] == "VALIDATED"


def test_an_answer_of_another_kind_is_refused(tmp_path: Path) -> None:
    """An acceptance approval about the Milestone is not consent to its plan."""
    canary = _submitted(tmp_path, code="OTHERKIND")
    action_ref = seal_plan_approval(
        canary.root,
        tmp_path / "runtime",
        subject=MILESTONE_URN,
        tag="kind",
        plan_revision=REVISION_KEY,
    )
    key = parse_qualified_urn(action_ref).entity_key
    row = collection(canary, "pending_action")[key]
    # A row shaped like a sealed acceptance approval of the same Milestone.
    seed(canary, {"pending_action": {key: {**row, "kind": "protected_approval"}}})
    before = document_path(canary).read_bytes()

    answer = _approve_citing(canary, tmp_path, key=REVISION_KEY, action_ref=action_ref)

    _assert_refused(answer, guard="approval_action_kind")
    assert document_path(canary).read_bytes() == before


def test_an_approval_citing_a_sealed_approving_answer_lands(tmp_path: Path) -> None:
    """Single boundary: the one answer that consents approves, and the receipt cites it."""
    canary = _submitted(tmp_path, code="CONSENT")
    action_ref = seal_plan_approval(
        canary.root,
        tmp_path / "runtime",
        subject=MILESTONE_URN,
        tag="yes",
        plan_revision=REVISION_KEY,
    )

    answer = _approve_citing(canary, tmp_path, key=REVISION_KEY, action_ref=action_ref)

    assert answer["status"] == "ok", answer["errors"]
    stored = collection(canary, "plan_revision")[REVISION_KEY]
    assert stored["status"] == "APPROVED"
    assert stored["approval"]["action_ref"] == action_ref


def test_the_approver_is_derived_from_the_sealing_operator(tmp_path: Path) -> None:
    """An agent approving with no approver of its own records the operator who sealed the act."""
    canary = _submitted(tmp_path, code="DERIVED")
    action_ref = seal_plan_approval(
        canary.root,
        tmp_path / "runtime",
        subject=MILESTONE_URN,
        tag="derived",
        plan_revision=REVISION_KEY,
    )

    answer = _approve_citing(
        canary, tmp_path, key=REVISION_KEY, action_ref=action_ref, approved_by=None
    )

    assert answer["status"] == "ok", answer["errors"]
    stored = collection(canary, "plan_revision")[REVISION_KEY]
    assert stored["status"] == "APPROVED"
    assert stored["approval"]["approved_by"] == {
        "principal_kind": "operator",
        "principal_id": OPERATOR_ID,
    }


def test_an_absent_approver_passes_the_boundary_and_a_bad_reference_still_refuses() -> None:
    """The boundary admits no approver, and still checks what the reference addresses."""
    action_ref = parse_qualified_urn(f"{SLOT}/pending-action/ACT-0001")
    params = ApprovePlanParams.model_validate(
        {
            "key": REVISION_KEY,
            "expected_revision": 2,
            "action_ref": str(action_ref),
            "actor": AGENT_ID,
            "idempotency_key": "approve-absent",
        }
    )

    assert params.approved_by is None
    assert validate_plan_approver(action_ref=action_ref, approved_by=None) is None
    with pytest.raises(ValueError, match="pending-action"):
        validate_plan_approver(action_ref=parse_qualified_urn(MILESTONE_URN), approved_by=None)


@pytest.mark.parametrize(
    ("approved_by", "resolver", "guard"),
    [
        (
            {"principal_kind": "operator", "principal_id": "OP-0002"},
            OPERATOR_ID,
            "approval_action_approver",
        ),
        (None, "OPERATOR", "approval_action_operator"),
    ],
)
def test_an_approver_or_resolver_that_is_not_the_sealing_operator_is_refused(
    tmp_path: Path, approved_by: dict[str, str] | None, resolver: str, guard: str
) -> None:
    """Another operator cannot approve on this seal; a seal no operator made approves nothing."""
    canary = _submitted(tmp_path, code="NOTSEALER")
    action_ref = seal_plan_approval(
        canary.root,
        tmp_path / "runtime",
        subject=MILESTONE_URN,
        tag="sealer",
        plan_revision=REVISION_KEY,
    )
    key = parse_qualified_urn(action_ref).entity_key
    row = collection(canary, "pending_action")[key]
    # A row shaped like one sealed before the seal checked the resolver.
    seed(
        canary,
        {
            "pending_action": {
                key: {
                    **row,
                    "resolution_actor": {"principal_kind": "human", "principal_id": resolver},
                }
            }
        },
    )
    before = document_path(canary).read_bytes()

    answer = _approve_citing(
        canary, tmp_path, key=REVISION_KEY, action_ref=action_ref, approved_by=approved_by
    )

    _assert_refused(answer, guard=guard)
    assert document_path(canary).read_bytes() == before
    assert collection(canary, "plan_revision")[REVISION_KEY]["status"] == "VALIDATED"
