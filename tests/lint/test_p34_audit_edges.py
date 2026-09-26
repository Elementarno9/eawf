"""The P34 audit's low edges stay closed.

Each check reads the source it guards and ships a companion that feeds the
checker a synthetic defect, so a regression reds instead of passing an
empty scan: a CI job condition that runs on a cancelled workflow, an
admission module whose prose names a status its code never checks, and a
daemon agent writer that lands ``state.json`` without validating it. The
prune's expected-SHA guard and the phase-branch refusal are pinned in
``tests/unit/workflow/lifecycle/test_wave_archive_prune.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import yaml

from eawf.workflow.release import admission

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"
_AGENT_METHODS = _REPO_ROOT / "src" / "eawf" / "runtime" / "daemon" / "methods" / "agent.py"


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


def unvalidated_state_writes(source: str) -> list[int]:
    """Return the line of every ``write_state_unlocked`` call fed an unvalidated payload.

    A payload counts as validated when it is a ``_validated_state_payload``
    call, or a name assigned from one earlier in the same function.
    """

    def _is_validator_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_validated_state_payload"
        )

    lines: list[int] = []
    for func in ast.walk(ast.parse(source)):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        validated = {
            target.id
            for node in ast.walk(func)
            if isinstance(node, ast.Assign) and _is_validator_call(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(func):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "write_state_unlocked"
            ):
                continue
            payload = node.args[1] if len(node.args) > 1 else None
            if payload is None or not (
                _is_validator_call(payload)
                or (isinstance(payload, ast.Name) and payload.id in validated)
            ):
                lines.append(node.lineno)
    return sorted(set(lines))


def test_agent_state_writers_validate_before_writing() -> None:
    source = _AGENT_METHODS.read_text(encoding="utf-8")
    assert len(re.findall(r"\bwrite_state_unlocked\(", source)) >= 4
    assert unvalidated_state_writes(source) == []


def test_unvalidated_write_check_reds_on_a_raw_dump() -> None:
    planted = (
        "def writer(state, state_path):\n"
        "    payload = state.model_dump(mode='json')\n"
        "    write_state_unlocked(state_path, payload)\n"
    )
    assert unvalidated_state_writes(planted) == [3]
