"""Tests for the operator-channel RPCs + channel fold.

Covers the steer / broadcast / override channels that append typed
:class:`~eawf.kernel.spec.operator_input.OperatorInput` rows to the daemon-owned
append-log, and the fold that turns the log into the round-loop decisions:

* :func:`~eawf.runtime.daemon.methods.research.steer` /
  :func:`~eawf.runtime.daemon.methods.research.broadcast` /
  :func:`~eawf.runtime.daemon.methods.research.override` each append one row.
* :func:`~eawf.runtime.daemon.methods.research.fold_operator_channel` folds the
  log honoring D-2 (blocking-only interrupt) + D-3 (persist-locked override).
* A mid-run steer changes the next round's recorded dispatch set (the steer
  note surfaces on the next round's :class:`ResearchRoundPayload`).

The handlers are driven through the module-level coroutines against an on-disk
state fixture, matching the in-process harness in
:mod:`tests.daemon.test_research_methods`.
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
    broadcast,
    create_campaign,
    fold_operator_channel,
    override,
    read_campaign_rounds,
    read_operator_inputs,
    run_campaign,
    steer,
)

pytestmark = pytest.mark.unit


def _build_ctx(state_path: Path | None) -> MethodContext:
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
        domains={"market-structure": ResearchDomainConfig(focus="venues")},
    )


def _stage_params(campaign_id: str) -> dict[str, Any]:
    block = _block()
    campaign = stage_campaign("options-pricing landscape", block)
    return {
        "campaign_id": campaign_id,
        "config": block.model_dump(mode="json"),
        "campaign": campaign.model_dump(mode="json"),
    }


def _produce(dispatch: StagedDispatch) -> Mapping[str, object]:
    return {
        "role": "researcher",
        "verdict": "pass",
        "confidence": "medium",
        "summary": f"surveyed {dispatch.domain}",
        "question": f"what does {dispatch.domain} reveal",
        "findings": [f"{dispatch.domain}-claim"],
        "recommendation": f"pursue {dispatch.domain}",
        "evidence_refs": [{"kind": "store_record", "ref": "src/x.py:1"}],
    }


# --------------------------------------------------------------------------
# steer / broadcast / override -- append typed OperatorInput rows
# --------------------------------------------------------------------------


def test_steer_appends_operator_input(tmp_path: Path) -> None:
    """A steer appends a non-blocking steer OperatorInput to the channel log."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-st"))
        result = await steer(ctx, {"text": "prioritise venues", "campaign_id": "campaign-st"})
        assert result["kind"] == "steer"
        assert result["blocking"] is False
        inputs = read_operator_inputs(state_path, "campaign-st")
        assert len(inputs) == 1
        assert inputs[0].note == "prioritise venues"
        assert inputs[0].kind.value == "steer"

    _run(body)


def test_broadcast_appends_notice(tmp_path: Path) -> None:
    """A broadcast appends a notice-broadcast input (no payload)."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-bc"))
        result = await broadcast(ctx, {"notice": "hold synthesis", "campaign_id": "campaign-bc"})
        assert result["kind"] == "notice-broadcast"
        inputs = read_operator_inputs(state_path, "campaign-bc")
        assert inputs[0].payload is None
        assert inputs[0].note == "hold synthesis"

    _run(body)


def test_override_appends_locked_blocking_input(tmp_path: Path) -> None:
    """An override appends a locked, blocking override input by default."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-ov"))
        result = await override(
            ctx, {"verdict": "accept the stronger claim", "campaign_id": "campaign-ov"}
        )
        assert result["kind"] == "override"
        assert result["blocking"] is True  # default urgency=urgent blocks (D-2)
        inputs = read_operator_inputs(state_path, "campaign-ov")
        assert inputs[0].locks_override is True  # default locked=True (D-3)

    _run(body)


def test_channel_rpc_rejects_unknown_campaign(tmp_path: Path) -> None:
    """A channel input against an unstaged campaign is rejected."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown campaign"):
            await steer(ctx, {"text": "x", "campaign_id": "campaign-ghost"})

    _run(body)


def test_channel_rpc_rejects_missing_campaign_id(tmp_path: Path) -> None:
    """A channel input with no campaign id is rejected."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="requires a campaign_id"):
            await steer(ctx, {"text": "x"})

    _run(body)


# --------------------------------------------------------------------------
# fold_operator_channel -- D-2 blocking-only + D-3 persist-locked
# --------------------------------------------------------------------------


def test_fold_partitions_blocking_and_queued(tmp_path: Path) -> None:
    """The fold partitions a blocking override from a non-blocking steer (D-2)."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-fold"))
        await steer(ctx, {"text": "narrow venues", "campaign_id": "campaign-fold"})
        await override(ctx, {"verdict": "pin model A", "campaign_id": "campaign-fold"})
        fold = fold_operator_channel(state_path, "campaign-fold")
        assert fold.paused is True  # the urgent override blocks (D-2)
        assert len(fold.blocking) == 1
        assert len(fold.queued) == 1  # the steer is queued, not blocking
        # The locked override is effective (D-3).
        assert len(fold.effective_overrides) == 1
        assert fold.effective_overrides[0].value == "pin model A"

    _run(body)


def test_fold_later_unlocked_override_clears_lock(tmp_path: Path) -> None:
    """A later unlocked override on the same scope clears the lock (D-3 clear)."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-clear"))
        await override(
            ctx,
            {
                "verdict": "pin A",
                "campaign_id": "campaign-clear",
                "scope": "topic:x",
                "locked": True,
            },
        )
        await override(
            ctx,
            {
                "verdict": "unpin",
                "campaign_id": "campaign-clear",
                "scope": "topic:x",
                "locked": False,
            },
        )
        fold = fold_operator_channel(state_path, "campaign-clear")
        # The latest override on topic:x is unlocked -> no effective override.
        assert fold.effective_overrides == ()

    _run(body)


# --------------------------------------------------------------------------
# mid-run steer changes the next round's recorded dispatch set
# --------------------------------------------------------------------------


def test_mid_run_steer_surfaces_on_next_round(tmp_path: Path) -> None:
    """A steer pushed before a run surfaces on the round's steer_notes."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-steered"))
        await steer(ctx, {"text": "focus the venues domain", "campaign_id": "campaign-steered"})
        run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-steered", round_budget=2),
            produce_agent_end=_produce,
        )
        rounds = read_campaign_rounds(state_path, "campaign-steered")
        assert rounds
        # The queued steer note shaped every round's recorded dispatch set.
        assert "focus the venues domain" in rounds[0].steer_notes

    _run(body)
