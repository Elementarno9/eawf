"""``eawf goal define`` mutator.

Creates a :class:`~eawf.state.models.Goal` record under ``state.goals`` and
mutates the typed state in place. The CLI handler runs the mutator inside
:func:`eawf.cli._mutation.state_transaction`, which holds
``portalock(state.json)`` across load + mutate + validate + write.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from eawf.cli.errors import InvalidInput
from eawf.evidence import _io
from eawf.state.enums import GoalStatus
from eawf.state.models import Goal, State
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


def define_goal(
    state: State,
    *,
    goal_id: str,
    title: str,
    summary: str,
    scope_id: str,
    outcome_ids: list[str] | None = None,
) -> Envelope:
    """Add a new goal in place and return the event envelope.

    Args:
        state: Current typed state. Mutated in place: ``state.goals`` is
            extended (or initialised) and ``state.updated_at`` advances.
        goal_id: Project-unique goal id (free-form non-empty string).
        title: Human-readable goal title.
        summary: 1-2 sentence elaboration (stored in ``summary``).
        scope_id: Owning scope (project/subproject id).
        outcome_ids: Optional outcome ids to seed (typically empty).

    Raises:
        InvalidInput: When ``goal_id`` is already present.
    """
    goals: dict[str, Goal] = dict(state.goals or {})
    if goal_id in goals:
        raise InvalidInput(f"goal {goal_id!r} already exists")

    now = datetime.now(UTC)
    goal = Goal(
        id=goal_id,
        scope_id=scope_id,
        title=title,
        summary=summary,
        status=GoalStatus.OPEN,
        outcome_ids=list(outcome_ids or []),
        created_at=now,
        closed_at=None,
    )
    goals[goal_id] = goal
    state.goals = goals
    state.updated_at = now

    event_args: dict[str, Any] = {
        "goal_id": goal_id,
        "title": title,
        "scope_id": scope_id,
    }
    return _io.event_envelope(
        event_id=f"EVT-goal-define-{goal_id}-{int(now.timestamp() * 1000)}",
        scope_id=scope_id,
        event_type="goal.define",
        actor="cli",
        command="goal define",
        args=event_args,
        summary=f"goal {goal_id} defined ({title!r})",
    )
