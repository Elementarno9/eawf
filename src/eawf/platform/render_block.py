"""Shared render-block conventions for managed documentation surfaces."""

from __future__ import annotations

from typing import Literal

RenderBlockTier = Literal["tier0", "reference"]

DEFAULT_RENDER_BLOCK_TIER: RenderBlockTier = "reference"
DEFAULT_TIER0_TOKEN_CAP = 1200

#: Where a managed-file render block puts its full body.
#:
#: - ``"root"`` — the body is emitted verbatim into the managed file (today's
#:   only behaviour, hence the default).
#: - ``"reference"`` — the body is written to its own file under
#:   :data:`RULE_REFERENCE_DIR` and the managed file carries one compact line
#:   naming the obligation plus the path to that expansion.
#:
#: Placement answers "where do the bytes live"; the sibling
#: :data:`RenderBlockTier` answers "which zone of the managed file does the
#: block belong to". The two are independent: a reference-placed block still
#: renders its compact line inside its declared zone.
RenderBlockPlacement = Literal["root", "reference"]

#: Default placement. ``"root"`` keeps every block that does not declare a
#: placement rendering exactly as it did before the field existed.
DEFAULT_RENDER_BLOCK_PLACEMENT: RenderBlockPlacement = "root"

#: Directory (relative to the managed file's parent) holding the full body of
#: every ``placement: reference`` block, one ``<block-id>.md`` per block. The
#: managed file links here, so the content is moved out of the reader's
#: always-loaded budget without being lost.
RULE_REFERENCE_DIR: str = "docs/rules"

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
    "DEFAULT_RENDER_BLOCK_PLACEMENT",
    "DEFAULT_RENDER_BLOCK_TIER",
    "DEFAULT_ROLE_TIER_TOKEN_CAP",
    "DEFAULT_TIER0_TOKEN_CAP",
    "DISPATCH_SYSTEM_PROMPT_TARGET",
    "RULE_REFERENCE_DIR",
    "RenderBlockPlacement",
    "RenderBlockTier",
]
