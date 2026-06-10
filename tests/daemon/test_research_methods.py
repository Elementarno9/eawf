"""Tests for the ``research.*`` JSON-RPC handlers (P29-I09-W07).

Covers :func:`eawf.runtime.daemon.methods.research.create_campaign` plus the
shared :func:`persist_campaign` helper. The handler is driven directly through
the module-level coroutine -- the JSON-RPC framing is exercised in
:mod:`tests.daemon.test_scaffolding`; routing through a live daemon is out of
scope here.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf import __version__
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    stage_campaign,
)
from eawf.kernel.state.enums import CampaignStatus, StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.research import (
    cancel_campaign,
    create_campaign,
    persist_campaign,
    read_latest_campaign,
    stage_campaign_method,
)

pytestmark = pytest.mark.unit


def _build_ctx(*, state_path: Path | None = None) -> MethodContext:
    return MethodContext(
        started_at="2026-06-03T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        state_path=state_path,
    )


def _run(body: Callable[[], Awaitable[None]]) -> None:
    """Run an async test body without ``pytest-asyncio``."""
    asyncio.run(body())


def _block() -> ResearchProfileBlock:
    """A two-domain research block (one focus override, one depth override)."""
    return ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(focus="venues + flow"),
            "pricing-models": ResearchDomainConfig(depth=ResearchDepth.DEEP),
        },
    )


def _params(campaign_id: str = "campaign-abc") -> dict[str, Any]:
    """Build a valid ``research.create_campaign`` params dict."""
    block = _block()
    campaign = stage_campaign("Survey the options-pricing landscape", block)
    return {
        "campaign_id": campaign_id,
        "config": block.model_dump(mode="json"),
        "campaign": campaign.model_dump(mode="json"),
    }


def _read_campaign_rows(state_path: Path) -> list[ResearchCampaignPayload]:
    """Return every campaign payload off the on-disk research_campaign store."""
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    rows: list[ResearchCampaignPayload] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = Envelope.model_validate_json(line)
        assert envelope.kind is StoreKind.RESEARCH_CAMPAIGN
        rows.append(ResearchCampaignPayload.model_validate(envelope.payload))
    return rows


# --------------------------------------------------------------------------
# create_campaign -- the happy path lands the row with the right payload
# --------------------------------------------------------------------------


def test_create_campaign_appends_row_with_payload(tmp_path: Path) -> None:
    """A valid campaign lands one row in the research_campaign store."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await create_campaign(ctx, _params("campaign-abc"))
        assert result["id"] == "campaign-abc"
        assert isinstance(result["appended_at"], str)
        rows = _read_campaign_rows(state_path)
        assert len(rows) == 1
        payload = rows[0]
        assert payload.campaign_id == "campaign-abc"
        assert payload.campaign.topic == "Survey the options-pricing landscape"
        assert payload.campaign.spawned is False
        domains = [dispatch.domain for dispatch in payload.campaign.dispatches]
        assert domains == ["market-structure", "pricing-models"]
        assert payload.config.default_depth is ResearchDepth.MEDIUM

    _run(body)


def test_create_campaign_appends_are_cumulative(tmp_path: Path) -> None:
    """Two campaigns append two rows (the store is append-only)."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        await create_campaign(ctx, _params("campaign-one"))
        await create_campaign(ctx, _params("campaign-two"))
        rows = _read_campaign_rows(state_path)
        assert [row.campaign_id for row in rows] == ["campaign-one", "campaign-two"]

    _run(body)


# --------------------------------------------------------------------------
# create_campaign -- error paths (extra param, empty id, missing state_path)
# --------------------------------------------------------------------------


def test_create_campaign_rejects_extra_param(tmp_path: Path) -> None:
    """An unknown param is rejected by ``extra='forbid'`` on the params model."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)
    params = _params()
    params["rogue"] = True

    async def body() -> None:
        with pytest.raises(ValidationError):
            await create_campaign(ctx, params)

    _run(body)


def test_create_campaign_rejects_empty_campaign_id(tmp_path: Path) -> None:
    """An empty campaign id violates the payload's ``min_length=1`` bound."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)
    params = _params(campaign_id="")

    async def body() -> None:
        with pytest.raises(ValidationError):
            await create_campaign(ctx, params)

    _run(body)


def test_create_campaign_raises_without_state_path() -> None:
    """The handler raises when the daemon context has no state path."""
    ctx = _build_ctx(state_path=None)

    async def body() -> None:
        with pytest.raises(RuntimeError, match="state_path not configured"):
            await create_campaign(ctx, _params())

    _run(body)


def test_create_campaign_no_store_write_when_state_path_missing(tmp_path: Path) -> None:
    """A missing state path fails before any store row is written."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=None)

    async def body() -> None:
        with pytest.raises(RuntimeError):
            await create_campaign(ctx, _params())

    _run(body)
    assert not store_path(state_path, StoreKind.RESEARCH_CAMPAIGN).exists()


# --------------------------------------------------------------------------
# stage_campaign -- stage from topic + block, then append one row
# --------------------------------------------------------------------------


def test_stage_campaign_appends_one_row_sorted_dispatches(tmp_path: Path) -> None:
    """A topic + two-domain block stages one row with two sorted dispatches."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)
    params = {
        "topic": "Survey the options-pricing landscape",
        "config": _block().model_dump(mode="json"),
        "campaign_id": "campaign-staged",
    }

    async def body() -> None:
        result: dict[str, Any] = await stage_campaign_method(ctx, params)
        assert result["id"] == "campaign-staged"
        assert result["campaign_id"] == "campaign-staged"
        assert result["topic"] == "Survey the options-pricing landscape"
        assert result["domain_count"] == 2
        assert isinstance(result["appended_at"], str)
        rows = _read_campaign_rows(state_path)
        assert len(rows) == 1
        domains = [dispatch.domain for dispatch in rows[0].campaign.dispatches]
        assert domains == ["market-structure", "pricing-models"]
        assert rows[0].campaign.spawned is False

    _run(body)


def test_stage_campaign_allocates_id_when_absent(tmp_path: Path) -> None:
    """A missing campaign_id allocates a fresh ``campaign-<hex>`` id."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)
    params = {
        "topic": "liquidity regimes",
        "config": _block().model_dump(mode="json"),
    }

    async def body() -> None:
        result: dict[str, Any] = await stage_campaign_method(ctx, params)
        assert result["campaign_id"].startswith("campaign-")
        rows = _read_campaign_rows(state_path)
        assert len(rows) == 1
        assert rows[0].campaign_id == result["campaign_id"]

    _run(body)


def test_stage_campaign_rejects_empty_topic(tmp_path: Path) -> None:
    """An empty topic is rejected (the -32602 invalid-params ValueError path)."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)
    params = {"topic": "", "config": _block().model_dump(mode="json")}

    async def body() -> None:
        with pytest.raises(ValueError, match="campaign topic must be non-empty"):
            await stage_campaign_method(ctx, params)

    _run(body)
    assert not store_path(state_path, StoreKind.RESEARCH_CAMPAIGN).exists()


def test_stage_campaign_rejects_whitespace_topic(tmp_path: Path) -> None:
    """A whitespace-only topic maps to the -32602 invalid-params ValueError path."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)
    params = {"topic": "   ", "config": _block().model_dump(mode="json")}

    async def body() -> None:
        with pytest.raises(ValueError, match="campaign topic must be non-empty"):
            await stage_campaign_method(ctx, params)

    _run(body)
    assert not store_path(state_path, StoreKind.RESEARCH_CAMPAIGN).exists()


def test_stage_campaign_rejects_extra_param(tmp_path: Path) -> None:
    """An unknown param is rejected by extra='forbid' on the params model."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)
    params = {"topic": "topic", "config": _block().model_dump(mode="json"), "rogue": True}

    async def body() -> None:
        with pytest.raises(ValidationError):
            await stage_campaign_method(ctx, params)

    _run(body)


def test_stage_campaign_raises_without_state_path() -> None:
    """The handler raises when the daemon context has no state path."""
    ctx = _build_ctx(state_path=None)
    params = {"topic": "topic", "config": _block().model_dump(mode="json")}

    async def body() -> None:
        with pytest.raises(RuntimeError, match="state_path not configured"):
            await stage_campaign_method(ctx, params)

    _run(body)


# --------------------------------------------------------------------------
# persist_campaign -- the shared helper (the RPC + the CLI fallback share it)
# --------------------------------------------------------------------------


def test_persist_campaign_returns_envelope_id(tmp_path: Path) -> None:
    """The shared helper appends a row and returns the campaign id."""
    state_path = tmp_path / "state.json"
    block = _block()
    payload = ResearchCampaignPayload(
        campaign_id="campaign-shared",
        config=block,
        campaign=stage_campaign("shared helper topic", block),
    )
    appended_id = persist_campaign(state_path, payload)
    assert appended_id == "campaign-shared"
    rows = _read_campaign_rows(state_path)
    assert len(rows) == 1
    assert rows[0].campaign_id == "campaign-shared"
    assert rows[0].campaign.topic == "shared helper topic"


def test_persist_campaign_empty_block_stages_zero_dispatches(tmp_path: Path) -> None:
    """A block with no domains persists a zero-dispatch campaign (boundary)."""
    state_path = tmp_path / "state.json"
    block = ResearchProfileBlock()
    payload = ResearchCampaignPayload(
        campaign_id="campaign-empty",
        config=block,
        campaign=stage_campaign("empty campaign topic", block),
    )
    persist_campaign(state_path, payload)
    rows = _read_campaign_rows(state_path)
    assert len(rows) == 1
    assert rows[0].campaign.dispatches == []


# --------------------------------------------------------------------------
# read_latest_campaign -- latest matching row wins
# --------------------------------------------------------------------------


def test_read_latest_campaign_none_when_store_absent(tmp_path: Path) -> None:
    """No campaign store on disk yields None (the common pre-stage path)."""
    assert read_latest_campaign(tmp_path / "state.json", "campaign-x") is None


def test_read_latest_campaign_none_for_unknown_id(tmp_path: Path) -> None:
    """A stored campaign whose id does not match yields None."""
    state_path = tmp_path / "state.json"

    async def body() -> None:
        ctx = _build_ctx(state_path=state_path)
        await create_campaign(ctx, _params("campaign-a"))

    _run(body)
    assert read_latest_campaign(state_path, "campaign-missing") is None


def test_read_latest_campaign_returns_last_matching_row(tmp_path: Path) -> None:
    """A re-appended campaign resolves to its most-recent payload."""
    state_path = tmp_path / "state.json"
    block = _block()
    first = ResearchCampaignPayload(
        campaign_id="campaign-dup",
        config=block,
        campaign=stage_campaign("first topic", block),
    )
    second = ResearchCampaignPayload(
        campaign_id="campaign-dup",
        config=block,
        campaign=stage_campaign("second topic", block),
    )
    persist_campaign(state_path, first)
    persist_campaign(state_path, second)
    latest = read_latest_campaign(state_path, "campaign-dup")
    assert latest is not None
    assert latest.campaign.topic == "second topic"


# --------------------------------------------------------------------------
# cancel_campaign -- happy path tombstones the campaign
# --------------------------------------------------------------------------


def test_cancel_campaign_appends_tombstoned_row(tmp_path: Path) -> None:
    """Cancelling an active campaign appends a cancelled copy with a tombstone."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        await create_campaign(ctx, _params("campaign-c"))
        result: dict[str, Any] = await cancel_campaign(
            ctx, {"campaign_id": "campaign-c", "reason": "superseded"}
        )
        assert result["id"] == "campaign-c"
        assert result["status"] == "cancelled"
        assert isinstance(result["cancelled_at"], str)
        latest = read_latest_campaign(state_path, "campaign-c")
        assert latest is not None
        assert latest.status is CampaignStatus.CANCELLED
        assert latest.tombstone is not None
        assert latest.tombstone.reason == "superseded"
        # The original active row stays in the append-only store.
        rows = _read_campaign_rows(state_path)
        assert len(rows) == 2
        assert rows[0].status is CampaignStatus.ACTIVE
        assert rows[1].status is CampaignStatus.CANCELLED

    _run(body)


def test_cancel_campaign_reason_optional(tmp_path: Path) -> None:
    """Cancelling with no reason records a tombstone with reason None."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        await create_campaign(ctx, _params("campaign-noreason"))
        await cancel_campaign(ctx, {"campaign_id": "campaign-noreason"})
        latest = read_latest_campaign(state_path, "campaign-noreason")
        assert latest is not None
        assert latest.status is CampaignStatus.CANCELLED
        assert latest.tombstone is not None
        assert latest.tombstone.reason is None

    _run(body)


# --------------------------------------------------------------------------
# cancel_campaign -- error paths
# --------------------------------------------------------------------------


def test_cancel_campaign_rejects_unknown_id(tmp_path: Path) -> None:
    """Cancelling a campaign id that was never staged is rejected."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown campaign"):
            await cancel_campaign(ctx, {"campaign_id": "campaign-ghost"})

    _run(body)


def test_cancel_campaign_rejects_already_cancelled(tmp_path: Path) -> None:
    """Cancelling an already-cancelled campaign is rejected (not re-tombstoned)."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        await create_campaign(ctx, _params("campaign-twice"))
        await cancel_campaign(ctx, {"campaign_id": "campaign-twice"})
        with pytest.raises(ValueError, match="not active"):
            await cancel_campaign(ctx, {"campaign_id": "campaign-twice"})

    _run(body)


def test_cancel_campaign_rejects_extra_param(tmp_path: Path) -> None:
    """An unknown param is rejected by extra='forbid' on the params model."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        await create_campaign(ctx, _params("campaign-extra"))
        with pytest.raises(ValidationError):
            await cancel_campaign(ctx, {"campaign_id": "campaign-extra", "rogue": True})

    _run(body)


def test_cancel_campaign_rejects_empty_id(tmp_path: Path) -> None:
    """An empty campaign id violates the params min_length bound."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await cancel_campaign(ctx, {"campaign_id": ""})

    _run(body)


def test_cancel_campaign_raises_without_state_path() -> None:
    """The handler raises when the daemon context has no state path."""
    ctx = _build_ctx(state_path=None)

    async def body() -> None:
        with pytest.raises(RuntimeError, match="state_path not configured"):
            await cancel_campaign(ctx, {"campaign_id": "campaign-x"})

    _run(body)
