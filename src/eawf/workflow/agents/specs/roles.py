"""Subagent role registry rendering to every kept runtime (P27-I03-W14).

The role registry is the typed counterpart to the dispatch
:class:`~eawf.workflow.agents.specs.models.SubagentSpec`: where the spec carries
the *wave-specific* prompt body, a :class:`RoleSpec` carries the
*role-specific* contract preamble (method, output contract,
anti-patterns) shared by every dispatch of that role.

The canonical role contract bodies already live in
:data:`eawf.surfaces.render.agents.AGENT_REGISTRY` (they back the static
``.claude/agents/<role>.md`` files). This module reuses those bodies
verbatim — it does not re-author them — and layers the per-runtime
placement note plus the typed-report store-kind pointer on top, so the
registry stays the single source of truth for the contract text (DRY).

Kept runtimes (per decision D12, v0.3-v0.5 scope): ``claude-code``,
``codex``, ``opencode``. :meth:`RoleSpec.render` accepts one of those
ids and returns a runtime-tailored contract block; an unknown runtime id
raises :class:`ValueError`. :func:`render_role_contract` is the
free-function entry point used by callers that hold a role + runtime
rather than a :class:`RoleSpec`.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.store.kinds.agent_report import store_kind_for_role
from eawf.runtime.runtimes.manifest import RuntimeId
from eawf.surfaces.render.agents import AGENT_REGISTRY

logger = logging.getLogger(__name__)


#: Runtimes the role library renders to. Pinned to the v0.3-v0.5 kept set
#: (decision D12) and ordered to match
#: :data:`eawf.runtime.runtimes.capabilities.RUNTIME_IDS`.
KEPT_RUNTIMES: tuple[RuntimeId, ...] = ("claude-code", "codex", "opencode")


#: Per-runtime note describing where the runtime materialises a subagent
#: role definition on disk. Mirrors the placement documented on
#: :class:`eawf.runtime.runtimes.manifest.PluginContributes.agents`.
_RUNTIME_PLACEMENT: dict[RuntimeId, str] = {
    "claude-code": "Rendered as `.claude/agents/<role>.md`.",
    "codex": "Nested inside the Codex skill bundle (no standalone agent file).",
    "opencode": "Rendered as `.opencode/agent/<role>.md`.",
}


class RoleSpec(BaseModel):
    """Typed contract for one subagent role across kept runtimes.

    Attributes:
        role: The canonical :class:`~eawf.kernel.state.enums.AgentSessionRole`.
        summary: One-sentence description (matches the role's
            ``AgentSpec.description`` in
            :data:`eawf.surfaces.render.agents.AGENT_REGISTRY`).
        body: The role contract Markdown (method, output contract,
            anti-patterns), reused verbatim from the same registry.
        report_store_kind: The role's typed-report store kind value
            (e.g. ``"executor_report"``) used in the report pointer.
    """

    model_config = ConfigDict(extra="forbid")

    role: AgentSessionRole
    summary: str
    body: str
    report_store_kind: str = Field(min_length=1)

    def render(self, runtime: RuntimeId) -> str:
        """Return the role contract block tailored for *runtime*.

        Args:
            runtime: One of :data:`KEPT_RUNTIMES`.

        Returns:
            A Markdown block: a runtime-named heading, the one-line
            summary, the per-runtime placement note, the verbatim
            contract body, and the typed-report store-kind pointer. No
            trailing newline.

        Raises:
            ValueError: *runtime* is not a kept runtime.
        """
        if runtime not in KEPT_RUNTIMES:
            raise ValueError(f"unknown runtime: {runtime!r}; expected one of {list(KEPT_RUNTIMES)}")
        placement = _RUNTIME_PLACEMENT[runtime]
        report_line = (
            f"On completion emit an `agent_end` report; it persists to the "
            f"`{self.report_store_kind}` store."
        )
        lines = [
            f"## Role: {self.role.value} ({runtime})",
            "",
            self.summary,
            "",
            placement,
            "",
            self.body.rstrip("\n"),
            "",
            report_line,
        ]
        return "\n".join(lines)


def _build_registry() -> dict[AgentSessionRole, RoleSpec]:
    """Project :data:`AGENT_REGISTRY` into a role-keyed :class:`RoleSpec` map.

    Each :class:`eawf.surfaces.render.agents.AgentSpec` row supplies the summary
    and body; the role's report store kind is read from
    :func:`eawf.kernel.store.kinds.agent_report.store_kind_for_role`. Building
    once at import time keeps the registry a frozen lookup.

    Returns:
        A mapping from every :class:`AgentSessionRole` member to its
        :class:`RoleSpec`.

    Raises:
        ValueError: An ``AgentSpec.role`` string does not map onto an
            :class:`AgentSessionRole` member (registry authoring error).
    """
    registry: dict[AgentSessionRole, RoleSpec] = {}
    for spec in AGENT_REGISTRY:
        try:
            role = AgentSessionRole(spec.role)
        except ValueError as exc:
            raise ValueError(f"unknown agent role: {spec.role!r}") from exc
        registry[role] = RoleSpec(
            role=role,
            summary=spec.description,
            body=spec.body,
            report_store_kind=store_kind_for_role(role).value,
        )
    return registry


#: Frozen role registry — every :class:`AgentSessionRole` mapped to its
#: :class:`RoleSpec`. Built once from :data:`AGENT_REGISTRY` at import.
ROLE_REGISTRY: dict[AgentSessionRole, RoleSpec] = _build_registry()


def get_role_spec(role: AgentSessionRole) -> RoleSpec:
    """Return the :class:`RoleSpec` for *role*.

    Args:
        role: The role to look up.

    Returns:
        The registered :class:`RoleSpec`.

    Raises:
        KeyError: *role* has no registered spec (cannot happen for a
            valid :class:`AgentSessionRole` — the registry covers every
            member — but the lookup stays explicit).
    """
    try:
        return ROLE_REGISTRY[role]
    except KeyError as exc:
        raise KeyError(f"no role spec registered for role: {role.value!r}") from exc


def render_role_contract(role: AgentSessionRole, runtime: RuntimeId) -> str:
    """Render the contract block for *role* on *runtime*.

    Convenience wrapper over :meth:`RoleSpec.render` for callers holding
    a role + runtime rather than a :class:`RoleSpec` instance.

    Args:
        role: The role whose contract to render.
        runtime: One of :data:`KEPT_RUNTIMES`.

    Returns:
        The rendered Markdown contract block (no trailing newline).

    Raises:
        KeyError: *role* has no registered spec.
        ValueError: *runtime* is not a kept runtime.
    """
    return get_role_spec(role).render(runtime)


__all__ = [
    "KEPT_RUNTIMES",
    "ROLE_REGISTRY",
    "RoleSpec",
    "get_role_spec",
    "render_role_contract",
]
