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
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)


SandboxPolicyScopeKind = Literal["wave", "profile", "global"]
SANDBOX_SCOPE_KINDS: tuple[SandboxPolicyScopeKind, ...] = get_args(SandboxPolicyScopeKind)


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

#: The canonical tool universe a deny-list is inverted against when a runtime
#: exposes only an allowlist grant (no native per-call deny flag). Modeled
#: from the eawf agent-role tool set (the union of every ``AgentSpec.tools`` in
#: :data:`eawf.surfaces.render.agents.AGENT_REGISTRY`) rather than imported
#: from the render layer -- the sandbox policy module is a leaf the render
#: layer may depend on, so a back-import here would invert that edge. The
#: universe is the closed set of tools an eawf-dispatched agent can hold; a
#: deny-list names a subset to drop, and the inverted allowlist is the
#: complement the allowlist-only runtime is granted.
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


def invert_deny_to_allow(denied: set[str] | frozenset[str] | list[str]) -> list[str]:
    """Invert a deny-list into the allowlist a deny-less runtime is granted.

    Runtimes that grant tools by allowlist only (no native per-call deny
    flag, e.g. ``codex exec``) cannot deny a tool directly. The deny is
    expressed as its complement: the child is granted every tool in
    :data:`TOOL_UNIVERSE` EXCEPT the denied names, so a denied tool is absent
    from the grant by construction. Names in *denied* that are not in the
    universe are simply not present in the complement (they were never
    grantable), so a stray name cannot widen the allow set.

    Args:
        denied: The per-wave deny-list of tool names to drop from the grant.

    Returns:
        The sorted allowlist (``TOOL_UNIVERSE`` minus *denied*) the runtime is
        granted. Sorted so the resulting argv is deterministic.
    """
    return sorted(TOOL_UNIVERSE - set(denied))


def allocate_policy_id(existing: dict[str, SandboxPolicy] | None) -> str:
    """Return the smallest free ``POL-<n>`` id given the existing pool."""
    pool = existing or {}
    next_n = 1
    for existing_id in pool:
        if not existing_id.startswith(_POLICY_ID_PREFIX):
            continue
        try:
            n = int(existing_id.removeprefix(_POLICY_ID_PREFIX))
        except ValueError:
            continue
        next_n = max(next_n, n + 1)
    return f"{_POLICY_ID_PREFIX}{next_n}"


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
    of the wave-dispatch projection (the same scoping ladder the grant
    projection in :func:`eawf.workflow.dispatch.renderer._project_allowed_tools`
    uses for wave-scoped grants).

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
    "SANDBOX_SCOPE_KINDS",
    "TOOL_UNIVERSE",
    "SandboxPolicy",
    "SandboxPolicyScopeKind",
    "allocate_policy_id",
    "invert_deny_to_allow",
    "resolve_denied_tools",
]
