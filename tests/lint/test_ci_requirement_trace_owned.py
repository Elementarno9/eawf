"""The CI requirement-trace job fails on any unaccounted requirement id.

``tools/requirement_trace.py check`` alone only proves the stored census is
fresh; an id nobody owns stays green unless the step passes
``--require-owned``. The job must also check out full history, because a
satisfied disposition cites the commit that built its ids and a shallow
clone cannot resolve it. Both are asserted on the workflow source, and a
companion test feeds the checker a copy with the flag removed to prove it
reds.
"""

from __future__ import annotations

import copy
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yaml"
_JOB = "requirement-trace"
_TOOL = "tools/requirement_trace.py"


def _trace_problems(workflow: dict[str, Any]) -> list[str]:
    """Return why *workflow*'s trace job would pass an unowned id, if it would."""
    job = workflow.get("jobs", {}).get(_JOB)
    if job is None:
        return [f"no {_JOB} job"]
    steps = job.get("steps", [])
    checks = [
        shlex.split(step["run"])
        for step in steps
        if _TOOL in step.get("run", "") and "check" in shlex.split(step["run"])
    ]
    problems = []
    if not checks:
        problems.append(f"no step runs {_TOOL} check")
    problems.extend(
        f"{shlex.join(argv)} omits --require-owned"
        for argv in checks
        if "--require-owned" not in argv
    )
    checkout = next(
        (step for step in steps if str(step.get("uses", "")).startswith("actions/checkout")), None
    )
    if checkout is None or (checkout.get("with") or {}).get("fetch-depth") != 0:
        problems.append("the checkout is shallow, so a satisfied commit cannot resolve")
    return problems


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))


def test_the_ci_trace_step_requires_every_id_owned() -> None:
    assert _trace_problems(_workflow()) == []


def test_removing_the_flag_reds_the_checker() -> None:
    workflow = copy.deepcopy(_workflow())
    for step in workflow["jobs"][_JOB]["steps"]:
        if _TOOL in step.get("run", ""):
            step["run"] = step["run"].replace(" --require-owned", "")

    assert _trace_problems(workflow) == [
        f"uv run python {_TOOL} check omits --require-owned",
    ]


def test_a_shallow_checkout_reds_the_checker() -> None:
    workflow = copy.deepcopy(_workflow())
    for step in workflow["jobs"][_JOB]["steps"]:
        step.pop("with", None)

    assert _trace_problems(workflow) == [
        "the checkout is shallow, so a satisfied commit cannot resolve"
    ]


def test_a_missing_job_or_step_reds_the_checker() -> None:
    assert _trace_problems({"jobs": {}}) == [f"no {_JOB} job"]
    assert _trace_problems({"jobs": {_JOB: {"steps": []}}}) == [
        f"no step runs {_TOOL} check",
        "the checkout is shallow, so a satisfied commit cannot resolve",
    ]
