"""Typed cost attribution for the runs joined to a completed unit of work.

A completed unit of work is one closed wave, and every run joined to it is
attributed to exactly one :class:`CostClass`. The split exists because
"what did this wave cost?" is two different questions: the spend that
*produced* the change (execution) and the spend that *checked* it
(verification). Folding both into one number makes a verification-heavy
delivery indistinguishable from an expensive implementation, so the two
sums are kept apart end to end and never re-added into a combined field.

:data:`ROLE_COST_CLASS` is total over :class:`~eawf.kernel.state.enums.AgentSessionRole`:
every declared role resolves to a class, so a new role member fails the
totality test rather than silently defaulting into the execution sum.
Attribution is derived from the role and never guessed: a run with no role
raises instead of landing in either sum, because an unlabelled run charged
to execution is a silent measurement error that no downstream consumer can
detect.
"""

from __future__ import annotations

from enum import StrEnum

from eawf.kernel.state.enums import AgentSessionRole

__all__ = [
    "ROLE_COST_CLASS",
    "CostClass",
    "classify_cost_class",
]


class CostClass(StrEnum):
    """Closed attribution taxonomy for one run's wall clock and cost.

    Values:
        VERIFICATION: The run checked work that already existed.
        EXECUTION: The run produced or changed the work.
        UNATTRIBUTED: The run belongs to neither side of the split and is
            excluded from both sums while still being counted.
    """

    VERIFICATION = "verification"
    EXECUTION = "execution"
    UNATTRIBUTED = "unattributed"


ROLE_COST_CLASS: dict[AgentSessionRole, CostClass] = {
    AgentSessionRole.RESEARCHER: CostClass.EXECUTION,
    AgentSessionRole.PLANNER: CostClass.EXECUTION,
    AgentSessionRole.EXECUTOR: CostClass.EXECUTION,
    AgentSessionRole.DOMAIN_SPECIALIST: CostClass.EXECUTION,
    AgentSessionRole.AUDITOR: CostClass.VERIFICATION,
    AgentSessionRole.REVIEWER: CostClass.VERIFICATION,
    AgentSessionRole.POLISHER: CostClass.VERIFICATION,
    AgentSessionRole.OPERATOR: CostClass.UNATTRIBUTED,
}
"""Total role-to-class map. Every ``AgentSessionRole`` member has a row."""


def classify_cost_class(role: AgentSessionRole | None) -> CostClass:
    """Return the cost class a run with *role* is attributed to.

    Args:
        role: The agent role the run was dispatched under.

    Returns:
        The :class:`CostClass` the run's wall clock and cost belong to.

    Raises:
        ValueError: When *role* is ``None``. An unlabelled run is a
            measurement gap, not an execution run.
        TypeError: When *role* is not an :class:`AgentSessionRole` member
            (a bare ``str`` role never classifies).
        KeyError: When *role* is a role member with no attribution row.
    """
    if role is None:
        raise ValueError(
            "run carries no agent role: cannot attribute its cost to verification or execution"
        )
    if not isinstance(role, AgentSessionRole):
        raise TypeError(f"role must be an AgentSessionRole, got {type(role).__name__}")
    try:
        return ROLE_COST_CLASS[role]
    except KeyError as exc:
        raise KeyError(f"no cost-class attribution for role {role.value!r}") from exc
