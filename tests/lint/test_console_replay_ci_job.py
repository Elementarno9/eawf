"""The console golden contract is replayed by a workflow CI calls.

The 261-frame / 25-journey contract is only evidence for as long as something runs
it. The failure mode this guards is not a red replay but a replay nobody notices has
stopped existing: the job deleted, the workflow made uncallable so ci.yaml cannot run
it, a trigger of its own that reports outside the ci-ok verdict, or the step quietly
reduced to one of the three test files. When ci.yaml calls the replay is pinned by
tests/unit/test_ci_workflow_structure.py.

The checker takes a parsed workflow rather than reading the file, so each case can feed
it a synthetic defective workflow and prove the gate reds on a real defect instead of
asserting only that the healthy tree passes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_REPLAY = _REPO_ROOT / ".github" / "workflows" / "console-replay.yaml"

#: The job that must own the replay.
JOB_ID = "console-replay"

#: Every test file the replay job has to invoke. Dropping one silently narrows the
#: contract: without the journeys the reset is unproven, without the map the residual
#: diff is unchecked, without the frames there is no contract left.
REQUIRED_TESTS: tuple[str, ...] = (
    "tests/snapshots/tui/console/test_golden_frames.py",
    "tests/snapshots/tui/console/test_golden_journeys.py",
    "tests/snapshots/tui/console/test_normalisation_map.py",
)


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    """Return a workflow's trigger table.

    YAML 1.1 reads the bare key ``on`` as the boolean ``True``, so the table is looked
    up under both spellings rather than under the one a reader expects.

    Args:
        workflow: A parsed workflow document.
    """
    raw = workflow.get(True, workflow.get("on"))
    return raw if isinstance(raw, dict) else {}


def console_replay_violations(workflow: dict[Any, Any]) -> list[str]:
    """Return every reason ``workflow`` fails to replay the contract when CI calls it.

    Args:
        workflow: A parsed workflow document.

    Returns:
        One finding per defect, in a stable order; empty when the workflow is callable,
        carries no pull_request trigger of its own and its replay job invokes every
        required test file.
    """
    violations: list[str] = []
    on = triggers(workflow)
    if "workflow_call" not in on:
        violations.append("no workflow_call trigger: ci.yaml cannot call the replay")
    if "pull_request" in on:
        violations.append("a pull_request trigger reports the replay outside the ci-ok verdict")
    job = (workflow.get("jobs") or {}).get(JOB_ID)
    if job is None:
        violations.append(f"no {JOB_ID} job")
        return violations
    commands = " ".join(
        str(step.get("run", "")) for step in job.get("steps") or () if isinstance(step, dict)
    )
    if "pytest" not in commands:
        violations.append(f"the {JOB_ID} job runs no pytest step")
    violations.extend(
        f"the {JOB_ID} job does not invoke {path}"
        for path in REQUIRED_TESTS
        if path not in commands
    )
    return violations


def test_console_replay_workflow_replays_the_contract_when_called() -> None:
    workflow: dict[Any, Any] = yaml.safe_load(_CONSOLE_REPLAY.read_text(encoding="utf-8"))
    assert console_replay_violations(workflow) == []


def test_console_replay_job_runs_the_contract_single_process() -> None:
    """One app instance is the contract's ordering rule, so xdist stays off."""
    workflow: dict[Any, Any] = yaml.safe_load(_CONSOLE_REPLAY.read_text(encoding="utf-8"))
    commands = " ".join(
        str(step.get("run", "")) for step in workflow["jobs"][JOB_ID]["steps"] if "run" in step
    )
    assert "-p no:xdist" in commands


def _replay(trigger: str, run: str = "uv run pytest " + " ".join(REQUIRED_TESTS)) -> Any:
    return yaml.safe_load(
        f"on:\n  {trigger}:\njobs:\n  {JOB_ID}:\n    steps:\n      - run: {run}\n"
    )


def test_console_replay_violations_reds_on_a_missing_job() -> None:
    defective = yaml.safe_load("on:\n  workflow_call:\njobs:\n  other: {}\n")
    assert console_replay_violations(defective) == [f"no {JOB_ID} job"]


def test_console_replay_violations_reds_on_a_pull_request_trigger() -> None:
    defective = yaml.safe_load(
        "on:\n"
        "  workflow_call:\n"
        "  pull_request:\n"
        "jobs:\n"
        f"  {JOB_ID}:\n"
        "    steps:\n"
        "      - run: uv run pytest " + " ".join(REQUIRED_TESTS) + "\n"
    )
    assert console_replay_violations(defective) == [
        "a pull_request trigger reports the replay outside the ci-ok verdict"
    ]


def test_console_replay_violations_reds_on_an_uncallable_workflow() -> None:
    assert console_replay_violations(_replay("push")) == [
        "no workflow_call trigger: ci.yaml cannot call the replay"
    ]


def test_console_replay_violations_reds_on_a_dropped_test_file() -> None:
    defective = _replay("workflow_call", run=f"uv run pytest {REQUIRED_TESTS[0]}")
    assert console_replay_violations(defective) == [
        f"the {JOB_ID} job does not invoke {REQUIRED_TESTS[1]}",
        f"the {JOB_ID} job does not invoke {REQUIRED_TESTS[2]}",
    ]


def test_console_replay_violations_reds_on_a_job_with_no_pytest_step() -> None:
    defective = _replay("workflow_call", run="echo skipped")
    assert console_replay_violations(defective)[0] == f"the {JOB_ID} job runs no pytest step"


def test_console_replay_violations_reds_on_an_empty_workflow() -> None:
    assert console_replay_violations({}) == [
        "no workflow_call trigger: ci.yaml cannot call the replay",
        f"no {JOB_ID} job",
    ]


def test_triggers_reads_the_yaml_boolean_on_key() -> None:
    assert triggers(yaml.safe_load("on:\n  push:\n")) == {"push": None}
    assert triggers({"on": {"push": None}}) == {"push": None}
    assert triggers({}) == {}
    assert triggers({True: "push"}) == {}
