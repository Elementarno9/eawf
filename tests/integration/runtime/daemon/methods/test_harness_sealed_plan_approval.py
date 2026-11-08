"""An operator approves a plan in session: the question hook seals the act, the agent approves.

Driven end to end against a planned canary: the plan-approval decision is filed through
``runtime.question.open_decision``, asked through the question hook exactly as Claude
Code reports an AskUserQuestion call on an operator's own session, which is on no Run,
and the plan is then approved by an agent principal citing the sealed act. Five forged
reports of the same call leave the act waiting with the code of the check they fail.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.pending_action_disposition import ACTION_ASSIGN_METHOD
from eawf.runtime.daemon.methods.planning import PLAN_APPROVE_METHOD
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.host_calls import record_host_question
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    method_context,
)
from tests.integration.runtime.daemon.methods.test_harness_seal import (
    APPROVE_LABEL,
    HUMAN,
    OPERATOR_ID,
    HookClient,
    claim,
    open_plan_decision,
    tool_input,
)
from tests.integration.workflow.planning.test_plan_revision_apply import (
    REVISION_KEY,
    collection,
    planned_canary,
    submit,
)

pytestmark = pytest.mark.integration

__all__ = ["claim"]

#: The agent that approves once the operator has answered.
AGENT_ID: Final = "EAWF-PLANNER"

#: The operator's own Claude Code session, which is on no Run, and its question call.
OPERATOR_SESSION: Final = "operator-session-0001"
CALL: Final = "toolu_01plan"


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """One daemon context shared by every call of a case."""
    return method_context(tmp_path / "runtime")


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding a validated plan revision for MLS-0030."""
    provisioned = planned_canary(tmp_path, code="HARNESS")
    assert submit(provisioned, tmp_path)["status"] == "ok"
    return provisioned


def event(
    event_type: HookEventType, tool: dict[str, Any], answers: dict[str, str] | None
) -> HookEvent:
    """Return the question hook's event for the call *tool* asks, as Claude Code sends it.

    The returned call carries the operator's answers in its input as well as its response.
    """
    payload: dict[str, Any] = {
        "session_id": OPERATOR_SESSION,
        "tool_name": "AskUserQuestion",
        "tool_use_id": CALL,
        "tool_input": tool,
    }
    if answers is not None:
        payload["tool_input"] = {**tool, "answers": answers}
        payload["tool_response"] = {"questions": tool["questions"], "answers": answers}
    return HookEvent(
        event_type=event_type,
        runtime="claude",
        occurred_at=AT,
        payloads={event_type.value: payload},
    )


def hook(
    ctx: MethodContext,
    canary: CanaryProvision,
    event_type: HookEventType,
    tool: dict[str, Any],
    answers: dict[str, str] | None = None,
) -> str:
    """Run the question hook on one event against the canary and return its output."""
    result = record_host_question(
        event(event_type, tool, answers),
        daemon_client_factory=lambda: HookClient(ctx),
        repo_root=canary.root,
    )
    assert not result.block
    return result.output


def action(canary: CanaryProvision) -> dict[str, Any]:
    """Return the plan-approval act the canary holds."""
    return collection(canary, "pending_action")["ACT-0001"]


def approve(ctx: MethodContext, canary: CanaryProvision, action_ref: str) -> dict[str, Any]:
    """Approve the plan as the agent, citing the act and naming no approver."""
    answer: dict[str, Any] = asyncio.run(
        methods.dispatch(
            PLAN_APPROVE_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "key": REVISION_KEY,
                "expected_revision": 2,
                "action_ref": action_ref,
                "actor": AGENT_ID,
                "idempotency_key": f"approve-{REVISION_KEY}",
            },
        )
    )
    return answer


def test_an_answer_in_session_seals_the_act_and_the_agent_approves(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    opened = open_plan_decision(ctx, canary, plan_revision=REVISION_KEY)
    tool = tool_input(opened["host_question"])

    raised = hook(ctx, canary, HookEventType.PRE_TOOL_USE, tool)
    answered = hook(
        ctx,
        canary,
        HookEventType.POST_TOOL_USE,
        tool,
        {tool["questions"][0]["question"]: APPROVE_LABEL},
    )

    assert raised == "runtime.host_question ok questions=QST-0001"
    assert answered == "runtime.host_question ok questions=QST-0001 sealed=ACT-0001"
    row = action(canary)
    assert (row["status"], row["resolution_actor"]) == ("SEALED", HUMAN)
    assert row["harness_attestation"]["tool_use_id"] == CALL
    assert row["harness_attestation"]["run_ref"] is None

    approved = approve(ctx, canary, opened["action_ref"])

    assert approved["status"] == "ok", approved["errors"]
    stored = collection(canary, "plan_revision")[REVISION_KEY]
    assert stored["status"] == "APPROVED"
    assert stored["approval"]["action_ref"] == opened["action_ref"]
    assert stored["approval"]["approved_by"] == {
        "principal_kind": "operator",
        "principal_id": OPERATOR_ID,
    }


def _assign(ctx: MethodContext, canary: CanaryProvision, action_ref: str) -> None:
    """Move the act one revision on while it still waits."""
    asyncio.run(
        methods.dispatch(
            ACTION_ASSIGN_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": action_ref,
                "expected_revision": 1,
                "idempotency_key": "assign-moved",
                "actor": OPERATOR_ID,
                "assignee": OPERATOR_ID,
            },
        )
    )


@pytest.mark.parametrize(
    "code",
    [
        "harness_raise_missing",
        "harness_options_mismatch",
        "harness_preanswered",
        "harness_action_stale",
        "harness_question_mismatch",
    ],
    ids=["lone-post", "relabelled", "tool-input-answers", "moved-revision", "paraphrased"],
)
def test_a_forged_report_leaves_the_act_waiting(
    canary: CanaryProvision, ctx: MethodContext, code: str
) -> None:
    opened = open_plan_decision(ctx, canary, plan_revision=REVISION_KEY)
    tool = tool_input(opened["host_question"])
    question = tool["questions"][0]
    label = APPROVE_LABEL
    if code == "harness_options_mismatch":
        label = "Apply the plan now"
        question["options"][0]["label"] = label
    if code == "harness_question_mismatch":
        # The marker and every label stay; the words the operator reads do not.
        marker = question["question"].split("\n", 1)[0]
        question["question"] = f"{marker}\nMay I tidy whitespace in README?"
        for option in question["options"]:
            option["description"] = "Harmless"
    if code == "harness_preanswered":
        tool["answers"] = {question["question"]: label}
    if code != "harness_raise_missing":
        hook(ctx, canary, HookEventType.PRE_TOOL_USE, tool)
    if code == "harness_action_stale":
        _assign(ctx, canary, opened["action_ref"])

    output = hook(ctx, canary, HookEventType.POST_TOOL_USE, tool, {question["question"]: label})

    assert output.endswith(f"refused=ACT-0001:{code}")
    assert action(canary)["status"] == "WAITING"
    refused = approve(ctx, canary, opened["action_ref"])
    assert refused["status"] == "error"
    assert refused["errors"][0]["guard"] == "approval_action_sealed"
