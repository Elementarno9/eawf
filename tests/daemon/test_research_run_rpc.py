"""Tests for research.run + research.followup + research.snapshot (P30-I18-W03).

Covers the campaign-run RPC surface that drives the W01 round runner over a
persisted staged campaign:

* :func:`~eawf.runtime.daemon.methods.research.run_campaign` drives the bounded
  loop with a stubbed per-dispatch spawner, persists one round record per
  executed round, and respects the round budget + saturation halt.
* :func:`~eawf.runtime.daemon.methods.research.followup` reports the rounds run
  + the next round number.
* :func:`~eawf.runtime.daemon.methods.research.snapshot` folds the persisted
  campaign + rounds into a typed run summary.

The live ``agent.dispatch`` spawn is replaced by a fixture ``agent_end``
producer, mirroring how :mod:`tests.daemon.test_fleet_drive` injects a spawner;
no live campaign is run.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from eawf import __version__
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    StagedDispatch,
    stage_campaign,
)
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.research import (
    RunCampaignParams,
    create_campaign,
    followup,
    read_campaign_rounds,
    run_campaign,
    snapshot,
)

pytestmark = pytest.mark.unit


def _build_ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        state_path=state_path,
    )


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def _block() -> ResearchProfileBlock:
    return ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(focus="venues + flow"),
            "pricing-models": ResearchDomainConfig(depth=ResearchDepth.DEEP),
        },
    )


def _stage_params(campaign_id: str) -> dict[str, Any]:
    block = _block()
    campaign = stage_campaign("options-pricing landscape", block)
    return {
        "campaign_id": campaign_id,
        "config": block.model_dump(mode="json"),
        "campaign": campaign.model_dump(mode="json"),
    }


def _agent_end_body(domain: str, *, findings: list[str]) -> dict[str, object]:
    return {
        "role": "researcher",
        "verdict": "pass",
        "confidence": "medium",
        "summary": f"surveyed {domain}",
        "question": f"what does {domain} reveal",
        "findings": findings,
        "recommendation": f"pursue {domain}",
        "evidence_refs": [{"kind": "store_record", "ref": f"src/{domain}.py:1"}],
    }


def _produce_findings(dispatch: StagedDispatch) -> Mapping[str, object]:
    """A stub producer: each dispatch returns one finding line (never dry)."""
    return _agent_end_body(dispatch.domain, findings=[f"{dispatch.domain}-claim"])


def _produce_empty(dispatch: StagedDispatch) -> Mapping[str, object]:
    """A stub producer that returns no findings -> the round saturates."""
    return _agent_end_body(dispatch.domain, findings=[])


# --------------------------------------------------------------------------
# run_campaign -- drives the bounded loop, persists each round
# --------------------------------------------------------------------------


def test_run_campaign_persists_each_round_to_budget(tmp_path: Path) -> None:
    """A never-dry run executes round_budget rounds and persists each one."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-run"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-run", round_budget=3),
            produce_agent_end=_produce_findings,
        )
        assert result["rounds_run"] == 3
        assert result["halt_reason"] == "round_budget"
        assert result["saturated"] is False
        # Two dispatches per round x 3 rounds = 6 claims.
        assert len(result["claim_ids"]) == 6
        rounds = read_campaign_rounds(state_path, "campaign-run")
        assert [r.round_number for r in rounds] == [1, 2, 3]
        assert rounds[0].finding_lines == ["market-structure-claim", "pricing-models-claim"]

    _run(body)


def test_run_campaign_halts_on_saturation(tmp_path: Path) -> None:
    """A round returning no findings saturates and halts the loop early."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-dry"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-dry", round_budget=5),
            produce_agent_end=_produce_empty,
        )
        assert result["rounds_run"] == 1
        assert result["halt_reason"] == "saturated"
        assert result["saturated"] is True
        rounds = read_campaign_rounds(state_path, "campaign-dry")
        assert len(rounds) == 1
        assert rounds[0].saturated is True

    _run(body)


def test_run_campaign_rejects_unknown_campaign(tmp_path: Path) -> None:
    """Running an unstaged campaign id is rejected."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)
    with pytest.raises(ValueError, match="unknown campaign"):
        run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-ghost"),
            produce_agent_end=_produce_findings,
        )


def test_run_campaign_raises_without_state_path() -> None:
    """run_campaign raises when the daemon context has no state path."""
    ctx = _build_ctx(state_path=None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="state_path not configured"):
        run_campaign(
            ctx,
            RunCampaignParams(campaign_id="x"),
            produce_agent_end=_produce_findings,
        )


# --------------------------------------------------------------------------
# research.followup -- reports rounds run + next round
# --------------------------------------------------------------------------


def test_followup_reports_next_round(tmp_path: Path) -> None:
    """Follow-up names the round a follow-up run would start at."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-fu"))
        run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-fu", round_budget=2),
            produce_agent_end=_produce_findings,
        )
        result = await followup(ctx, {"campaign_id": "campaign-fu", "note": "dig deeper"})
        assert result["rounds_run"] == 2
        assert result["next_round"] == 3
        assert result["note"] == "dig deeper"

    _run(body)


def test_followup_rejects_unknown_campaign(tmp_path: Path) -> None:
    """Follow-up on an unknown campaign is rejected."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown campaign"):
            await followup(ctx, {"campaign_id": "campaign-missing"})

    _run(body)


# --------------------------------------------------------------------------
# research.snapshot -- typed run summary
# --------------------------------------------------------------------------


def test_snapshot_summarises_run(tmp_path: Path) -> None:
    """Snapshot folds the campaign + rounds into a typed run summary."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-snap"))
        run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-snap", round_budget=2),
            produce_agent_end=_produce_findings,
        )
        result = await snapshot(ctx, {"campaign_id": "campaign-snap"})
        assert result["campaign_id"] == "campaign-snap"
        assert result["status"] == "active"
        assert result["topic"] == "options-pricing landscape"
        assert result["rounds_run"] == 2
        assert result["total_findings"] == 4  # 2 dispatches x 2 rounds
        assert result["total_claims"] == 4
        assert result["checkpoints"] >= 1  # ON_HALT records the terminal round

    _run(body)


def test_snapshot_pre_run_reports_zero_rounds(tmp_path: Path) -> None:
    """A staged-but-not-run campaign snapshots zero rounds."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-staged"))
        result = await snapshot(ctx, {"campaign_id": "campaign-staged"})
        assert result["rounds_run"] == 0
        assert result["saturated"] is False
        assert result["total_findings"] == 0

    _run(body)


def test_snapshot_rejects_unknown_campaign(tmp_path: Path) -> None:
    """Snapshot on an unknown campaign is rejected."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown campaign"):
            await snapshot(ctx, {"campaign_id": "campaign-nope"})

    _run(body)
