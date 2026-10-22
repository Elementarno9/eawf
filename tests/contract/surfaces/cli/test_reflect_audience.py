"""/reflect stays operator-only: an agent lane may never start a reflection run.

Requirement row proved here, by id:

- ``SKILL-034``: /reflect stays ``user_only`` and an agent never initiates a run, while
  the report it emits stays readable by an agent.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.workflow.skills.arguments import InvocationRefusedError, check_invocation
from eawf.workflow.skills.catalog import resolve_skill, skill_lanes

runner = CliRunner()


def test_reflect_is_user_only() -> None:
    """The catalog keeps the skill out of the agent lane."""
    entry = resolve_skill("reflect")
    assert entry.audience == "user_only"
    assert skill_lanes(entry) == frozenset({"operator"})


@pytest.mark.parametrize(
    "args",
    [
        {"action": "run"},
        {"action": "run", "local_only": True},
        {"action": "run", "window": "7d", "scope": "project", "format": "json"},
        {},
    ],
    ids=["bare-run", "local-only", "selection-flags", "no-action"],
)
def test_an_agent_invocation_of_reflect_run_is_refused(args: dict[str, object]) -> None:
    """Every agent-lane invocation is refused before a Run starts, whatever its arguments."""
    with pytest.raises(InvocationRefusedError) as refused:
        check_invocation(resolve_skill("reflect"), args, lane="agent")
    assert refused.value.code == "lane_refused"


def test_the_operator_lane_takes_the_selection_flags() -> None:
    """Boundary: the operator lane admits run with --window, --scope and --format."""
    args = {"action": "run", "window": "7d", "scope": "milestone:MLS-0104", "format": "json"}
    check_invocation(resolve_skill("reflect"), args, lane="operator")


def test_a_selection_flag_on_another_action_is_refused() -> None:
    """Error path: --scope belongs to run alone."""
    with pytest.raises(InvocationRefusedError) as refused:
        check_invocation(
            resolve_skill("reflect"), {"action": "show", "scope": "project"}, lane="operator"
        )
    assert refused.value.code == "action_incompatible"


def test_the_skill_runner_refuses_the_agent_lane() -> None:
    """The headless skill runner exits non-zero naming the refusal."""
    result = runner.invoke(
        app,
        ["skill", "run", "/reflect", "--lane", "agent"],
        input=json.dumps({"action": "run"}),
    )
    assert result.exit_code != 0
    assert "lane_refused" in result.output
