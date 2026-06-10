"""Shared render-block conventions for managed documentation surfaces."""

from __future__ import annotations

from typing import Literal

RenderBlockTier = Literal["tier0", "reference"]

DEFAULT_RENDER_BLOCK_TIER: RenderBlockTier = "reference"
DEFAULT_TIER0_TOKEN_CAP = 1200

#: ``RenderBlock.target`` value that marks a block as a per-role dispatch
#: render block (FLEET-5 / P30-I06-W05). A block carrying this target plus a
#: non-``None`` :attr:`~eawf.platform.profiles.models.RenderBlock.agent_role`
#: is the "Zone 3" role tier: its body is injected into the dispatched system
#: prompt for waves whose ``agent_role`` matches. The target name is namespaced
#: with the ``dispatch:`` prefix so it never collides with a managed-file
#: target such as ``"AGENTS.md"``.
DISPATCH_SYSTEM_PROMPT_TARGET: str = "dispatch:system_prompt"

#: Default token cap for one role-tier dispatch block. The role-tier injection
#: (W05) leaves this cap as a measurable seam; W06 wires the per-role budget
#: gate that bounds an injected block's
#: :func:`~eawf.platform.lint.tools.agents_md_budget.count_tokens` weight
#: against it. Pinned to the same magnitude as the tier-0 cap so a role block
#: stays a preamble, not a second prompt.
DEFAULT_ROLE_TIER_TOKEN_CAP = 1200

__all__ = [
    "DEFAULT_RENDER_BLOCK_TIER",
    "DEFAULT_ROLE_TIER_TOKEN_CAP",
    "DEFAULT_TIER0_TOKEN_CAP",
    "DISPATCH_SYSTEM_PROMPT_TARGET",
    "RenderBlockTier",
]
