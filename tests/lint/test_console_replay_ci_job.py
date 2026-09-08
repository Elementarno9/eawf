"""The console golden contract is replayed by CI on every push.

The 261-frame / 25-journey contract is only evidence for as long as something runs
it. It was measured once under a gitignored spike, so the failure mode this guards is
not a red replay but a replay nobody notices has stopped existing: the job deleted, the
trigger narrowed to the protected branches, or the step quietly reduced to one of the
three test files.

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
    """Return every reason ``workflow`` fails to replay the contract on every push.

    Args:
        workflow: A parsed workflow document.

    Returns:
        One finding per defect, in a stable order; empty when the workflow declares
        the replay job under an unfiltered push trigger and the job's steps invoke
        every required test file.
    """
    violations: list[str] = []
    on = triggers(workflow)
    if "push" not in on:
        violations.append("no push trigger: the replay must run on every push")
    else:
        filters = on["push"] or {}
        if isinstance(filters, dict) and "branches" in filters:
            violations.append(
                "the push trigger is branch-filtered; the replay must run on every push"
            )
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


def test_console_replay_workflow_replays_the_contract_on_every_push() -> None:
    workflow: dict[Any, Any] = yaml.safe_load(_CONSOLE_REPLAY.read_text(encoding="utf-8"))
    assert console_replay_violations(workflow) == []


def test_console_replay_job_runs_the_contract_single_process() -> None:
    """One app instance is the contract's ordering rule, so xdist stays off."""
    workflow: dict[Any, Any] = yaml.safe_load(_CONSOLE_REPLAY.read_text(encoding="utf-8"))
    commands = " ".join(
        str(step.get("run", "")) for step in workflow["jobs"][JOB_ID]["steps"] if "run" in step
    )
    assert "-p no:xdist" in commands


def test_console_replay_violations_reds_on_a_missing_job() -> None:
    defective = yaml.safe_load("on:\n  push:\njobs:\n  other: {}\n")
    assert console_replay_violations(defective) == [f"no {JOB_ID} job"]


def test_console_replay_violations_reds_on_a_branch_filtered_push() -> None:
    defective = yaml.safe_load(
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "jobs:\n"
        f"  {JOB_ID}:\n"
        "    steps:\n"
        "      - run: uv run pytest " + " ".join(REQUIRED_TESTS) + "\n"
    )
    assert console_replay_violations(defective) == [
        "the push trigger is branch-filtered; the replay must run on every push"
    ]


def test_console_replay_violations_reds_on_a_pull_request_only_workflow() -> None:
    defective = yaml.safe_load(
        "on:\n"
        "  pull_request:\n"
        "jobs:\n"
        f"  {JOB_ID}:\n"
        "    steps:\n"
        "      - run: uv run pytest " + " ".join(REQUIRED_TESTS) + "\n"
    )
    assert console_replay_violations(defective) == [
        "no push trigger: the replay must run on every push"
    ]


def test_console_replay_violations_reds_on_a_dropped_test_file() -> None:
    defective = yaml.safe_load(
        "on:\n"
        "  push:\n"
        "jobs:\n"
        f"  {JOB_ID}:\n"
        "    steps:\n"
        "      - run: uv run pytest " + REQUIRED_TESTS[0] + "\n"
    )
    assert console_replay_violations(defective) == [
        f"the {JOB_ID} job does not invoke {REQUIRED_TESTS[1]}",
        f"the {JOB_ID} job does not invoke {REQUIRED_TESTS[2]}",
    ]


def test_console_replay_violations_reds_on_a_job_with_no_pytest_step() -> None:
    defective = yaml.safe_load(
        f"on:\n  push:\njobs:\n  {JOB_ID}:\n    steps:\n      - run: echo skipped\n"
    )
    assert console_replay_violations(defective)[0] == f"the {JOB_ID} job runs no pytest step"


def test_console_replay_violations_reds_on_an_empty_workflow() -> None:
    assert console_replay_violations({}) == [
        "no push trigger: the replay must run on every push",
        f"no {JOB_ID} job",
    ]


def test_triggers_reads_the_yaml_boolean_on_key() -> None:
    assert triggers(yaml.safe_load("on:\n  push:\n")) == {"push": None}
    assert triggers({"on": {"push": None}}) == {"push": None}
    assert triggers({}) == {}
    assert triggers({True: "push"}) == {}
