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

Kept runtimes for v0.3-v0.5: ``claude-code``, ``codex``, ``opencode``.
:meth:`RoleSpec.render` accepts one of those
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
from eawf.surfaces.render.agents import AGENT_REGISTRY, AgentSpec

logger = logging.getLogger(__name__)


#: Runtimes the role library renders to. Pinned to the v0.3-v0.5 kept set
#: and ordered to match
#: :data:`eawf.runtime.runtimes.capabilities.RUNTIME_IDS`.
KEPT_RUNTIMES: tuple[RuntimeId, ...] = ("claude-code", "codex", "opencode")


#: Per-runtime note describing where the runtime materialises a subagent
#: role definition on disk. Mirrors the placement documented on
#: :class:`eawf.runtime.runtimes.manifest.PluginContributes.agents`.
_RUNTIME_PLACEMENT: dict[RuntimeId, str] = {
    "claude-code": "Rendered as `.claude/agents/<role>.md`.",
    "codex": "Rendered as `.codex/agents/<role>.toml`.",
    "opencode": "Rendered as `.opencode/agents/<role>.md`.",
}


class RoleSpec(BaseModel):
    """Typed contract for one subagent role across kept runtimes.

    P28-I01-W12 extended this model with the role-level invariants every
    per-role surface (Claude / Codex / OpenCode / dispatch
    :class:`~eawf.workflow.agents.specs.models.SubagentSpec`) consumes:
    ``system_prompt``, ``allowed_tools``, ``denied_tools``, ``model``,
    ``memory``, ``report_schema_ref``, ``stop_conditions``. The
    :func:`eawf.workflow.dispatch.renderer.build_role_contract` projection
    reads these fields into a :class:`RoleContract`
    consumed by :class:`SubagentSpec`, so the dispatch prompt's role
    body is driven by the role registry rather than a hardcoded constant.

    The legacy ``body`` attribute is preserved as a read-only alias of
    :attr:`system_prompt` so pre-W12 callers continue to work without
    edits; new callers should read :attr:`system_prompt` directly.

    Attributes:
        role: The canonical :class:`~eawf.kernel.state.enums.AgentSessionRole`.
        summary: One-sentence description (matches the role's
            ``AgentSpec.description`` in
            :data:`eawf.surfaces.render.agents.AGENT_REGISTRY`).
        system_prompt: The role contract Markdown (method, output
            contract, anti-patterns), reused verbatim from the same
            registry. Renamed from ``body`` in P28-I01-W12; the legacy
            ``body`` attribute remains as a read-only alias.
        allowed_tools: Tool names the role may invoke (e.g.
            ``["Read", "Edit", "Bash"]``). Sourced from
            :attr:`AgentSpec.tools`.
        denied_tools: Tool names the role MUST NOT invoke. Empty by
            default; per-wave sandbox policies tighten this list further
            at dispatch time via
            :func:`~eawf.runtime.sandbox.policy.resolve_denied_tools`.
        model: Preferred model identifier (e.g. ``"opus"``), or ``None``
            when the role inherits the dispatcher's default.
        memory: Whether the role retains memory across invocations.
            Sourced from :attr:`AgentSpec.memory`.
        report_schema_ref: The typed-report store-kind reference for
            this role's ``agent_end`` reports (e.g.
            ``"executor_report"``). Equal to :attr:`report_store_kind`
            today; a future wave may evolve this to a full schema URN
            without touching :attr:`report_store_kind`.
        report_store_kind: Legacy alias of :attr:`report_schema_ref`
            (preserved for back-compat with the W14 surface that already
            persisted this name in goldens).
        stop_conditions: Conditions under which the role's session
            must stop and report. Empty when the role has no
            role-specific stop conditions beyond the default.
    """

    model_config = ConfigDict(extra="forbid")

    role: AgentSessionRole
    summary: str
    system_prompt: str
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    model: str | None = None
    memory: bool = False
    report_schema_ref: str = Field(min_length=1)
    report_store_kind: str = Field(min_length=1)
    stop_conditions: list[str] = Field(default_factory=list)

    @property
    def body(self) -> str:
        """Legacy alias for :attr:`system_prompt` (read-only).

        Pre-W12 callers referenced ``RoleSpec.body``; that name is now
        :attr:`system_prompt`. The property keeps the old surface usable
        without forcing downstream call sites to migrate in lockstep.
        """
        return self.system_prompt

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
            self.system_prompt.rstrip("\n"),
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
        registry[role] = _role_spec_from_agent_spec(spec, role)
    return registry


def _role_spec_from_agent_spec(spec: AgentSpec, role: AgentSessionRole) -> RoleSpec:
    """Project an :class:`AgentSpec` registry row into a :class:`RoleSpec`.

    Centralises the AGENT_REGISTRY → RoleSpec mapping so the field set
    has a single home. P28-I01-W12 widened ``RoleSpec`` with role-level
    invariants (``allowed_tools``, ``denied_tools``, ``model``, …); the
    projection wires every one of those off the ``AgentSpec`` row that
    already carries them.

    ``stop_conditions`` and ``denied_tools`` are intentionally empty
    here — the seam exists for future waves (W18/W30/W38) to fill the
    bodies without re-walking the registry.

    Args:
        spec: The source :class:`AgentSpec` row from
            :data:`AGENT_REGISTRY`.
        role: The matching :class:`AgentSessionRole` enum member (already
            validated by the caller).

    Returns:
        A fully-populated :class:`RoleSpec` for this role.
    """
    store_kind = store_kind_for_role(role).value
    return RoleSpec(
        role=role,
        summary=spec.description,
        system_prompt=spec.body,
        allowed_tools=list(spec.tools),
        denied_tools=[],
        model=spec.model,
        memory=spec.memory,
        report_schema_ref=store_kind,
        report_store_kind=store_kind,
        stop_conditions=[],
    )


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
