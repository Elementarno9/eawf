"""The real-bwrap containment test runs in the CI job that installs bubblewrap.

``test_bwrap_containment.py`` launches the real ``bwrap`` and skips on a host
without it. The ``test`` matrix installs no bubblewrap, so unless the
``linux-jail`` job names the file in its pytest step the containment cases skip
on every CI host and a jail that leaks reads as green.

The checker takes a parsed workflow, so each defect case feeds it a synthetic
workflow and proves the gate reds rather than only that the healthy tree passes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_CI_WORKFLOW: Final = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"

#: The job that installs bubblewrap before running the sandbox tests.
JOB_ID: Final = "linux-jail"

#: Every test file the job's pytest step has to name.
REQUIRED_TESTS: Final[tuple[str, ...]] = (
    "tests/integration/runtime/sandbox/test_jail.py",
    "tests/integration/runtime/sandbox/test_bwrap_containment.py",
    "tests/unit/runtime/sandbox/test_jail_expiry.py",
    "tests/unit/runtime/sandbox/test_env_scrub.py",
)


def bwrap_job_violations(workflow: dict[Any, Any]) -> list[str]:
    """Return every reason *workflow* would skip a real-bwrap test file.

    Args:
        workflow: A parsed workflow document.

    Returns:
        One finding per defect, in a stable order; empty when the job installs
        bubblewrap and one of its pytest steps names every required file.
    """
    job = (workflow.get("jobs") or {}).get(JOB_ID)
    if not isinstance(job, dict):
        return [f"no {JOB_ID} job"]
    runs = [str(step.get("run", "")) for step in job.get("steps") or () if isinstance(step, dict)]
    violations: list[str] = []
    if not any("bubblewrap" in run for run in runs):
        violations.append(f"the {JOB_ID} job installs no bubblewrap")
    pytest_runs = [run for run in runs if "pytest" in run]
    if not pytest_runs:
        violations.append(f"the {JOB_ID} job runs no pytest step")
        return violations
    tokens = {token for run in pytest_runs for token in run.split()}
    violations.extend(
        f"the {JOB_ID} pytest step does not name {path}"
        for path in REQUIRED_TESTS
        if path not in tokens
    )
    return violations


def _workflow(*runs: str) -> dict[Any, Any]:
    """Return a synthetic workflow whose ``linux-jail`` job runs *runs*."""
    return {"jobs": {JOB_ID: {"steps": [{"run": run} for run in runs]}}}


def test_the_linux_jail_job_runs_the_bwrap_containment_test() -> None:
    workflow: dict[Any, Any] = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))

    assert bwrap_job_violations(workflow) == []


def test_a_step_without_the_containment_file_is_refused() -> None:
    """The defect this gate exists for: the file the job never ran."""
    named = " ".join(path for path in REQUIRED_TESTS if "containment" not in path)
    defective = _workflow("sudo apt-get install -y bubblewrap", f"uv run pytest {named}")

    assert bwrap_job_violations(defective) == [
        f"the {JOB_ID} pytest step does not name {REQUIRED_TESTS[1]}"
    ]


def test_a_job_without_bubblewrap_is_refused() -> None:
    defective = _workflow(f"uv run pytest {' '.join(REQUIRED_TESTS)}")

    assert bwrap_job_violations(defective) == [f"the {JOB_ID} job installs no bubblewrap"]


def test_a_job_without_a_pytest_step_is_refused() -> None:
    defective = _workflow("sudo apt-get install -y bubblewrap")

    assert bwrap_job_violations(defective) == [f"the {JOB_ID} job runs no pytest step"]


def test_a_workflow_without_the_job_is_refused() -> None:
    assert bwrap_job_violations({}) == [f"no {JOB_ID} job"]
    assert bwrap_job_violations({"jobs": {JOB_ID: "not a job"}}) == [f"no {JOB_ID} job"]


def test_a_prefix_of_the_file_name_does_not_count() -> None:
    """A path is matched as a whole argument, never as a substring of another."""
    truncated = [path.removesuffix(".py") for path in REQUIRED_TESTS]
    defective = _workflow("apt-get install bubblewrap", f"uv run pytest {' '.join(truncated)}")

    assert len(bwrap_job_violations(defective)) == len(REQUIRED_TESTS)
