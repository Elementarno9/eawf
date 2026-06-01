"""Tests for the ``research:`` profile block + the plan-only Level-1 runner.

Pins the P29-I01-W14 contract:

- The per-domain ``research:`` config (:class:`ResearchProfileBlock` +
  :class:`ResearchDomainConfig`) parses with ``extra="forbid"`` and
  composes with the canonical :class:`ResearchDepth` ladder.
- The Level-1 runner :func:`stage_campaign` stages a campaign WITHOUT a
  live spawn: it imports no adapter, opens no subprocess, and the staged
  campaign's ``spawned`` flag is fixed ``False``.
- Boundary cases: an empty campaign (no domains), a single domain, and
  deterministic sorted output across insertion orders.
- Error path: an empty / whitespace topic raises ``ValueError``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.research import DEFAULT_RESEARCH_DEPTH, ResearchDepth
from eawf.kernel.spec.research_campaign import (
    DEFAULT_CAMPAIGN_ROLE,
    ResearchDomainConfig,
    ResearchProfileBlock,
    StagedCampaign,
    StagedDispatch,
    stage_campaign,
)

# --- Per-domain config parses -------------------------------------------


def test_domain_config_parses_with_depth_and_focus() -> None:
    """A per-domain config parses its depth override + focus line."""
    cfg = ResearchDomainConfig.model_validate(
        {"depth": "deep", "focus": "microstructure liquidity"}
    )
    assert cfg.depth is ResearchDepth.DEEP
    assert cfg.focus == "microstructure liquidity"
    assert cfg.agent_role == DEFAULT_CAMPAIGN_ROLE


def test_domain_config_defaults_depth_to_none() -> None:
    """An absent per-domain depth defers to the block default (None here)."""
    cfg = ResearchDomainConfig()
    assert cfg.depth is None
    assert cfg.focus is None
    assert cfg.resolved_depth(ResearchDepth.MEDIUM) is ResearchDepth.MEDIUM


def test_domain_config_resolved_depth_prefers_override() -> None:
    """A per-domain depth override wins over the block default."""
    cfg = ResearchDomainConfig(depth=ResearchDepth.EXHAUSTIVE)
    assert cfg.resolved_depth(ResearchDepth.SHALLOW) is ResearchDepth.EXHAUSTIVE


def test_domain_config_rejects_unknown_field() -> None:
    """``extra='forbid'`` rejects a typo'd per-domain key (AGENTS rule 2)."""
    with pytest.raises(ValidationError):
        ResearchDomainConfig.model_validate({"dpth": "deep"})


def test_domain_config_rejects_bad_depth_token() -> None:
    """An out-of-ladder depth token fails validation, not silently coerces."""
    with pytest.raises(ValidationError):
        ResearchDomainConfig.model_validate({"depth": "ludicrous"})


def test_profile_block_parses_per_domain_map() -> None:
    """The ``research:`` block parses a per-domain config map."""
    block = ResearchProfileBlock.model_validate(
        {
            "default_depth": "shallow",
            "domains": {
                "market-structure": {"depth": "deep"},
                "regulatory": {"focus": "EU MiFID II"},
            },
        }
    )
    assert block.default_depth is ResearchDepth.SHALLOW
    assert set(block.domains) == {"market-structure", "regulatory"}
    assert block.domains["market-structure"].depth is ResearchDepth.DEEP


def test_profile_block_default_depth_is_canonical() -> None:
    """An omitted default_depth resolves to the canonical research default."""
    block = ResearchProfileBlock()
    assert block.default_depth is DEFAULT_RESEARCH_DEPTH
    assert block.domains == {}


def test_profile_block_rejects_unknown_field() -> None:
    """``extra='forbid'`` rejects an unknown block-level key."""
    with pytest.raises(ValidationError):
        ResearchProfileBlock.model_validate({"defualt_depth": "deep"})


# --- Level-1 runner stages WITHOUT spawning -----------------------------


def test_stage_campaign_is_plan_only_not_spawned() -> None:
    """The staged campaign's ``spawned`` flag is the plan-only discriminator."""
    block = ResearchProfileBlock(domains={"a": ResearchDomainConfig()})
    campaign = stage_campaign("liquidity regimes", block)
    assert isinstance(campaign, StagedCampaign)
    assert campaign.spawned is False
    assert campaign.topic == "liquidity regimes"


def test_stage_campaign_does_not_import_spawn_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Staging never touches the live-spawn surface (no subprocess, no adapter).

    The Level-1 runner is pure: it must not import the runtime adapter
    module nor invoke ``subprocess``. We sabotage both seams and assert
    staging still succeeds, proving it reaches neither.
    """
    import subprocess

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("stage_campaign must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    block = ResearchProfileBlock(domains={"a": ResearchDomainConfig(), "b": ResearchDomainConfig()})
    campaign = stage_campaign("topic", block)

    assert campaign.domain_count == 2
    # Every staged dispatch is read-only — no execution implied.
    assert all(d.read_only is True for d in campaign.dispatches)
    # The plan-only runner does not pull the live-spawn adapter into its
    # own module namespace — staging reaches no runtime seam.
    import eawf.kernel.spec.research_campaign as campaign_mod

    assert not hasattr(campaign_mod, "subprocess")
    assert not hasattr(campaign_mod, "RuntimeAdapter")


def test_stage_campaign_one_dispatch_per_domain() -> None:
    """Each configured domain yields exactly one staged dispatch."""
    block = ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "alpha": ResearchDomainConfig(depth=ResearchDepth.DEEP),
            "beta": ResearchDomainConfig(focus="narrow scope"),
        },
    )
    campaign = stage_campaign("cross-domain sweep", block)
    assert campaign.domain_count == 2
    by_domain = {d.domain: d for d in campaign.dispatches}
    # Per-domain depth override is honoured; the other inherits the default.
    assert by_domain["alpha"].depth is ResearchDepth.DEEP
    assert by_domain["beta"].depth is ResearchDepth.MEDIUM
    # The focus line is appended to the staged prompt; the bare topic
    # stages otherwise.
    assert by_domain["beta"].prompt.endswith("Focus: narrow scope")
    assert by_domain["alpha"].prompt == "cross-domain sweep"


def test_stage_campaign_output_is_sorted_deterministic() -> None:
    """Domains stage in sorted-name order regardless of insertion order."""
    block_a = ResearchProfileBlock(
        domains={
            "zeta": ResearchDomainConfig(),
            "alpha": ResearchDomainConfig(),
            "mu": ResearchDomainConfig(),
        }
    )
    block_b = ResearchProfileBlock(
        domains={
            "alpha": ResearchDomainConfig(),
            "mu": ResearchDomainConfig(),
            "zeta": ResearchDomainConfig(),
        }
    )
    domains_a = [d.domain for d in stage_campaign("t", block_a).dispatches]
    domains_b = [d.domain for d in stage_campaign("t", block_b).dispatches]
    assert domains_a == ["alpha", "mu", "zeta"]
    assert domains_a == domains_b


# --- Boundary: empty campaign -------------------------------------------


def test_stage_campaign_empty_block_stages_no_dispatches() -> None:
    """A block with no domains stages an empty (but valid) campaign."""
    campaign = stage_campaign("topic with no domains", ResearchProfileBlock())
    assert campaign.spawned is False
    assert campaign.dispatches == []
    assert campaign.domain_count == 0


def test_stage_campaign_single_domain() -> None:
    """The single-domain boundary stages exactly one dispatch."""
    block = ResearchProfileBlock(domains={"only": ResearchDomainConfig()})
    campaign = stage_campaign("solo", block)
    assert campaign.domain_count == 1
    assert campaign.dispatches[0].domain == "only"
    assert campaign.dispatches[0].agent_role == DEFAULT_CAMPAIGN_ROLE


# --- Error path ----------------------------------------------------------


def test_stage_campaign_rejects_empty_topic() -> None:
    """An empty topic raises ValueError at the runner boundary."""
    with pytest.raises(ValueError, match="non-empty"):
        stage_campaign("", ResearchProfileBlock())


def test_stage_campaign_rejects_whitespace_topic() -> None:
    """A whitespace-only topic raises ValueError at the runner boundary."""
    with pytest.raises(ValueError, match="non-empty"):
        stage_campaign("   ", ResearchProfileBlock())


# --- StagedDispatch model invariants ------------------------------------


def test_staged_dispatch_read_only_is_fixed_true() -> None:
    """A staged dispatch cannot be constructed with read_only=False."""
    with pytest.raises(ValidationError):
        StagedDispatch.model_validate(
            {
                "domain": "d",
                "agent_role": "researcher",
                "depth": "medium",
                "prompt": "p",
                "read_only": False,
            }
        )
