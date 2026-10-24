"""Sandbox / permission policy table.

A :class:`SandboxPolicy` declares the tool-allow / tool-deny shape an agent
session may use under a given scope (wave, profile, or global). The table
is populated and queryable.

Layout mirrors :class:`~eawf.kernel.state.models.McpGrant`:

- ``id`` follows ``POL-<n>``;
- ``scope_kind`` ∈ ``{"wave", "profile", "global"}``;
- ``scope_id`` is the wave id, profile name, or literal ``"global"``;
- ``allowed_tools`` / ``denied_tools`` are explicit name lists (no globs
  in v0.2);
- ``granted_at`` is the immutable creation timestamp.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)


SandboxPolicyScopeKind = Literal["wave", "profile", "global"]


class SandboxPolicy(BaseModel):
    """Scope-binding between a wave/profile/global scope and a tool list.

    Strict shape — unknown keys are rejected so state-level corruption is
    caught at load time.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^\S+$")
    scope_kind: SandboxPolicyScopeKind
    scope_id: str = Field(min_length=1)
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    granted_at: UtcDatetime


_POLICY_ID_PREFIX: str = "POL-"

#: The canonical tool universe an ambient deny-list draws its names from. Modeled
#: from the eawf agent-role tool set (the union of every ``AgentSpec.tools`` in
#: :data:`eawf.surfaces.render.agents.AGENT_REGISTRY`) rather than imported
#: from the render layer -- the sandbox policy module is a leaf the render
#: layer may depend on, so a back-import here would invert that edge. The
#: universe is the closed set of tools an eawf-dispatched agent can hold; a
#: deny-list names a subset to drop.
TOOL_UNIVERSE: frozenset[str] = frozenset(
    {
        "Read",
        "Write",
        "Edit",
        "Grep",
        "Glob",
        "Bash",
        "WebFetch",
        "WebSearch",
        "Skill",
        "Agent",
        "TaskCreate",
        "TaskUpdate",
        "TaskList",
        "TaskGet",
    }
)


__all__ = [
    "TOOL_UNIVERSE",
    "SandboxPolicy",
    "SandboxPolicyScopeKind",
]
