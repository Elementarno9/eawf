"""The P34 audit's low edges stay closed.

Each check reads the source it guards and ships a companion that feeds the
checker a synthetic defect, so a regression reds instead of passing an
empty scan: a CI job condition that runs on a cancelled workflow, and an
admission module whose prose names a status its code never checks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from eawf.workflow.release import admission

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"


def always_job_violations(workflow: dict[str, Any]) -> list[str]:
    """Name every job whose condition keeps it running on a cancelled workflow.

    ``always()`` evaluates true after a cancel too, so a job that must survive
    a failed dependency spells that as ``!cancelled()`` instead.
    """
    return [
        name
        for name, job in workflow.get("jobs", {}).items()
        if "always()" in str(job.get("if", ""))
    ]


def test_ci_jobs_skip_cancelled_runs() -> None:
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    assert always_job_violations(workflow) == []
    guarded = [
        name
        for name, job in workflow["jobs"].items()
        if str(job.get("if", "")).startswith("!cancelled()")
    ]
    assert len(guarded) >= 2


def test_always_job_check_reds_on_a_planted_always() -> None:
    planted = {"jobs": {"test": {"if": "always() && needs.changes.outputs.code == 'true'"}}}
    assert always_job_violations(planted) == ["test"]


def test_admission_text_and_code_both_read_completed() -> None:
    source = Path(admission.__file__).read_text(encoding="utf-8")
    assert "ACCEPTED" not in source
    assert "COMPLETED Milestone" in (admission.__doc__ or "")
    assert "MilestoneStatus.COMPLETED" in source
