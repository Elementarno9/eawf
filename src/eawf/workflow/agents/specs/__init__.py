"""Typed subagent-spec + role library.

Public API:

- :class:`~eawf.workflow.agents.specs.models.SubagentSpec` — typed dispatch spec
  for one wave. :meth:`SubagentSpec.render` produces the wave prompt from
  the typed spec rather than ad-hoc string concatenation.
- :class:`~eawf.workflow.agents.specs.roles.RoleSpec` + :data:`ROLE_REGISTRY` —
  the role library. Each role renders a contract block to every kept
  runtime (``claude-code`` / ``codex`` / ``opencode``)
  via :meth:`RoleSpec.render` / :func:`render_role_contract`.

All rendering is pure — no I/O, no logging side-effects beyond the
module-level loggers.
"""

from __future__ import annotations

from eawf.workflow.agents.specs.models import (
    RoleContract,
    SpecAudit,
    SpecDecision,
    SpecDependency,
    SpecHypothesis,
    SpecWorktree,
    SubagentSpec,
)
from eawf.workflow.agents.specs.roles import (
    KEPT_RUNTIMES,
    ROLE_REGISTRY,
    RoleSpec,
    get_role_spec,
    render_role_contract,
)

__all__ = [
    "KEPT_RUNTIMES",
    "ROLE_REGISTRY",
    "RoleContract",
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
