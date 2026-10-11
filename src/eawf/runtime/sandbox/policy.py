"""Sandbox / permission policy table.

A :class:`SandboxPolicy` declares the tool-allow / tool-deny shape an agent
session may use under a given scope (wave, profile, or global). The table
is populated and queryable, and :func:`resolve_denied_tools` resolves the
deny set the dispatcher subtracts from the rendered ``allowed_tools``
projection so a wave with a denied tool cannot dispatch it.

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


def resolve_denied_tools(
    policies: dict[str, SandboxPolicy] | None,
    *,
    wave_id: str,
) -> set[str]:
    """Return the set of tool names denied for *wave_id* by the policy table.

    A policy applies to *wave_id* when it is wave-scoped at that exact wave
    (``scope_kind == "wave"`` and ``scope_id == wave_id``) or global
    (``scope_kind == "global"`` — global denials cover every wave). The
    returned set is the union of those policies' ``denied_tools``.

    Profile-scoped policies are not resolved here: the dispatcher does not
    yet know the dispatched wave's profile, so profile deny-lists stay out
    of the wave-dispatch projection.

    Args:
        policies: The ``state.sandbox_policies`` map, or ``None`` when no
            policies are registered.
        wave_id: The dispatched wave id.

    Returns:
        The union of denied tool names that apply to *wave_id*. Empty when
        *policies* is ``None``/empty or no policy targets the wave.
    """
    pool = policies or {}
    denied: set[str] = set()
    for policy in pool.values():
        applies = (policy.scope_kind == "wave" and policy.scope_id == wave_id) or (
            policy.scope_kind == "global"
        )
        if applies:
            denied.update(policy.denied_tools)
    return denied


__all__ = [
    "TOOL_UNIVERSE",
    "SandboxPolicy",
    "SandboxPolicyScopeKind",
    "resolve_denied_tools",
]
