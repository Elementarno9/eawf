"""Sandbox / permission policy table.

A :class:`SandboxPolicy` declares the tool-allow / tool-deny shape an agent
session may use under a given scope (wave, profile, or global). In v0.2 the
table is populated and queryable; enforcement (hard refusal at dispatch
time) lands in a follow-up wave — the renderer surfaces the allowed-tool
list in the envelope hint only.

Layout mirrors :class:`~eawf.state.models.McpGrant`:

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

from eawf.state.types import UtcDatetime

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


__all__ = [
    "SANDBOX_SCOPE_KINDS",
    "SandboxPolicy",
    "SandboxPolicyScopeKind",
    "allocate_policy_id",
]
