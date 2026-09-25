"""The git integration workspace delivers what git says, and blocks cleanly.

Every case runs in a scratch repository under ``tmp_path``: a base commit,
candidate commits made on side branches and pinned the way a submission
pins them, and a detached integration worktree the workspace adds and
removes. What the workspace reports is compared against git itself, so a
revision whose head or tree git would not recognise fails here rather
than downstream.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from eawf.kernel.delivery.integration import IntegrationGenerationLedger
from eawf.kernel.delivery.receipts import RevisionBinding, RevisionRefKind
from eawf.kernel.runtime.candidate import CandidateBundle, SealCheck, candidate_identity
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.runtime.integration.apply import (
    ApplyDisposition,
    IntegrationRefusal,
    IntegrationRefusedError,
    integration_order,
)
from eawf.runtime.integration.git_workspace import (
    COMMIT_ARTIFACT_PREFIX,
    GitIntegrationWorkspace,
    candidate_pin_ref,
    delivery_pin_ref,
)
from eawf.runtime.integration.recovery import (
    IntegrationOutcomeKind,
    IntegrationPlan,
    integrate_batch,
    plan_deliveries,
)

pytestmark = pytest.mark.integration


CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
REPOSITORY: Final = f"{CONTAINER}/repository/REP-EAWF"
BATCH: Final = f"{CONTAINER}/batch/BAT-0001"
RUN: Final = f"{CONTAINER}/run/RUN-00000010"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
DIGEST: Final = f"sha256:{'c' * 64}"
LINES: Final = "one\ntwo\nthree\nfour\nfive\n"


def git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A one-commit repository holding ``notes.txt`` on ``main``."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "ci@example.com")
    git(root, "config", "user.name", "ci")
    (root / "notes.txt").write_text(LINES, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    return root


def commit_on(repo: Path, *, branch: str, start: str, files: dict[str, str | None]) -> str:
    """Commit *files* on a new *branch* from *start* and return the commit.

    A ``None`` content deletes the file. ``main`` is checked out again
    afterwards, so the caller's working tree is where it started.
    """
    git(repo, "checkout", "-q", "-b", branch, start)
    for path, content in files.items():
        if content is None:
            git(repo, "rm", "-q", path)
            continue
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(content, encoding="utf-8")
        git(repo, "add", path)
    git(repo, "commit", "-q", "-m", f"work on {branch}")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


def bundle(
    repo: Path, *, sha: str, base: str, task: str = "EAWF-0001", pin: bool = True
) -> CandidateBundle:
    """Return a sealed candidate naming commit *sha*, pinned unless *pin* is off."""
    urn = f"{CONTAINER}/task/{task}"
    resulting = f"sha256:{sha[:2] * 32}"
    sealed = CandidateBundle.model_validate(
        {
            "candidate_ref": candidate_identity(task_ref=urn, resulting_tree_digest=resulting),
            "run_ref": RUN,
            "task_ref": urn,
            "submission_ref": f"{COMMIT_ARTIFACT_PREFIX}{sha}",
            "report_digest": f"sha256:{'b' * 64}",
            "verdict": AgentReportVerdict.PASS,
            "changed_paths": ("notes.txt",),
            "resulting_tree_digest": resulting,
            "base_commit": base,
            "workspace_generation": 1,
            "checks_passed": tuple(SealCheck),
            "sealed_at": AT,
        }
    )
    if pin:
        git(repo, "update-ref", candidate_pin_ref(sealed.candidate_ref), sha)
    return sealed


def binding(*, head: str, generation: int = 1) -> RevisionBinding:
    """Return the Batch revision at *generation*, bound to commit *head*."""
    return RevisionBinding(
        repository_ref=REPOSITORY,
        ref_kind=RevisionRefKind.INTEGRATION,
        head_sha=head,
        tree_sha="3c" * 20,
        parent_sha=None,
        batch_ref=BATCH,
        integration_generation=generation,
        manifest_digest=DIGEST,
        criteria_digest=DIGEST,
        policy_digest=DIGEST,
        environment_digest=None,
        bound_at=AT,
    )


def plan_for(
    *bundles: CandidateBundle, source: str, target: RevisionBinding | None = None
) -> IntegrationPlan:
    """Return the one batch-unit delivery of *bundles*."""
    ordered = integration_order(bundles)
    return plan_deliveries(
        ordered,
        repository_ref=REPOSITORY,
        batch_ref=BATCH,
        source_base=binding(head=source),
        target_base=target if target is not None else binding(head=source),
        parent_generation_id=None if target is None else f"ING-{target.integration_generation:06d}",
        subjects={item.candidate_ref: f"deliver {item.task_ref.entity_key}" for item in ordered},
        batch_subject="deliver the batch",
        unit="batch",
        task_reference="trailer",
    )[0]


def workspace(repo: Path, tmp_path: Path) -> GitIntegrationWorkspace:
    """Return an integration workspace over *repo*, not yet materialized."""
    return GitIntegrationWorkspace(
        repo_root=repo, directory=tmp_path / "integration" / "int-0001", batch_ref=BATCH
    )


def refs(repo: Path) -> str:
    """Return every ref the repository holds, with the commit it names."""
    return git(repo, "for-each-ref", "--format=%(refname) %(objectname)")


def worktrees(repo: Path) -> list[str]:
    """Return the path of every worktree the repository has registered."""
    listed = git(repo, "worktree", "list", "--porcelain")
    return [line.split(" ", 1)[1] for line in listed.splitlines() if line.startswith("worktree ")]


class MemoryLock:
    """A state lock that is always free to take."""

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Hold nothing; a single-threaded test has nobody to exclude."""
        yield


class MemoryStore:
    """A generation store kept in memory."""

    def __init__(self, ledger: IntegrationGenerationLedger) -> None:
        """Start from *ledger*."""
        self.ledger = ledger

    def read(self) -> IntegrationGenerationLedger:
        """Return the history as it stands."""
        return self.ledger

    def write(self, ledger: IntegrationGenerationLedger) -> None:
        """Keep *ledger* as the history."""
        self.ledger = ledger


# ---- a clean candidate is delivered as git sees it --------------------------


def test_a_clean_candidate_is_delivered_with_the_head_and_tree_git_holds(
    repo: Path, tmp_path: Path
) -> None:
    """The revision names the commit and tree git pinned, parented on the base."""
    base = git(repo, "rev-parse", "HEAD")
    sha = commit_on(repo, branch="c1", start=base, files={"notes.txt": LINES.replace("one", "1")})
    candidate = bundle(repo, sha=sha, base=base)
    plan = plan_for(candidate, source=base)

    with workspace(repo, tmp_path) as integration:
        assert integration.materialize(base_commit=base) == base
        applied = integration.apply(candidate)
        revision = integration.commit(plan.delivery)

    assert applied.disposition is ApplyDisposition.APPLIED
    assert revision.head_sha == git(repo, "rev-parse", delivery_pin_ref(plan.manifest.manifest_id))
    assert revision.tree_sha == git(repo, "rev-parse", f"{revision.head_sha}^{{tree}}")
    assert revision.tree_sha == git(repo, "rev-parse", f"{sha}^{{tree}}")
    assert revision.parent_sha == base == git(repo, "rev-parse", f"{revision.head_sha}^")
    assert git(repo, "show", f"{revision.head_sha}:notes.txt").startswith("1\n")
    assert git(repo, "log", "-1", "--format=%B", revision.head_sha) == plan.delivery.message.strip()
    assert revision.diff_digest.startswith("sha256:")


def test_delivery_moves_no_branch(repo: Path, tmp_path: Path) -> None:
    """Only the delivery pin is written; every branch stays where it was."""
    base = git(repo, "rev-parse", "HEAD")
    sha = commit_on(repo, branch="c1", start=base, files={"added.txt": "new\n"})
    candidate = bundle(repo, sha=sha, base=base)
    before = refs(repo)

    with workspace(repo, tmp_path) as integration:
        integration.materialize(base_commit=base)
        integration.apply(candidate)
        integration.commit(plan_for(candidate, source=base).delivery)

    added = set(refs(repo).splitlines()) - set(before.splitlines())
    assert [line.split(" ")[0] for line in added] == [
        delivery_pin_ref(plan_for(candidate, source=base).manifest.manifest_id)
    ]
    assert set(before.splitlines()) <= set(refs(repo).splitlines())


def test_two_candidates_land_as_one_commit_on_the_base(repo: Path, tmp_path: Path) -> None:
    """The scratch steps are not reachable: the delivery is one commit on the base."""
    base = git(repo, "rev-parse", "HEAD")
    first = commit_on(repo, branch="c1", start=base, files={"a.txt": "a\n"})
    second = commit_on(repo, branch="c2", start=base, files={"b.txt": "b\n"})
    candidates = (
        bundle(repo, sha=first, base=base, task="EAWF-0001"),
        bundle(repo, sha=second, base=base, task="EAWF-0002"),
    )
    plan = plan_for(*candidates, source=base)

    with workspace(repo, tmp_path) as integration:
        integration.materialize(base_commit=base)
        dispositions = [integration.apply(item).disposition for item in plan.ordered]
        revision = integration.commit(plan.delivery)

    assert dispositions == [ApplyDisposition.APPLIED, ApplyDisposition.APPLIED]
    assert git(repo, "rev-list", "--count", f"{base}..{revision.head_sha}") == "1"
    assert git(repo, "ls-tree", "--name-only", revision.head_sha).splitlines() == [
        "a.txt",
        "b.txt",
        "notes.txt",
    ]


def test_a_second_materialize_moves_the_same_worktree(repo: Path, tmp_path: Path) -> None:
    """A per-Task delivery unit re-materializes rather than adding a worktree."""
    base = git(repo, "rev-parse", "HEAD")
    later = commit_on(repo, branch="later", start=base, files={"later.txt": "l\n"})

    with workspace(repo, tmp_path) as integration:
        integration.materialize(base_commit=base)
        assert integration.materialize(base_commit=later) == later
        assert len(worktrees(repo)) == 2


def test_commit_before_materialize_is_refused(repo: Path, tmp_path: Path) -> None:
    """There is no base to author a delivery on before one is materialized."""
    base = git(repo, "rev-parse", "HEAD")
    sha = commit_on(repo, branch="c1", start=base, files={"a.txt": "a\n"})
    plan = plan_for(bundle(repo, sha=sha, base=base), source=base)

    with (
        workspace(repo, tmp_path) as integration,
        pytest.raises(RuntimeError, match="materialized"),
    ):
        integration.commit(plan.delivery)


# ---- an overlapping candidate conflicts and moves nothing ------------------


def test_an_overlapping_candidate_is_conflicted_with_its_frame_and_no_ref_moved(
    repo: Path, tmp_path: Path
) -> None:
    """Both sides of the overlap are framed, and the refs are exactly as before."""
    base = git(repo, "rev-parse", "HEAD")
    head = commit_on(
        repo, branch="head", start=base, files={"notes.txt": LINES.replace("two", "ours")}
    )
    sha = commit_on(
        repo, branch="c1", start=base, files={"notes.txt": LINES.replace("two", "theirs")}
    )
    candidate = bundle(repo, sha=sha, base=base)
    before = refs(repo)

    with workspace(repo, tmp_path) as integration:
        integration.materialize(base_commit=head)
        applied = integration.apply(candidate)
        assert git(integration.directory, "status", "--porcelain") == ""
        assert git(integration.directory, "rev-parse", "HEAD") == head

    assert applied.disposition is ApplyDisposition.CONFLICTED
    assert [item.path for item in applied.conflict_files] == ["notes.txt"]
    hunk = applied.conflict_files[0].hunks[0]
    assert (hunk.ours.lines, hunk.theirs.lines) == (("ours",), ("theirs",))
    assert (hunk.ours.sha, hunk.theirs.sha) == (head, sha)
    assert (applied.ahead, applied.behind) == (1, 1)
    assert refs(repo) == before


def test_a_delete_against_an_edit_is_framed_as_whole_files(repo: Path, tmp_path: Path) -> None:
    """A conflict with no markers still carries both sides."""
    base = git(repo, "rev-parse", "HEAD")
    head = commit_on(repo, branch="head", start=base, files={"notes.txt": None})
    sha = commit_on(repo, branch="c1", start=base, files={"notes.txt": LINES.replace("one", "1")})

    with workspace(repo, tmp_path) as integration:
        integration.materialize(base_commit=head)
        applied = integration.apply(bundle(repo, sha=sha, base=base))

    hunk = applied.conflict_files[0].hunks[0]
    assert applied.disposition is ApplyDisposition.CONFLICTED
    assert hunk.ours.lines == ()
    assert hunk.theirs.lines[0] == "1"


@pytest.mark.parametrize("delivered", [True, False], ids=["delivered", "blocked"])
def test_the_worktree_is_removed_on_exit_delivered_or_blocked(
    repo: Path, tmp_path: Path, delivered: bool
) -> None:
    """Closing the workspace leaves only the main worktree registered."""
    base = git(repo, "rev-parse", "HEAD")
    head = commit_on(repo, branch="head", start=base, files={"notes.txt": "ours\n"})
    sha = commit_on(repo, branch="c1", start=base, files={"notes.txt": "theirs\n"})
    candidate = bundle(repo, sha=sha, base=base)
    integration = workspace(repo, tmp_path)

    with integration:
        integration.materialize(base_commit=base if delivered else head)
        applied = integration.apply(candidate)
        if delivered:
            integration.commit(plan_for(candidate, source=base).delivery)

    assert (applied.disposition is ApplyDisposition.APPLIED) is delivered
    assert not integration.directory.exists()
    assert len(worktrees(repo)) == 1


def test_the_worktree_is_removed_when_the_block_raises(repo: Path, tmp_path: Path) -> None:
    """An exception inside the integration still removes the tree."""
    base = git(repo, "rev-parse", "HEAD")
    integration = workspace(repo, tmp_path)

    with pytest.raises(KeyError), integration:
        integration.materialize(base_commit=base)
        raise KeyError("boom")

    assert not integration.directory.exists()
    assert len(worktrees(repo)) == 1


# ---- a candidate the daemon did not pin cannot be applied ------------------


def test_an_unpinned_candidate_is_refused_as_unresolvable(repo: Path, tmp_path: Path) -> None:
    """A submission whose commit nobody pinned has no work the daemon holds."""
    base = git(repo, "rev-parse", "HEAD")
    sha = commit_on(repo, branch="c1", start=base, files={"a.txt": "a\n"})

    with workspace(repo, tmp_path) as integration:
        integration.materialize(base_commit=base)
        with pytest.raises(IntegrationRefusedError) as caught:
            integration.apply(bundle(repo, sha=sha, base=base, pin=False))

    assert caught.value.code is IntegrationRefusal.CANDIDATE_UNRESOLVABLE


def test_a_pin_naming_another_commit_is_refused_as_unresolvable(repo: Path, tmp_path: Path) -> None:
    """The pin must hold exactly the commit the submission names."""
    base = git(repo, "rev-parse", "HEAD")
    sha = commit_on(repo, branch="c1", start=base, files={"a.txt": "a\n"})
    candidate = bundle(repo, sha=sha, base=base)
    git(repo, "update-ref", candidate_pin_ref(candidate.candidate_ref), base)

    with workspace(repo, tmp_path) as integration:
        integration.materialize(base_commit=base)
        with pytest.raises(IntegrationRefusedError) as caught:
            integration.apply(candidate)

    assert caught.value.code is IntegrationRefusal.CANDIDATE_UNRESOLVABLE


def test_a_submission_naming_no_commit_is_refused_as_unresolvable(
    repo: Path, tmp_path: Path
) -> None:
    """A bundle sealed before submissions named commits cannot be integrated."""
    base = git(repo, "rev-parse", "HEAD")
    sha = commit_on(repo, branch="c1", start=base, files={"a.txt": "a\n"})
    legacy = bundle(repo, sha=sha, base=base).model_copy(
        update={"submission_ref": "artifact://candidate/executor-success"}
    )

    with workspace(repo, tmp_path) as integration:
        integration.materialize(base_commit=base)
        with pytest.raises(IntegrationRefusedError, match="unresolvable"):
            integration.apply(legacy)


# ---- integrate_batch applies over the Batch head, not the source base ------


def test_integrate_batch_delivers_over_the_batch_head(repo: Path, tmp_path: Path) -> None:
    """A Batch that already delivered keeps that work under the next delivery."""
    base = git(repo, "rev-parse", "HEAD")
    head = commit_on(repo, branch="head", start=base, files={"earlier.txt": "e\n"})
    sha = commit_on(repo, branch="c1", start=base, files={"notes.txt": LINES.replace("one", "1")})
    candidate = bundle(repo, sha=sha, base=base)
    plan = plan_for(candidate, source=base, target=binding(head=head, generation=2))
    store = MemoryStore(IntegrationGenerationLedger(batch_ref=BATCH))

    with workspace(repo, tmp_path) as integration:
        outcome = integrate_batch(
            plan.model_copy(update={"parent_generation_id": None}),
            workspace=integration,
            lock=MemoryLock(),
            store=store,
            now=AT,
        )

    assert outcome.kind is IntegrationOutcomeKind.DELIVERED
    assert outcome.revision is not None
    assert outcome.revision.parent_sha == head
    delivered = outcome.revision.head_sha
    assert git(repo, "ls-tree", "--name-only", delivered).splitlines() == [
        "earlier.txt",
        "notes.txt",
    ]
