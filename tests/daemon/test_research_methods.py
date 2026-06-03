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
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.research import create_campaign, persist_campaign

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
