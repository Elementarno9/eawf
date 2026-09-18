"""The plugins-dist job leaves a receipt behind, whatever it concluded.

The Codex leg pushes a rendered tree to a branch and then the runner is
gone. Everything the release ledger later learns about that push it
learns from a ``publication-receipt-plugins-dist.json`` the job uploaded,
so a job that uploads one only when it succeeded is worse than useless:
the failure it needs to report is exactly the case that leaves no
evidence, and the checkpoint goes on to bake on the legs that did
report.

The check reads the workflow source because the defect lives in a job
that runs only on a tag push, and it ships with companions that feed the
checker the two defective shapes -- no receipt at all, and a receipt
conditioned on success -- so the gate is known to red on a real one
rather than only on a stub.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.workflow.release.publication_receipt import receipt_filename

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_RELEASE = _REPO_ROOT / ".github" / "workflows" / "plugin-release.yaml"

#: The job that publishes the Codex tree to the branch.
_PLUGINS_DIST_JOB = "publish-codex-branch"

#: The leg the job publishes, and the receipt filename it owns.
_TARGET_ID = "plugins-dist"
_RECEIPT_FILE = receipt_filename(_TARGET_ID)

#: The part of the filename a step assembling it from ``TARGET_ID``
#: still spells out.
_RECEIPT_STEM = "publication-receipt-"

#: The ``if`` expression that makes a step run on a failed job too.
_ALWAYS = "always()"

#: Every field ``PublicationReceipt`` requires the job to record.
_RECEIPT_KEYS = ("target_id", "version", "artifact_digests", "job_conclusion", "run_id")


def _load(path: Path) -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return workflow


def _steps(workflow: dict[str, Any], job_id: str) -> list[dict[str, Any]]:
    """Return the step mappings of *job_id*, or an empty list."""
    job = workflow.get("jobs", {}).get(job_id)
    if not isinstance(job, dict):
        return []
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _runs_unconditionally(step: dict[str, Any]) -> bool:
    """Return whether *step* runs even when the job already failed."""
    return str(step.get("if", "")).strip() == _ALWAYS


def _writes_receipt(step: dict[str, Any]) -> bool:
    """Return whether *step* renders this leg's receipt.

    The receipt name is assembled from ``TARGET_ID`` inside the script
    rather than spelled out, so the leg is identified by the environment
    the step declares and not by a substring of the body.
    """
    env = step.get("env") or {}
    return _RECEIPT_STEM in str(step.get("run", "")) and str(env.get("TARGET_ID", "")) == _TARGET_ID


def _uploads_receipt(step: dict[str, Any]) -> bool:
    """Return whether *step* uploads this leg's receipt file."""
    return "upload-artifact" in str(step.get("uses", "")) and _RECEIPT_FILE in str(
        (step.get("with") or {}).get("path", "")
    )


def plugins_dist_receipt_violations(workflow: dict[str, Any]) -> list[str]:
    """Return why *workflow* leaves the plugins-dist leg unreceipted.

    Args:
        workflow: A decoded GitHub Actions workflow document.

    Returns:
        One line per defect, empty when the job writes and uploads a
        complete receipt whatever its conclusion.
    """
    steps = _steps(workflow, _PLUGINS_DIST_JOB)
    if not steps:
        return [f"workflow declares no {_PLUGINS_DIST_JOB!r} job with steps"]
    problems: list[str] = []
    writers = [step for step in steps if _writes_receipt(step)]
    uploads = [step for step in steps if _uploads_receipt(step)]
    if not writers:
        problems.append(f"{_PLUGINS_DIST_JOB} writes no {_RECEIPT_FILE}")
    if not uploads:
        problems.append(f"{_PLUGINS_DIST_JOB} uploads no {_RECEIPT_FILE}")
    for step in writers + uploads:
        if not _runs_unconditionally(step):
            problems.append(
                f"step {str(step.get('name', '?'))!r} handles {_RECEIPT_FILE} under "
                f"if {step.get('if', '<none>')!r}, so a failed job leaves none"
            )
    for step in writers:
        body = str(step.get("run", ""))
        missing = sorted(key for key in _RECEIPT_KEYS if f'"{key}"' not in body)
        if missing:
            problems.append(f"the {_RECEIPT_FILE} written by this job records no {missing}")
    return problems


def _without_receipt_steps(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return *workflow* with the plugins-dist receipt steps removed."""
    defective = copy.deepcopy(workflow)
    job = defective["jobs"][_PLUGINS_DIST_JOB]
    job["steps"] = [
        step for step in job["steps"] if not (_writes_receipt(step) or _uploads_receipt(step))
    ]
    return defective


def test_plugins_dist_job_uploads_a_publication_receipt() -> None:
    assert plugins_dist_receipt_violations(_load(_PLUGIN_RELEASE)) == []


def test_plugins_dist_receipt_gate_reds_on_a_receiptless_job() -> None:
    defective = _without_receipt_steps(_load(_PLUGIN_RELEASE))
    problems = plugins_dist_receipt_violations(defective)
    assert problems == [
        f"{_PLUGINS_DIST_JOB} writes no {_RECEIPT_FILE}",
        f"{_PLUGINS_DIST_JOB} uploads no {_RECEIPT_FILE}",
    ]


def test_plugins_dist_receipt_gate_reds_on_a_receipt_only_written_on_success() -> None:
    """The shape the hole actually took: evidence only when it is not needed."""
    defective = copy.deepcopy(_load(_PLUGIN_RELEASE))
    for step in defective["jobs"][_PLUGINS_DIST_JOB]["steps"]:
        if _writes_receipt(step) or _uploads_receipt(step):
            step.pop("if", None)
    problems = plugins_dist_receipt_violations(defective)
    assert len(problems) == 2
    assert all("a failed job leaves none" in problem for problem in problems)


def test_plugins_dist_receipt_gate_reds_on_a_receipt_missing_a_ledger_field() -> None:
    defective = copy.deepcopy(_load(_PLUGIN_RELEASE))
    for step in defective["jobs"][_PLUGINS_DIST_JOB]["steps"]:
        if _writes_receipt(step):
            step["run"] = str(step["run"]).replace('"job_conclusion"', '"conclusion"')
    problems = plugins_dist_receipt_violations(defective)
    assert problems == [f"the {_RECEIPT_FILE} written by this job records no ['job_conclusion']"]


def test_plugins_dist_receipt_gate_reds_on_a_workflow_without_the_job() -> None:
    assert plugins_dist_receipt_violations({"jobs": {}}) == [
        f"workflow declares no {_PLUGINS_DIST_JOB!r} job with steps"
    ]
