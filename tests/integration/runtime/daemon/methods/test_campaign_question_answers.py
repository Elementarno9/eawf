"""A Campaign that converges answers its open questions from its synthesis.

Driven end to end on the native verbs with the stub research agent: once every step
is done, the close answers each question the Campaign filed, citing the synthesis
artifact and the artifacts of the steps that worked the question, each with its
digest. A Campaign a budget stop starved, or one the operator cancelled, leaves its
questions open.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.campaign import CampaignView, stored_revisions
from eawf.kernel.state.enums import OpenQuestionStatus
from eawf.kernel.state.epoch2.campaign import Campaign
from eawf.kernel.state.epoch2.question import OpenQuestion
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods.campaign_answers import answered_questions
from eawf.runtime.daemon.methods.campaign_run import CAMPAIGN_START_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import document_path
from tests.integration.runtime.daemon.methods.test_campaign_run import (
    StubAgent,
    _settled,
    _start,
    agent,
    canary,
    isolated,
    served,
)

pytestmark = pytest.mark.integration

__all__ = ["agent", "canary", "isolated"]

AT = datetime(2026, 10, 8, tzinfo=UTC)


def _questions(canary: CanaryProvision) -> dict[str, OpenQuestion]:
    rows = document_rows(read_document(document_path(canary)), Epoch2Collection.OPEN_QUESTION)
    return {key: OpenQuestion.model_validate(row) for key, row in rows.items() if "urn" in row}


def _campaign(canary: CanaryProvision, key: str = "CAM-0001") -> Campaign:
    return Campaign.model_validate(read_document(document_path(canary))["campaign"][key])


def _drive(canary: CanaryProvision, tmp_path: Path, **brief: Any) -> CampaignView:
    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(CAMPAIGN_START_METHOD, **_start(canary.root, **brief))
            return await _settled(client, canary.root, started["record"]["key"])

    return asyncio.run(body())


def test_a_converged_campaign_answers_its_questions_from_the_synthesis(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    view = _drive(canary, tmp_path, depth="medium", agents=1)

    assert view.stop is not None
    assert view.stop.reason == "converged"
    [survey_card, synthesis_card] = view.artifacts
    [question] = [q for q in _questions(canary).values() if q.scope_ref == view.campaign_ref]
    assert question.status is OpenQuestionStatus.ANSWERED
    assert question.resolution_actor == "OP-0001"
    assert question.resolved_at is not None
    assert question.reply is not None
    assert question.reply.principal == "OP-0001"
    assert question.reply.text.splitlines() == [
        "Answered when CAM-0001 converged: Order holds across restarts",
        f"Synthesis: {synthesis_card.artifact_ref} ({synthesis_card.digest})",
        f"Step 1: {survey_card.artifact_ref} ({survey_card.digest})",
    ]


def test_a_budget_stopped_campaign_leaves_its_questions_open(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    budget = {"axes": [{"axis_kind": "rounds", "limit": 1, "unit": "rounds"}]}

    view = _drive(canary, tmp_path, depth="medium", agents=1, budget=budget)

    assert view.stop is not None
    assert view.stop.reason == "budget_exhausted"
    statuses = {q.status for q in _questions(canary).values() if q.scope_ref == view.campaign_ref}
    assert statuses == {OpenQuestionStatus.OPEN}


def test_a_cancelled_campaign_leaves_its_questions_open(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    async def body() -> dict[str, Any]:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="medium", drive=False)
            )
            record = started["record"]
            return await client.call(
                "runtime.campaign.close",
                repo_root=str(canary.root),
                actor="OP-0001",
                urn=record["urn"],
                expected_revision=record["revision"],
                to_status="cancelled",
                reason="no longer needed",
            )

    closed = asyncio.run(body())

    assert closed["record"]["status"] == "cancelled"
    assert agent.assignments == []
    assert [q.status for q in _questions(canary).values()] == [OpenQuestionStatus.OPEN]


def test_answers_only_open_questions_of_a_campaign_with_a_cited_synthesis(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    view = _drive(canary, tmp_path, depth="medium", agents=1)
    campaign = _campaign(canary)
    path = ledger_path(document_path(canary), Epoch2Collection.ARTIFACT)
    artifacts = stored_revisions(r.payload for r in effective_records(read_ledger_records(path)))
    reopened = {
        key: q.model_copy(
            update={
                "status": OpenQuestionStatus.OPEN,
                "reply": None,
                "resolution_actor": None,
                "resolved_at": None,
            }
        )
        for key, q in _questions(canary).items()
    }
    unproduced = campaign.model_copy(
        update={
            "plan_steps": tuple(
                s.model_copy(update={"produced": ()}) if s.method.value == "synthesis" else s
                for s in campaign.plan_steps
            )
        }
    )
    stranger = campaign.model_copy(update={"urn": view.steps[0].step.run_refs[0]})

    [answer] = answered_questions(campaign, reopened, artifacts, actor="OP-0001", now=AT)
    assert (answer.status, answer.revision) == (OpenQuestionStatus.ANSWERED, 3)
    assert answered_questions(campaign, {}, artifacts, actor="OP-0001", now=AT) == ()
    assert answered_questions(campaign, reopened, {}, actor="OP-0001", now=AT) == ()
    assert answered_questions(unproduced, reopened, artifacts, actor="OP-0001", now=AT) == ()
    assert answered_questions(stranger, reopened, artifacts, actor="OP-0001", now=AT) == ()
    answered = _questions(canary)
    assert answered_questions(campaign, answered, artifacts, actor="OP-0001", now=AT) == ()
