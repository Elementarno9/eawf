"""A Campaign round's metered usage lands on its Run, its report, its step and its budget.

The round is driven end to end on the native verbs through a fake driver that hands
its usage readings to the launch request's sink, as the official drivers do. Each
reading is stated as ``usage_observed`` on the round's own Run, so ``eawf run report``
answers from the Run's stream, the Campaign view states each step's tokens, and a
Campaign that bounds tokens is charged them. A round whose driver metered nothing
says so rather than reading as zero.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.campaign import CampaignView
from eawf.kernel.runtime.events import RunEventKind
from eawf.kernel.runtime.usage import UsagePayload
from eawf.kernel.state.enums import EffortBucket
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.reflect.run_report import ReportPartName, plan_run_report
from eawf.observability.reflect.runs import read_tree_run
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import campaign_run
from eawf.runtime.daemon.methods.campaign_run import CAMPAIGN_START_METHOD, HostResearchAgent
from eawf.runtime.daemon.spend import run_usage
from eawf.runtime.runtimes.metering import UsageSample
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import document_path
from tests.integration.runtime.daemon.methods.test_campaign_run import (
    _settled,
    _start,
    served,
)
from tests.integration.runtime.daemon.test_campaign_round_driver import (
    FakeDriver,
    _isolated,
    canary,
)

pytestmark = pytest.mark.integration

__all__ = ["_isolated", "canary"]

#: Two running totals of one turn: the fold keeps the larger, never their sum.
READINGS = (
    UsageSample(input_tokens=100, output_tokens=20),
    UsageSample(input_tokens=120, output_tokens=30, cache_read_input_tokens=7),
)


def _drive(
    canary: CanaryProvision,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    driver: FakeDriver,
    budget: dict[str, Any] | None = None,
) -> CampaignView:
    def agent_for(runtime: str, context: Epoch2RootContext, _depth: Any) -> HostResearchAgent:
        return HostResearchAgent(runtime, context, EffortBucket.S, launchers={"claude": driver})

    monkeypatch.setattr(campaign_run, "research_agent_for", agent_for)
    brief: dict[str, Any] = {"depth": "shallow", "agents": 1}
    if budget is not None:
        brief["budget"] = budget

    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(CAMPAIGN_START_METHOD, **_start(canary.root, **brief))
            return await _settled(client, canary.root, started["record"]["key"])

    return asyncio.run(body())


def _usage_events(canary: CanaryProvision, key: str) -> list[UsagePayload]:
    reading = read_tree_run(canary.root / ".ea", key)
    return [
        event.payload
        for event in reading.events
        if event.event_kind is RunEventKind.USAGE_OBSERVED
        and isinstance(event.payload, UsagePayload)
    ]


def _usage_lines(canary: CanaryProvision, key: str) -> tuple[str, ...]:
    plan = plan_run_report(
        read_tree_run(canary.root / ".ea", key),
        parts=(ReportPartName.USAGE_AND_COST,),
        actor="OP-0001",
        tree_root=canary.root / ".ea",
        on=date(2026, 10, 8),
    )
    [part] = [p for p in plan.parts if p.name is ReportPartName.USAGE_AND_COST]
    return part.lines


def test_a_metered_round_states_usage_on_its_run_and_charges_its_step_and_budget(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = {
        "axes": [
            {"axis_kind": "rounds", "limit": 2, "unit": "rounds"},
            {"axis_kind": "tokens", "limit": 10_000, "unit": "tokens"},
        ]
    }

    view = _drive(canary, tmp_path, monkeypatch, FakeDriver("claude", usage=READINGS), budget)

    assert [step.state for step in view.steps] == ["done", "done"]
    for step in view.steps:
        key = step.step.run_refs[-1].entity_key
        events = _usage_events(canary, key)
        assert [(e.input_tokens, e.output_tokens, e.cache_tokens) for e in events] == [
            (100, 20, 0),
            (120, 30, 7),
        ]
        assert all(e.is_cumulative and e.measurement_quality == "measured" for e in events)
        lines = _usage_lines(canary, key)
        assert "usage_and_cost input_tokens 120" in lines
        assert "usage_and_cost output_tokens 30" in lines
        assert not any("unavailable" in line for line in lines)
        assert step.tokens == 150
        axis = step.step.bound.axis("tokens")
        assert axis is not None
        assert (axis.spent, axis.spent_quality) == (150, "measured")
    total = view.evidence_budget.axis("tokens")
    assert total is not None
    assert total.spent == 300


def test_a_round_with_no_reading_states_unavailable_never_zero(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = {
        "axes": [
            {"axis_kind": "rounds", "limit": 2, "unit": "rounds"},
            {"axis_kind": "tokens", "limit": 10_000, "unit": "tokens"},
        ]
    }

    view = _drive(canary, tmp_path, monkeypatch, FakeDriver("claude", usage=()), budget)

    for step in view.steps:
        key = step.step.run_refs[-1].entity_key
        assert _usage_events(canary, key) == []
        assert _usage_lines(canary, key) == (
            "usage_and_cost unavailable: the Run holds no captured runtime",
        )
        assert step.tokens is None
        axis = step.step.bound.axis("tokens")
        assert axis is not None
        assert (axis.spent, axis.spent_quality) == (0, "unavailable")


def test_the_view_states_step_tokens_with_no_token_axis(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _drive(canary, tmp_path, monkeypatch, FakeDriver("claude", usage=READINGS[:1]))

    assert [axis.axis_kind for axis in view.evidence_budget.axes] == ["rounds"]
    assert [step.tokens for step in view.steps] == [120, 120]


def test_run_usage_folds_each_run_and_leaves_out_a_run_with_no_reading(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _drive(canary, tmp_path, monkeypatch, FakeDriver("claude", usage=READINGS))
    records = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))
    [first, second] = [step.step.run_refs[-1] for step in view.steps]

    assert run_usage(records, ()) == {}
    assert run_usage((), (first,)) == {}
    totals = run_usage(records, (first, second))
    assert set(totals) == {first.entity_key, second.entity_key}
    folded = totals[first.entity_key]
    assert (folded.input_tokens, folded.output_tokens, folded.tokens) == (120, 30, 157)
