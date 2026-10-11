"""A plan approval is consent only when the PendingAction it cites says so.

Driven through the daemon's own plan verbs and the question, evidence and
seal verbs an operator answers through. Each refusal cites an action that
fails exactly one of the checks the approve verb makes under its locks --
held, sealed, answered approve, asked about this plan's Milestone, not
already spent on another revision -- and asserts the typed refusal names
that check and that nothing was written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.daemon.methods.planning import PLAN_APPROVE_METHOD, PLAN_SUBMIT_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    MILESTONE_URN,
    document_path,
)
from tests.integration.workflow.planning._plan_approval import (
    DECLINE,
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


def _approve_citing(canary: Any, tmp_path: Path, *, key: str, action_ref: str) -> dict[str, Any]:
    return dispatch(
        PLAN_APPROVE_METHOD,
        canary,
        tmp_path,
        params={
            "key": key,
            "expected_revision": 2,
            "action_ref": action_ref,
            "approved_by": OPERATOR,
            "actor": ACTOR,
            "idempotency_key": f"approve-{key}",
        },
    )


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


def test_an_approval_citing_a_sealed_approving_answer_lands(tmp_path: Path) -> None:
    """Single boundary: the one answer that consents approves, and the receipt cites it."""
    canary = _submitted(tmp_path, code="CONSENT")
    action_ref = seal_plan_approval(
        canary.root, tmp_path / "runtime", subject=MILESTONE_URN, tag="yes"
    )

    answer = _approve_citing(canary, tmp_path, key=REVISION_KEY, action_ref=action_ref)

    assert answer["status"] == "ok", answer["errors"]
    stored = collection(canary, "plan_revision")[REVISION_KEY]
    assert stored["status"] == "APPROVED"
    assert stored["approval"]["action_ref"] == action_ref
