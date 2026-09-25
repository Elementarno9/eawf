"""``runtime.delivery.integrate`` delivers through a real git worktree.

The canary here is walked the way a caller on the socket walks it: a Run
is dispatched for real and leased a worktree, the worker commits in it,
the candidate is submitted and pinned, its report is bound and sealed,
and only then is the Batch integrated through the registered verb. The
integration worktree is the daemon's own, and every case checks it is
gone when the call returns -- after a delivery, after a conflict, and
after a refusal raised halfway through.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

import eawf.runtime.daemon.methods.delivery as delivery_methods
from eawf.kernel.delivery.integration import IntegrationGeneration
from eawf.kernel.state.epoch2.urns import BatchUrn
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.candidate import (
    CANDIDATE_REPORT_BIND_METHOD,
    CANDIDATE_SUBMIT_METHOD,
)
from eawf.runtime.daemon.methods.delivery import DELIVERY_INTEGRATE_METHOD
from eawf.runtime.integration.git_workspace import (
    GitIntegrationWorkspace,
    candidate_pin_ref,
    delivery_pin_ref,
    git_integration_workspace,
)
from eawf.runtime.integration.recovery import generation_record_key
from tests.integration.runtime.daemon.methods.test_delivery_candidate_sealing import (
    SUCCESS,
    commit_in_lease,
    dispatched,
    report_params,
    submit_params,
)
from tests.integration.runtime.daemon.test_native_dispatch import ACTOR, root_ctx
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration


REPAIR_TASK: Final = f"{world.CONTAINER}/task/EAWF-0050"
EVIDENCE: Final = f"{world.CONTAINER}/evidence/EVD-0001"
BRANCH: Final = "feature/canary-delivery"


def git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def call(method: str, ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one registered verb the way the socket listener does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


class Opened:
    """Every integration workspace the verb opened, through the real factory."""

    def __init__(self) -> None:
        """Start with nothing opened."""
        self.workspaces: list[GitIntegrationWorkspace] = []

    def __call__(self, context: Epoch2RootContext, batch_ref: BatchUrn) -> GitIntegrationWorkspace:
        """Open the shipped workspace and remember where it lives."""
        workspace = git_integration_workspace(context, batch_ref)
        self.workspaces.append(workspace)
        return workspace


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> Iterator[Opened]:
    """Record the workspaces the verb opens without replacing what it opens."""
    recorder = Opened()
    monkeypatch.setattr(delivery_methods, "INTEGRATION_WORKSPACE_FACTORY", recorder)
    yield recorder


def sealed_canary(tmp_path: Path) -> tuple[CanaryProvision, Path, MethodContext, str, str]:
    """Walk one Task from dispatch to a sealed candidate on a real commit.

    Returns:
        The canary, its runtime directory, the daemon context, the base
        commit the lease was issued at, and the sealed candidate's ref.
    """
    canary, runtime, ctx = dispatched(tmp_path)
    context = root_ctx(canary, runtime)
    world.seed_task(context, world.task_row())
    base = git(canary.root, "rev-parse", "main")
    ref = commit_in_lease(canary, runtime)
    claim = SUCCESS["submission"]
    call(CANDIDATE_SUBMIT_METHOD, ctx, submit_params(canary, claim=claim, submission_ref=ref))
    sealed = call(
        CANDIDATE_REPORT_BIND_METHOD,
        ctx,
        report_params(canary, claim=claim, report=SUCCESS["report"]),
    )
    assert sealed["sealed"] is True
    return canary, runtime, ctx, base, sealed["candidate_ref"]


def integrate_params(canary: CanaryProvision, *, base: str, candidate: str) -> dict[str, Any]:
    """Return the wire params of one integration of the canary Batch."""
    return {
        "repo_root": str(canary.root),
        "urn": world.BATCH,
        "actor": ACTOR,
        "idempotency_key": "integrate-01",
        "base": world.binding(generation=1, head_sha=base).model_dump(mode="json"),
        "branch": BRANCH,
        "subject": "Deliver the canary batch",
        "subjects": {candidate: "Deliver the canary task"},
        "exit_refs": {"repair_task": REPAIR_TASK, "rebase_task": REPAIR_TASK},
        "diagnostic_ref": EVIDENCE,
    }


def seed_head(canary: CanaryProvision, runtime: Path, *, base: str, head: str) -> None:
    """File generation two of the Batch, delivered at commit *head*."""
    generation: IntegrationGeneration = world.generation(
        ordinal=2,
        head_sha=head,
        target_base=world.binding(generation=1, head_sha=base),
        tasks=(world.OTHER_TASK,),
        selected=True,
    )
    world.seed_lines(
        root_ctx(canary, runtime),
        world.BATCH,
        [
            LedgerRecord(
                collection=Epoch2Collection.BATCH,
                record_key=generation_record_key(generation),
                status="selected",
                recorded_at=world.AT,
                payload=generation.model_dump(mode="json"),
            )
        ],
    )


def side_commit(canary: CanaryProvision, *, path: str, content: str) -> str:
    """Commit *content* to *path* on a side branch of main and return the commit."""
    root = canary.root
    git(root, "checkout", "-q", "-b", "delivered-earlier", "main")
    (root / path).write_text(content, encoding="utf-8")
    git(root, "add", path)
    git(root, "commit", "-q", "-m", "delivered earlier")
    sha = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "main")
    return sha


def batch_lines(canary: CanaryProvision, runtime: Path) -> tuple[LedgerRecord, ...]:
    """Return every line of the canary's Batch ledger."""
    with root_ctx(canary, runtime).session([world.BATCH]) as session:
        return read_ledger_records(session.ledger_path(Epoch2Collection.BATCH))


def assert_removed(canary: CanaryProvision, opened: Opened) -> None:
    """Assert every workspace the verb opened is gone and unregistered."""
    assert opened.workspaces, "the verb opened an integration workspace"
    for workspace in opened.workspaces:
        assert not workspace.directory.exists()
    registered = git(canary.root, "worktree", "list", "--porcelain")
    assert "local/epoch2/integration" not in registered


def test_one_sealed_candidate_is_delivered_with_a_selected_generation(
    tmp_path: Path, opened: Opened
) -> None:
    """The verb delivers, selects generation two and pins the real commit."""
    canary, runtime, ctx, base, candidate = sealed_canary(tmp_path)

    answer = call(
        DELIVERY_INTEGRATE_METHOD, ctx, integrate_params(canary, base=base, candidate=candidate)
    )

    assert answer["delivered"] is True
    assert answer["generation_ids"] == ["ING-000002"]
    delivered = git(canary.root, "rev-parse", delivery_pin_ref(answer["manifest_ids"][0]))
    selected = [line for line in batch_lines(canary, runtime) if line.status == "selected"]
    assert len(selected) == 1
    assert selected[0].payload["integrated_revision"]["head_sha"] == delivered
    assert git(canary.root, "rev-parse", f"{delivered}^") == base
    assert git(canary.root, "show", f"{delivered}:src/module.py") == "x = 2"
    assert git(canary.root, "rev-parse", "main") == base
    assert_removed(canary, opened)


def test_a_batch_with_a_head_is_delivered_on_that_head(tmp_path: Path, opened: Opened) -> None:
    """Earlier deliveries survive: the new commit parents on the Batch head."""
    canary, runtime, ctx, base, candidate = sealed_canary(tmp_path)
    head = side_commit(canary, path="earlier.txt", content="earlier\n")
    seed_head(canary, runtime, base=base, head=head)

    answer = call(
        DELIVERY_INTEGRATE_METHOD, ctx, integrate_params(canary, base=base, candidate=candidate)
    )

    delivered = git(canary.root, "rev-parse", delivery_pin_ref(answer["manifest_ids"][0]))
    assert answer["delivered"] is True
    assert answer["generation_ids"] == ["ING-000003"]
    assert git(canary.root, "rev-parse", f"{delivered}^") == head
    assert git(canary.root, "show", f"{delivered}:earlier.txt") == "earlier"
    assert git(canary.root, "show", f"{delivered}:src/module.py") == "x = 2"
    assert_removed(canary, opened)


def test_a_conflicting_candidate_blocks_and_the_worktree_is_still_removed(
    tmp_path: Path, opened: Opened
) -> None:
    """An overlap with the Batch head files a conflict frame and moves no ref."""
    canary, runtime, ctx, base, candidate = sealed_canary(tmp_path)
    head = side_commit(canary, path="src/module.py", content="x = 9\n")
    seed_head(canary, runtime, base=base, head=head)
    refs_before = git(canary.root, "for-each-ref", "--format=%(refname) %(objectname)")

    answer = call(
        DELIVERY_INTEGRATE_METHOD, ctx, integrate_params(canary, base=base, candidate=candidate)
    )

    assert answer["delivered"] is False
    assert answer["blocked_on"] == candidate
    assert answer["conflict"]["files"][0]["path"] == "src/module.py"
    assert [line.status for line in batch_lines(canary, runtime)] == ["selected", "blocked"]
    assert git(canary.root, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before
    assert_removed(canary, opened)


def test_an_unpinned_candidate_is_refused_and_the_worktree_is_still_removed(
    tmp_path: Path, opened: Opened
) -> None:
    """A refusal raised mid-apply appends nothing and leaves no tree behind."""
    canary, runtime, ctx, base, candidate = sealed_canary(tmp_path)
    git(canary.root, "update-ref", "-d", candidate_pin_ref(candidate))

    with pytest.raises(DaemonValidationError, match="integration_candidate_unresolvable"):
        call(
            DELIVERY_INTEGRATE_METHOD,
            ctx,
            integrate_params(canary, base=base, candidate=candidate),
        )

    assert batch_lines(canary, runtime) == ()
    assert_removed(canary, opened)


def test_the_shipped_factory_is_the_git_workspace() -> None:
    """With nothing injected the verb integrates in a real git worktree."""
    assert delivery_methods.INTEGRATION_WORKSPACE_FACTORY is git_integration_workspace
