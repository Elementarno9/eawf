"""A submission pins the leased worktree's commit, or it records nothing.

Every case runs against a canary whose Run was dispatched for real, so the
lease is one the daemon issued over a real worktree and the commit a
submission names is one a worker could actually have made there. A claim
naming that worktree's HEAD is pinned under ``refs/eawf/candidates/`` and
survives the lease branch being deleted; a claim naming anything else is
refused with a typed code, no pin and no ledger line.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import eawf.runtime.daemon.methods.candidate  # noqa: F401  -- registers runtime.candidate.*
from eawf.kernel.runtime.candidate import CandidateRefusal, candidate_identity
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.candidate import CANDIDATE_SUBMIT_METHOD
from eawf.runtime.integration.git_workspace import (
    COMMIT_ARTIFACT_PREFIX,
    candidate_pin_ref,
    submission_commit,
)
from tests.integration.runtime.daemon.methods.test_delivery_candidate_sealing import (
    SUCCESS,
    commit_in_lease,
    dispatched,
    leased_workspace,
    records_of,
    submissions_on,
    submit_params,
)
from tests.integration.runtime.daemon.test_native_dispatch import TASK_URN, call_verb, method_ctx

pytestmark = pytest.mark.integration


def git_out(repo: Path, *args: str) -> str:
    """Return the stripped stdout of one git command in *repo*."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def pinned(canary: CanaryProvision) -> list[str]:
    """Return every candidate ref the canary's repository holds."""
    listed = git_out(canary.root, "for-each-ref", "--format=%(refname)", "refs/eawf/candidates/")
    return [line for line in listed.splitlines() if line]


def candidate_ref() -> str:
    """Return the identity the fixture claim derives."""
    return candidate_identity(
        task_ref=TASK_URN,
        resulting_tree_digest=SUCCESS["submission"]["resulting_tree_digest"],
    )


def refused_inert(canary: CanaryProvision, runtime: Path, ref: str, code: CandidateRefusal) -> None:
    """Assert a submission naming *ref* is refused with *code* and leaves nothing."""
    before = records_of(canary, runtime)
    with pytest.raises(DaemonValidationError, match=code.value):
        call_verb(
            CANDIDATE_SUBMIT_METHOD,
            method_ctx(runtime),
            submit_params(canary, claim=SUCCESS["submission"], submission_ref=ref),
        )
    assert records_of(canary, runtime) == before
    assert submissions_on(canary, runtime) == ()
    assert pinned(canary) == []


def test_submission_of_the_lease_head_pins_that_commit(tmp_path: Path) -> None:
    """The commit a worker made in its lease is pinned under the candidate's name."""
    canary, runtime, ctx = dispatched(tmp_path)
    ref = commit_in_lease(canary, runtime)

    answer = call_verb(
        CANDIDATE_SUBMIT_METHOD,
        ctx,
        submit_params(canary, claim=SUCCESS["submission"], submission_ref=ref),
    )

    sha = submission_commit(ref)
    assert answer["candidate_ref"] == candidate_ref()
    assert git_out(canary.root, "rev-parse", candidate_pin_ref(candidate_ref())) == sha
    assert submissions_on(canary, runtime)[0].submission_ref == ref


def test_the_pin_keeps_the_commit_once_the_lease_branch_is_gone(tmp_path: Path) -> None:
    """Reconcile deletes the lease branch; the pinned work stays reachable."""
    canary, runtime, ctx = dispatched(tmp_path)
    ref = commit_in_lease(canary, runtime)
    call_verb(
        CANDIDATE_SUBMIT_METHOD,
        ctx,
        submit_params(canary, claim=SUCCESS["submission"], submission_ref=ref),
    )
    workspace = leased_workspace(canary, runtime)
    branch = git_out(workspace, "symbolic-ref", "--short", "HEAD")

    subprocess.run(["git", "worktree", "remove", "--force", str(workspace)], cwd=canary.root)
    subprocess.run(["git", "branch", "-D", branch], cwd=canary.root, check=True)
    subprocess.run(["git", "gc", "-q", "--prune=now"], cwd=canary.root, check=True)

    sha = submission_commit(ref)
    assert git_out(canary.root, "cat-file", "-t", f"{sha}") == "commit"
    assert git_out(canary.root, "rev-parse", candidate_pin_ref(candidate_ref())) == sha


def test_a_replayed_submission_keeps_its_pin(tmp_path: Path) -> None:
    """Presenting the same claim again replays and pins nothing new."""
    canary, runtime, ctx = dispatched(tmp_path)
    ref = commit_in_lease(canary, runtime)
    params = submit_params(canary, claim=SUCCESS["submission"], submission_ref=ref)
    call_verb(CANDIDATE_SUBMIT_METHOD, ctx, params)

    again = call_verb(CANDIDATE_SUBMIT_METHOD, ctx, params)

    assert again["replayed"] is True
    assert pinned(canary) == [candidate_pin_ref(candidate_ref())]
    assert len(submissions_on(canary, runtime)) == 1


def test_a_submission_naming_another_commit_is_refused_inert(tmp_path: Path) -> None:
    """A real commit that is not the lease's HEAD is somebody else's work."""
    canary, runtime, _ctx = dispatched(tmp_path)
    ref = commit_in_lease(canary, runtime)
    commit_in_lease(canary, runtime, content="x = 3\n")

    refused_inert(canary, runtime, ref, CandidateRefusal.SUBMISSION_NOT_HEAD)


def test_a_submission_naming_a_commit_the_repository_lacks_is_refused_inert(
    tmp_path: Path,
) -> None:
    """A well-formed name of no object is not the lease's HEAD."""
    canary, runtime, _ctx = dispatched(tmp_path)
    commit_in_lease(canary, runtime)

    refused_inert(
        canary, runtime, f"{COMMIT_ARTIFACT_PREFIX}{'e' * 40}", CandidateRefusal.SUBMISSION_NOT_HEAD
    )


def test_a_submission_naming_the_untouched_base_is_refused_inert(tmp_path: Path) -> None:
    """A lease that committed nothing has no work on its base to propose."""
    canary, runtime, _ctx = dispatched(tmp_path)
    base = git_out(leased_workspace(canary, runtime), "rev-parse", "HEAD")

    refused_inert(
        canary, runtime, f"{COMMIT_ARTIFACT_PREFIX}{base}", CandidateRefusal.SUBMISSION_OFF_BASE
    )


def test_a_head_that_does_not_descend_from_the_base_is_refused_inert(tmp_path: Path) -> None:
    """Work rebuilt on an unrelated root is not work on the leased base."""
    canary, runtime, _ctx = dispatched(tmp_path)
    workspace = leased_workspace(canary, runtime)
    subprocess.run(["git", "checkout", "-q", "--orphan", "stray"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "stray root"], cwd=workspace, check=True)
    head = git_out(workspace, "rev-parse", "HEAD")

    refused_inert(
        canary, runtime, f"{COMMIT_ARTIFACT_PREFIX}{head}", CandidateRefusal.SUBMISSION_OFF_BASE
    )


@pytest.mark.parametrize(
    "ref",
    [
        "artifact://candidate/executor-success",
        f"{COMMIT_ARTIFACT_PREFIX}{'a' * 39}",
        f"{COMMIT_ARTIFACT_PREFIX}{'a' * 41}",
        "artifact://git/tree/" + "a" * 40,
    ],
    ids=["not-a-commit", "short-name", "long-name", "tree-not-commit"],
)
def test_a_submission_naming_no_commit_is_refused_inert(tmp_path: Path, ref: str) -> None:
    """Only a full commit name can be pinned, so anything else records nothing."""
    canary, runtime, _ctx = dispatched(tmp_path)
    commit_in_lease(canary, runtime)

    refused_inert(canary, runtime, ref, CandidateRefusal.SUBMISSION_NOT_COMMIT)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (f"{COMMIT_ARTIFACT_PREFIX}{'0' * 40}", "0" * 40),
        (f"{COMMIT_ARTIFACT_PREFIX}{'A' * 40}", None),
        (f"{COMMIT_ARTIFACT_PREFIX}", None),
        ("artifact://candidate/x", None),
    ],
)
def test_submission_commit_reads_only_a_full_lowercase_commit_name(
    ref: str, expected: str | None
) -> None:
    """The parser admits one spelling of a commit and nothing near it."""
    assert submission_commit(ref) == expected
