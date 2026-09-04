"""The release pipeline really exposes the three receipts it promises.

``tests/lint/test_phase_ci_gates.py`` reads the workflow *source* and
asserts the job is declared, needed and uploading the right three names.
That is the every-wave half, and it can only prove the YAML says the
right thing.

This is the other half. The ship-cadence check asks GitHub what the
dry-run workflow run for the ship commit actually produced, because a
job can be declared perfectly and still upload nothing: a producer that
exits zero without writing, a path that resolved to an empty directory,
an upload skipped by a condition nobody noticed. Only the run knows.

It is opt-in (``EAWF_SHIP_CADENCE=1``) because it needs a pushed commit,
a completed workflow run and a ``gh`` credential -- none of which exist
while a wave is being written. Skipped, the selector exits 0; the
checker it would call is pinned unconditionally by the two cases above
it, so the live half cannot silently rot into a check that passes on
anything.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.workflow.release.receipts import RECEIPT_FILENAMES

pytestmark = pytest.mark.integration

#: Repository root, four directories above ``tests/integration/...``.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: Workflow whose run is inspected.
RELEASE_WORKFLOW = "release.yaml"

#: Opt-in switch. The check needs a pushed commit and a completed run,
#: so it is a ship-cadence step rather than a per-wave one.
SHIP_CADENCE_ENV = "EAWF_SHIP_CADENCE"

#: Overrides which commit's run is inspected; defaults to ``HEAD``.
SHIP_SHA_ENV = "EAWF_SHIP_CADENCE_SHA"

#: Wall budget for one ``gh api`` call.
GH_TIMEOUT_SECONDS = 60


def missing_receipt_artifacts(payload: Any) -> list[str]:
    """Return the declared receipts a workflow run did not expose.

    Args:
        payload: Decoded ``/actions/runs/<id>/artifacts`` response.

    Returns:
        The missing artifact names, sorted; empty when the run exposed
        every receipt the sweep reads back.

    Raises:
        ValueError: When the payload carries no ``artifacts`` list. An
            unreadable answer must not read as "nothing is missing".
    """
    artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
    if not isinstance(artifacts, list):
        raise ValueError(f"workflow run payload carries no 'artifacts' list: {payload!r:.200}")
    exposed = {row.get("name") for row in artifacts if isinstance(row, dict)}
    return sorted(set(RECEIPT_FILENAMES) - exposed)


def _gh_api(path: str) -> Any:
    """Return the decoded ``gh api`` answer for *path*."""
    completed = subprocess.run(
        ["gh", "api", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"gh api {path} exited {completed.returncode}: {completed.stderr[-400:]}")
    return json.loads(completed.stdout)


def _ship_sha() -> str:
    """Return the commit whose workflow run is inspected."""
    pinned = os.environ.get(SHIP_SHA_ENV)
    if pinned:
        return pinned
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_SECONDS,
        check=True,
    ).stdout.strip()


# --- the checker, pinned unconditionally -------------------------------------


def test_inventory_artifacts_checker_accepts_a_complete_run() -> None:
    """A run exposing all three receipts has nothing missing."""
    payload = {"artifacts": [{"name": name} for name in RECEIPT_FILENAMES]}
    assert missing_receipt_artifacts(payload) == []


def test_inventory_artifacts_checker_names_every_absent_receipt() -> None:
    """The gate fires on the real defect: a run that uploaded only the dist."""
    payload = {"artifacts": [{"name": "dist"}, {"name": "dependency-manifest"}]}
    assert missing_receipt_artifacts(payload) == [
        "reproducible-build-receipt",
        "vulnerability-report",
    ]


def test_inventory_artifacts_checker_refuses_an_unreadable_answer() -> None:
    """An API answer with no artifact list must not read as a clean run."""
    with pytest.raises(ValueError, match="no 'artifacts' list"):
        missing_receipt_artifacts({"total_count": 0})


# --- the ship-cadence half ---------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get(SHIP_CADENCE_ENV) or shutil.which("gh") is None,
    reason=f"ship-cadence live check; set {SHIP_CADENCE_ENV}=1 with gh authenticated",
)
def test_inventory_artifacts_are_exposed_by_the_dry_run_workflow_run() -> None:
    """The dry-run release run for the ship commit exposes all three receipts."""
    sha = _ship_sha()
    repo = _gh_api("repos/{owner}/{repo}")["full_name"]
    runs = _gh_api(
        f"repos/{repo}/actions/workflows/{RELEASE_WORKFLOW}/runs?head_sha={sha}&per_page=1"
    )["workflow_runs"]
    assert runs, f"no {RELEASE_WORKFLOW} run exists for {sha[:12]}; dispatch a dry-run first"
    run = runs[0]
    assert run["status"] == "completed", f"run {run['id']} is {run['status']}, not completed"
    missing = missing_receipt_artifacts(_gh_api(f"repos/{repo}/actions/runs/{run['id']}/artifacts"))
    assert missing == [], (
        f"the {RELEASE_WORKFLOW} run for {sha[:12]} exposed no {missing} artifact(s); "
        f"the inventory-and-reproducibility job declared them but produced nothing"
    )
