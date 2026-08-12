"""Cross-field validator tests for :class:`PrepBody`.

Mechanizes the ``/prep`` DAG-render rule on the planning path. A body that
claims to plan an iter (``no_op`` and ``blocked`` both ``False``) MUST carry:

- a non-empty ``dag``;
- waves whose every referenced task reconciles to a ``dag`` task;
- tasks whose every dep references an existing task id.

The two lifecycle stub paths (``no_op=True`` already-active idempotent and
``blocked=True`` closed-phase) keep ``dag`` optional -- the conditional
exemption. These tests pin each failure mode (empty dag, unreconciled wave,
dangling dep) and each clean path (a fully-reconciled plan, a no_op stub, a
blocked stub).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.workflow.skills.bodies.prep import PrepBody


def _reconciled_payload() -> dict[str, Any]:
    """Return a fully-reconciled planning-body payload (two tasks, one dep)."""
    return {
        "iter_id": "P00-I01",
        "objective": "plan P00-I01",
        "dag": [
            {"task_id": "P00-I01-W01", "risk": "low"},
            {"task_id": "P00-I01-W02", "deps": ["P00-I01-W01"], "risk": "low"},
        ],
        "waves": [
            {
                "wave_id": "P00-I01",
                "tasks": ["P00-I01-W01", "P00-I01-W02"],
                "worktree_policy": "auto",
                "estimate_eu": 2.0,
            }
        ],
    }


def test_planning_body_with_reconciled_dag_validates() -> None:
    """A non-empty, fully-reconciled planning body validates clean."""
    body = PrepBody.model_validate(_reconciled_payload())
    assert body.no_op is False
    assert body.blocked is False
    assert {task.task_id for task in body.dag} == {"P00-I01-W01", "P00-I01-W02"}


def test_planning_body_empty_dag_raises() -> None:
    """An empty dag on the planning path is rejected."""
    payload = _reconciled_payload()
    payload["dag"] = []
    payload["waves"] = []
    with pytest.raises(ValidationError, match="non-empty dag"):
        PrepBody.model_validate(payload)


def test_planning_body_unreconciled_wave_raises_naming_task() -> None:
    """A wave that references a task absent from the dag is rejected, named."""
    payload = _reconciled_payload()
    payload["waves"][0]["tasks"] = ["P00-I01-W01", "P00-I01-W99"]
    with pytest.raises(ValidationError, match="'P00-I01-W99'"):
        PrepBody.model_validate(payload)


def test_planning_body_dangling_dep_raises_naming_dep() -> None:
    """A task whose dep references a non-existent task is rejected, named."""
    payload = _reconciled_payload()
    payload["dag"][1]["deps"] = ["P00-I01-W42"]
    with pytest.raises(ValidationError, match="dangling dep 'P00-I01-W42'"):
        PrepBody.model_validate(payload)


def test_no_op_body_with_no_dag_validates() -> None:
    """A ``no_op=True`` body validates clean with no dag (the exemption)."""
    body = PrepBody(iter_id="P00-I01", objective="no-op", no_op=True)
    assert body.no_op is True
    assert body.dag == []


def test_blocked_body_with_no_dag_validates() -> None:
    """A ``blocked=True`` closed-phase stub validates clean with no dag."""
    body = PrepBody(iter_id="P00-I01", objective="blocked", blocked=True)
    assert body.blocked is True
    assert body.dag == []


def test_planning_body_dangling_dep_message_substring() -> None:
    """The dangling-dep raise also names the offending task id."""
    payload = _reconciled_payload()
    payload["dag"][1]["deps"] = ["P00-I01-W42"]
    with pytest.raises(ValidationError, match="task 'P00-I01-W02'"):
        PrepBody.model_validate(payload)
