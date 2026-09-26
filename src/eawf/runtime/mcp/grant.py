"""The one computation that decides which MCP tools a native Run reaches.

A Run's MCP exposure is the intersection of three independent answers:
what its role may produce (the gateway's role ceiling), what its task
granted (the capsule request's grants), and what the bound certification
proved the runtime can carry (the ``semantic_tools`` capability). Each
answer only narrows, so no one of them can hand a Run a tool the other two
withheld.

The intersection is applied twice on purpose. :func:`intersect_run_tools`
narrows the grants before the capsule is sealed, so the sealed capsule --
the single place authority is written -- already is the intersection; and
:func:`assert_capsule_within_intersection` re-checks a capsule at launch,
so a capsule sealed by any other path cannot reach the rendered MCP
configuration wider than the intersection allows.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.compiled import CapabilityObservation, CompiledRunSpec
from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.state.enums import AgentSessionRole
from eawf.runtime.daemon.semantic_gateway import ROLE_GATED_TOOLS, ROLE_TOOL_CEILING

logger = logging.getLogger(__name__)

#: The certified capability that carries the per-Run semantic MCP server.
SEMANTIC_TOOLS_CAPABILITY: Final = "semantic_tools"

#: Capability statuses under which a certification actually carries the
#: capability; every other status names why it does not.
_CARRYING_STATUSES: Final[frozenset[str]] = frozenset({"verified", "degraded"})


@dataclass(frozen=True, slots=True)
class RunToolIntersection:
    """The three grant answers of one Run and the tools all three admit.

    Attributes:
        role_admitted: The catalog tools the Run's role may call.
        task_granted: The tools the task asked for, in request order.
        certified: Whether the bound certification carries semantic tools.
        granted: The task's tools that the role admits, in request order,
            or nothing at all when the certification does not carry the
            capability.
    """

    role_admitted: frozenset[SemanticToolId]
    task_granted: tuple[SemanticToolId, ...]
    certified: bool
    granted: tuple[SemanticToolId, ...]


def role_admitted_tools(role: AgentSessionRole) -> frozenset[SemanticToolId]:
    """Return the catalog tools *role* may call.

    Args:
        role: The Run's agent role.

    Returns:
        Every ungated catalog tool plus the gated tools the role's
        ceiling adds back.

    Raises:
        KeyError: *role* has no ceiling row, which the gateway's own
            import-time check already makes impossible for a known role.
    """
    ungated = frozenset(SemanticToolId) - ROLE_GATED_TOOLS
    return ungated | ROLE_TOOL_CEILING[role]


def certifies_semantic_tools(capabilities: Iterable[CapabilityObservation]) -> bool:
    """Return whether a certification carries the semantic tool capability.

    Args:
        capabilities: The negotiated capability rows of the bound runtime.

    Returns:
        ``True`` when a ``semantic_tools`` row is verified or degraded.
    """
    return any(
        row.capability_id == SEMANTIC_TOOLS_CAPABILITY and row.status in _CARRYING_STATUSES
        for row in capabilities
    )


def intersect_run_tools(
    *,
    role: AgentSessionRole,
    task_grants: Sequence[str],
    capabilities: Iterable[CapabilityObservation],
) -> RunToolIntersection:
    """Intersect a Run's role, task and certification grants.

    Args:
        role: The Run's agent role.
        task_grants: The tool ids the task requested. Each must name a
            catalog tool.
        capabilities: The bound certification's capability rows.

    Returns:
        The three answers and the tools every one of them admits.

    Raises:
        ValueError: A task grant names no catalog tool.
    """
    task = tuple(SemanticToolId(tool_id) for tool_id in task_grants)
    admitted = role_admitted_tools(role)
    certified = certifies_semantic_tools(capabilities)
    granted = tuple(tool for tool in task if tool in admitted) if certified else ()
    dropped = len(task) - len(granted)
    if dropped:
        logger.info(
            f"intersect_run_tools role={role.value} requested={len(task)} "
            f"granted={len(granted)} certified={certified}"
        )
    return RunToolIntersection(
        role_admitted=admitted, task_granted=task, certified=certified, granted=granted
    )


def assert_capsule_within_intersection(
    *, spec: CompiledRunSpec, capsule: AuthorityCapsule
) -> RunToolIntersection:
    """Refuse a capsule that reaches a tool outside the Run's intersection.

    Args:
        spec: The compiled spec supplying the role and certification.
        capsule: The sealed capsule the launch would expose.

    Returns:
        The intersection the capsule was checked against.

    Raises:
        ValueError: The capsule reaches a tool the role or the
            certification does not admit.
    """
    intersection = intersect_run_tools(
        role=spec.agent_role,
        task_grants=[tool.value for tool in capsule.semantic_tools],
        capabilities=spec.capabilities,
    )
    widened = sorted(
        tool.value for tool in capsule.semantic_tools if tool not in intersection.granted
    )
    if widened:
        raise ValueError(
            f"mcp_grant_widened: the capsule reaches {widened} outside the intersection of "
            f"role {spec.agent_role.value!r}, task and certification; run={spec.run_ref}"
        )
    return intersection


__all__ = [
    "SEMANTIC_TOOLS_CAPABILITY",
    "RunToolIntersection",
    "assert_capsule_within_intersection",
    "certifies_semantic_tools",
    "intersect_run_tools",
    "role_admitted_tools",
]
