"""Typed role library.

Public API:

- :class:`~eawf.workflow.agents.specs.models.RoleContract` — the typed
  projection of one role's invariants a runtime adapter accepts.
- :class:`~eawf.workflow.agents.specs.roles.RoleSpec` + :data:`ROLE_REGISTRY` —
  the role library. Each role renders a contract block to every kept
  runtime (``claude-code`` / ``codex`` / ``opencode``)
  via :meth:`RoleSpec.render` / :func:`render_role_contract`.

All rendering is pure — no I/O, no logging side-effects beyond the
module-level loggers.
"""

from __future__ import annotations

from eawf.workflow.agents.specs.models import RoleContract
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
    "get_role_spec",
    "render_role_contract",
]
