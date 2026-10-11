"""A drive of an approved Campaign routes its rounds at the depth its plan was drawn at.

The Campaign is planned with an explicit depth and not driven, the repository's
default depth is set to another, and ``runtime.campaign.run`` then starts the drive:
the research agent is asked for at the planned depth, read back from the plan's
methods, never at the repository default.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.kernel.spec.research import ResearchDepth
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import campaign_run
from eawf.runtime.daemon.methods.campaign_run import CAMPAIGN_RUN_METHOD, CAMPAIGN_START_METHOD
from tests.integration.runtime.daemon.methods.test_campaign_run import (
    StubAgent,
    _settled,
    _start,
    canary,
    isolated,
    served,
)

pytestmark = pytest.mark.integration

__all__ = ["canary", "isolated"]


def _default_depth(canary: CanaryProvision, depth: str) -> None:
    config = canary.root / ".ea" / "config.yaml"
    layers = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else {}
    layers = {**(layers or {}), "research": {"default_depth": depth}}
    config.write_text(yaml.safe_dump(layers), encoding="utf-8")


def _driven_depths(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planned: str
) -> list[ResearchDepth]:
    asked: list[ResearchDepth] = []
    stub = StubAgent()

    def agent_for(_runtime: str, _context: Any, depth: ResearchDepth) -> StubAgent:
        asked.append(depth)
        return stub

    monkeypatch.setattr(campaign_run, "research_agent_for", agent_for)

    async def body() -> None:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth=planned, drive=False)
            )
            key = started["record"]["key"]
            await client.call(
                CAMPAIGN_RUN_METHOD, repo_root=str(canary.root), actor="OP-0001", campaign_key=key
            )
            await _settled(client, canary.root, key)

    asyncio.run(body())
    return asked


@pytest.mark.parametrize(
    ("planned", "default", "routed"),
    [
        ("exhaustive", "shallow", ResearchDepth.EXHAUSTIVE),
        ("deep", "shallow", ResearchDepth.DEEP),
        ("shallow", "deep", ResearchDepth.MEDIUM),
        ("shallow", "shallow", ResearchDepth.SHALLOW),
        ("medium", "medium", ResearchDepth.MEDIUM),
    ],
)
def test_a_run_routes_rounds_at_the_planned_depth_not_the_repository_default(
    canary: CanaryProvision,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    planned: str,
    default: str,
    routed: ResearchDepth,
) -> None:
    _default_depth(canary, default)

    assert _driven_depths(canary, tmp_path, monkeypatch, planned) == [routed]
