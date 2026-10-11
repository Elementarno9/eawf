"""Seal a plan approval the way an operator does, through the daemon's own verbs.

A plan approval cites a PendingAction the tree holds sealed with its
approving option. The ``/plan`` skill gets one by filing the question with
``runtime.question.open_decision`` and the operator's answer is sealed by
``runtime.delivery.seal_acceptance_approval`` against an evidence row
``runtime.delivery.record_evidence`` filed. Every planning suite that
approves walks those same three verbs here, so a test cannot approve with
a reference nobody answered.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Final

from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.delivery_acceptance import DELIVERY_RECORD_EVIDENCE_METHOD
from eawf.runtime.daemon.methods.delivery_approval import DELIVERY_SEAL_APPROVAL_METHOD
from eawf.runtime.daemon.methods.question_decision import QUESTION_OPEN_DECISION_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context

#: The person who asks and answers. A sealed answer is typed a human.
OPERATOR_ID: Final = "OP-0001"
HUMAN: Final[dict[str, str]] = {"principal_kind": "human", "principal_id": OPERATOR_ID}

#: The option ids the approval question offers.
APPROVE: Final = "approve"
DECLINE: Final = "decline"


def _options() -> list[dict[str, Any]]:
    return [
        {
            "option_id": APPROVE,
            "label": "Apply the plan",
            "effect": "approve",
            "consequence": "The Milestone, its Batches and its Tasks are created as written.",
            "cost": "Work starts on the plan as it stands.",
            "preview": "plan -> Milestone -> Tasks",
        },
        {
            "option_id": DECLINE,
            "label": "Do not apply it",
            "effect": "decline",
            "consequence": "Nothing is created and the plan waits for a revision.",
            "cost": "The work waits until a revised plan is approved.",
            "preview": "plan -> revise",
        },
    ]


def _call(repo_root: Path, runtime: Path, method: str, **params: Any) -> dict[str, Any]:
    ctx = method_context(runtime)
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(repo_root), **params}))


def open_plan_approval(repo_root: Path, runtime: Path, *, subject: str, tag: str) -> str:
    """File the waiting approval question about *subject* and return its URN.

    Args:
        repo_root: The tree the question is filed in.
        runtime: The daemon runtime directory the verbs run under.
        subject: The Milestone the plan lands.
        tag: Distinguishes this question's idempotency keys from another's.
    """
    opened = _call(
        repo_root,
        runtime,
        QUESTION_OPEN_DECISION_METHOD,
        urn=subject,
        idempotency_key=f"ask-plan-{tag}",
        actor=OPERATOR_ID,
        requested_by=dict(HUMAN),
        question="Should this plan be applied as written?",
        options=_options(),
        recommended_option_id=APPROVE,
        recommendation_rationale="The plan passed every check it was submitted against.",
    )
    return str(opened["action_ref"])


def answer_plan_approval(
    repo_root: Path, runtime: Path, *, action_ref: str, option_id: str, tag: str
) -> None:
    """Seal the operator's *option_id* answer onto the waiting question *action_ref*."""
    evidence = _call(
        repo_root,
        runtime,
        DELIVERY_RECORD_EVIDENCE_METHOD,
        urn=action_ref,
        actor=OPERATOR_ID,
        idempotency_key=f"evidence-plan-{tag}",
        kind="decision",
        summary=f"the operator answered {action_ref} with {option_id}",
    )
    _call(
        repo_root,
        runtime,
        DELIVERY_SEAL_APPROVAL_METHOD,
        urn=action_ref,
        expected_revision=1,
        idempotency_key=f"seal-plan-{tag}",
        actor=OPERATOR_ID,
        resolver=dict(HUMAN),
        option_id=option_id,
        receipt_ref=evidence["evidence_ref"],
    )


def seal_plan_approval(repo_root: Path, runtime: Path, *, subject: str, tag: str) -> str:
    """File the approval question about *subject*, answer it approve, and return its URN."""
    action_ref = open_plan_approval(repo_root, runtime, subject=subject, tag=tag)
    answer_plan_approval(repo_root, runtime, action_ref=action_ref, option_id=APPROVE, tag=tag)
    return action_ref
