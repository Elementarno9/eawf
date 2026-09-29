"""Delegation: how many child Runs a subtree may hold, and what a child is granted.

A Run that delegates is bounded by its ``child_runs`` ceiling, and the ceiling counts the
whole subtree under it rather than its direct children: a child that delegates in turn
spends the same allowance, so depth cannot multiply it. Every Run on the path from a new
child up to the root is checked, because each of them sealed a ceiling of its own.

A child starts from nothing its parent held. It is sealed a fresh capsule of its own, and
its grant is cut to the parent's: a tool the parent was not granted, or an authority level
above the parent's, is not handed down however the child's own policy resolved.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StrictInt, model_validator

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.provider import AUTHORITY_LEVEL_ORDER, AuthorityGrant, RuntimeRecord
from eawf.kernel.state.epoch2.run import RunScope, TaskScope
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)

#: The most child Runs any policy resolves for a subtree.
MAX_CHILD_RUNS: Final = 32


def resolved_child_runs(scope: RunScope) -> int:
    """Return the ``child_runs`` ceiling a Run's scope class resolves to.

    A Task-scoped Run fans out to nobody: one Task binds one lease and one
    candidate, and two writers inside it could not be told apart. Every other
    scope reads, so its children produce evidence rather than competing edits.

    Args:
        scope: The Run's scope.

    Returns:
        ``0`` for a Task scope, :data:`MAX_CHILD_RUNS` for any other.
    """
    return 0 if isinstance(scope, TaskScope) else MAX_CHILD_RUNS


@dataclasses.dataclass(frozen=True, slots=True)
class SubtreeOverrun:
    """A delegation subtree holding more child Runs than one of its Runs admits.

    Attributes:
        ancestor: The Run whose ceiling the subtree passed.
        ceiling: That Run's ``child_runs`` ceiling.
        descendants: How many Runs its subtree holds, the new child included.
    """

    ancestor: str
    ceiling: int
    descendants: int


def _descendants(children: Mapping[str, Sequence[str]], root: str) -> int:
    """Return how many Runs sit anywhere under *root*."""
    count, frontier = 0, list(children.get(root, ()))
    while frontier:
        run = frontier.pop()
        count += 1
        frontier.extend(children.get(run, ()))
    return count


def subtree_overrun(
    parents: Mapping[str, str | None], *, child: str, ceiling_of: Callable[[str], int]
) -> SubtreeOverrun | None:
    """Return the first Run above *child* whose subtree passes its ceiling, if any.

    Args:
        parents: Every Run the tree holds, the child included, mapped to the
            Run that delegated it or ``None`` for a root.
        child: The child Run being counted.
        ceiling_of: The ``child_runs`` ceiling of one Run.

    Returns:
        The nearest ancestor whose subtree holds more Runs than its ceiling, or
        ``None`` when every ancestor's subtree is within its ceiling.

    Raises:
        KeyError: *child* is not one of *parents*.
    """
    children: dict[str, list[str]] = {}
    for run, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(run)
    ancestor = parents[child]
    while ancestor is not None:
        count, ceiling = _descendants(children, ancestor), ceiling_of(ancestor)
        if count > ceiling:
            return SubtreeOverrun(ancestor=ancestor, ceiling=ceiling, descendants=count)
        ancestor = parents.get(ancestor)
    return None


class ChildCeilingBreach(RuntimeRecord):
    """A child Run admitted past a ``child_runs`` ceiling, as the run ledger holds it.

    Only a child that was already running when it was adopted is admitted past
    a ceiling: the host spawned it, so refusing its record would hide it rather
    than stop it. The breach is filed against the ancestor whose ceiling was
    passed so the overrun is read where the delegation was made.

    Attributes:
        payload_kind: The discriminator separating a breach line from the
            other lines of the run ledger.
        child_run_ref: The child that took the subtree past its ceiling.
        ancestor_run_ref: The Run whose ceiling was passed.
        ceiling: That Run's ``child_runs`` ceiling.
        descendants: How many Runs its subtree held once the child joined.
        recorded_at: When the breach was recorded.
    """

    payload_kind: Literal["child_ceiling_breach"] = "child_ceiling_breach"
    child_run_ref: RunUrn
    ancestor_run_ref: RunUrn
    ceiling: Annotated[StrictInt, Field(ge=0, le=MAX_CHILD_RUNS)]
    descendants: Annotated[StrictInt, Field(ge=1)]
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _subtree_is_over_its_ceiling(self) -> Self:
        """Refuse a breach whose subtree sits within the ceiling.

        Raises:
            ValueError: ``descendants`` does not exceed ``ceiling``.
        """
        if self.descendants <= self.ceiling:
            raise ValueError(
                f"{self.descendants} Runs sit within a ceiling of {self.ceiling}; nothing breached"
            )
        return self


def child_grant(
    *, tool_grants: Sequence[str], authority: AuthorityGrant, parent: AuthorityCapsule
) -> tuple[tuple[str, ...], AuthorityGrant]:
    """Cut a child's grant to what its parent's sealed capsule holds.

    Args:
        tool_grants: The tools the child's own policy grants.
        authority: The authority the child's own policy resolves.
        parent: The parent's sealed capsule.

    Returns:
        The tools both grant, in the child's order, and each authority axis at
        the lower of the two levels.
    """
    held = {tool.value for tool in parent.semantic_tools}
    tools = tuple(tool for tool in tool_grants if tool in held)
    levels = {
        axis: min(getattr(authority, axis), getattr(parent.authority, axis), key=order.index)
        for axis, order in AUTHORITY_LEVEL_ORDER.items()
    }
    if len(tools) != len(tool_grants):
        logger.info(
            f"child_grant parent={parent.run_ref} requested={len(tool_grants)} kept={len(tools)}"
        )
    return tools, AuthorityGrant.model_validate(levels)


__all__ = [
    "MAX_CHILD_RUNS",
    "ChildCeilingBreach",
    "SubtreeOverrun",
    "child_grant",
    "resolved_child_runs",
    "subtree_overrun",
]
