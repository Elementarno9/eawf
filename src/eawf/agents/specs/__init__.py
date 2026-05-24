"""Typed subagent-spec + role library (P27-I03-W14).

Public API:

- :class:`~eawf.agents.specs.models.SubagentSpec` — typed dispatch spec
  for one wave. The dispatch renderer builds it from a validated
  :class:`~eawf.kernel.state.models.State` snapshot and calls
  :meth:`SubagentSpec.render` to produce the wave prompt, so the prompt
  is rendered from a typed spec rather than ad-hoc string concatenation.
- :class:`~eawf.agents.specs.roles.RoleSpec` + :data:`ROLE_REGISTRY` —
  the role library. Each role renders a contract block to every kept
  runtime (``claude-code`` / ``codex`` / ``opencode`` per decision D12)
  via :meth:`RoleSpec.render` / :func:`render_role_contract`.

All rendering is pure — no I/O, no logging side-effects beyond the
module-level loggers.
"""

from __future__ import annotations

from eawf.agents.specs.models import (
    SpecAudit,
    SpecDecision,
    SpecDependency,
    SpecHypothesis,
    SpecWorktree,
    SubagentSpec,
)
from eawf.agents.specs.roles import (
    KEPT_RUNTIMES,
    ROLE_REGISTRY,
    RoleSpec,
    get_role_spec,
    render_role_contract,
)

__all__ = [
    "KEPT_RUNTIMES",
    "ROLE_REGISTRY",
    "RoleSpec",
    "SpecAudit",
    "SpecDecision",
    "SpecDependency",
    "SpecHypothesis",
    "SpecWorktree",
    "SubagentSpec",
    "get_role_spec",
    "render_role_contract",
]
