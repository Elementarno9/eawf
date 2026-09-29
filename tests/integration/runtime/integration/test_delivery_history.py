"""DEL-019: history carries coherent delivery projections, never per-Run commits.

The daemon authors history in exactly one place, the integration
workspace, and only the commits the configured delivery unit renders: one
per Batch or one per Task. Runs, Task status moves and receipts are
ledger rows; none of them is a commit, and none of their identities leaks
into the commit that projects the delivery. Replanning the same offering
renders the same commit, so a retry or a fresh receipt does not mint a
second projection of one delivery.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, get_args

import pytest

from eawf.kernel.config.schema import IntegrationCommitUnit
from eawf.kernel.delivery.receipts import RevisionBinding, RevisionRefKind
from eawf.kernel.runtime.candidate import CandidateBundle, SealCheck, candidate_identity
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.runtime.integration.apply import integration_order
from eawf.runtime.integration.git_workspace import (
    COMMIT_ARTIFACT_PREFIX,
    GitIntegrationWorkspace,
    candidate_pin_ref,
)
from eawf.runtime.integration.recovery import IntegrationPlan, plan_deliveries

pytestmark = pytest.mark.integration

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
REPOSITORY: Final = f"{CONTAINER}/repository/REP-EAWF"
BATCH: Final = f"{CONTAINER}/batch/BAT-0001"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
DIGEST: Final = f"sha256:{'c' * 64}"
RECEIPT_DIGEST: Final = f"sha256:{'b' * 64}"
SOURCE_ROOT: Final = Path(__file__).resolve().parents[4] / "src" / "eawf"


def git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A one-commit repository on ``main``."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "ci@example.com")
    git(root, "config", "user.name", "ci")
    (root / "notes.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    return root


def _candidate(repo: Path, *, base: str, task: str, run: int, path: str) -> CandidateBundle:
    """Commit one Run's work on a side branch and seal it as a candidate."""
    git(repo, "checkout", "-q", "-b", f"run-{run}", base)
    (repo / path).write_text(f"{task}\n", encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", f"run {run} of {task}")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    urn = f"{CONTAINER}/task/{task}"
    resulting = f"sha256:{sha[:2] * 32}"
    sealed = CandidateBundle.model_validate(
        {
            "candidate_ref": candidate_identity(task_ref=urn, resulting_tree_digest=resulting),
            "run_ref": f"{CONTAINER}/run/RUN-{run:08d}",
            "task_ref": urn,
            "submission_ref": f"{COMMIT_ARTIFACT_PREFIX}{sha}",
            "report_digest": RECEIPT_DIGEST,
            "verdict": AgentReportVerdict.PASS,
            "changed_paths": (path,),
            "resulting_tree_digest": resulting,
            "base_commit": base,
            "workspace_generation": 1,
            "checks_passed": tuple(SealCheck),
            "sealed_at": AT,
        }
    )
    git(repo, "update-ref", candidate_pin_ref(sealed.candidate_ref), sha)
    return sealed


def _binding(head: str) -> RevisionBinding:
    return RevisionBinding(
        repository_ref=REPOSITORY,
        ref_kind=RevisionRefKind.INTEGRATION,
        head_sha=head,
        tree_sha="3c" * 20,
        parent_sha=None,
        batch_ref=BATCH,
        integration_generation=1,
        manifest_digest=DIGEST,
        criteria_digest=DIGEST,
        policy_digest=DIGEST,
        environment_digest=None,
        bound_at=AT,
    )


def _plans(
    candidates: tuple[CandidateBundle, ...], *, base: str, unit: IntegrationCommitUnit
) -> tuple[IntegrationPlan, ...]:
    ordered = integration_order(candidates)
    return plan_deliveries(
        ordered,
        repository_ref=REPOSITORY,
        batch_ref=BATCH,
        source_base=_binding(base),
        target_base=_binding(base),
        parent_generation_id=None,
        subjects={item.candidate_ref: f"deliver {item.task_ref.entity_key}" for item in ordered},
        batch_subject="deliver the batch",
        unit=unit,
        task_reference="trailer",
    )


def _deliver(repo: Path, tmp_path: Path, plans: tuple[IntegrationPlan, ...], base: str) -> str:
    """Apply every plan in order on *base* and return the final delivered head."""
    head = base
    with GitIntegrationWorkspace(
        repo_root=repo, directory=tmp_path / "integration" / "int-0001", batch_ref=BATCH
    ) as workspace:
        for plan in plans:
            workspace.materialize(base_commit=head)
            for item in plan.ordered:
                workspace.apply(item)
            head = workspace.commit(plan.delivery).head_sha
    return head


def _two_runs(repo: Path) -> tuple[str, tuple[CandidateBundle, ...]]:
    base = git(repo, "rev-parse", "HEAD")
    return base, (
        _candidate(repo, base=base, task="EAWF-0001", run=11, path="a.txt"),
        _candidate(repo, base=base, task="EAWF-0002", run=12, path="b.txt"),
    )


def test_del_019_the_delivery_units_are_batch_and_task_only() -> None:
    assert set(get_args(IntegrationCommitUnit)) == {"batch", "task"}


def test_del_019_two_runs_in_a_batch_project_as_one_commit(repo: Path, tmp_path: Path) -> None:
    base, candidates = _two_runs(repo)
    head = _deliver(repo, tmp_path, _plans(candidates, base=base, unit="batch"), base)
    assert git(repo, "rev-list", "--count", f"{base}..{head}") == "1"
    message = git(repo, "log", "-1", "--format=%B", head)
    assert "Task: EAWF-0001" in message
    assert "Task: EAWF-0002" in message
    assert message.count("Eawf-Provenance: manifest://MFT-") == 1


def test_del_019_the_task_unit_projects_one_commit_per_task(repo: Path, tmp_path: Path) -> None:
    base, candidates = _two_runs(repo)
    plans = _plans(candidates, base=base, unit="task")
    head = _deliver(repo, tmp_path, plans, base)
    subjects = git(repo, "log", "--format=%s", f"{base}..{head}").splitlines()
    assert sorted(subjects) == ["deliver EAWF-0001", "deliver EAWF-0002"]


@pytest.mark.parametrize("unit", ["batch", "task"])
def test_del_019_no_run_or_receipt_identity_reaches_history(
    repo: Path, tmp_path: Path, unit: IntegrationCommitUnit
) -> None:
    base, candidates = _two_runs(repo)
    head = _deliver(repo, tmp_path, _plans(candidates, base=base, unit=unit), base)
    history = git(repo, "log", "--format=%B", f"{base}..{head}")
    assert "RUN-" not in history
    assert "/run/" not in history
    assert RECEIPT_DIGEST not in history
    assert "run 11" not in history


def test_del_019_replanning_one_offering_renders_the_same_commit(repo: Path) -> None:
    base, candidates = _two_runs(repo)
    first = _plans(candidates, base=base, unit="batch")[0].delivery
    again = _plans(tuple(reversed(candidates)), base=base, unit="batch")[0].delivery
    assert again == first


def test_del_019_the_integration_workspace_is_the_only_history_author() -> None:
    authors = sorted(
        str(path.relative_to(SOURCE_ROOT))
        for path in SOURCE_ROOT.rglob("*.py")
        if '"commit-tree"' in path.read_text(encoding="utf-8")
    )
    assert authors == ["runtime/integration/git_workspace.py"]
