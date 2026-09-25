"""P34-I01-W46: the semantic ``submit_candidate`` handler pins its commit too.

W40 taught ``runtime.candidate.submit`` to pin the leased worktree's
commit under the candidate's ref before writing its claim, because the
lease's branch is deleted once the Run is reconciled and integration must
still be able to find the work afterwards. The semantic gateway's
``submit_candidate`` handler wrote the same claim without ever pinning
it, so a candidate a worker filed through MCP could never be integrated:
``runtime.delivery.integrate`` reads the pin, not the claim, and refused
every one of them as unresolvable (``candidate_unresolvable``). This
suite drives a submission through the semantic gateway end to end -- pin,
seal and deliver -- against a real leased git worktree, and proves a
submission the pin check will not hold is refused the same way the
native verb refuses it: inertly, with nothing pinned and nothing
recorded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import eawf.runtime.worktree.git as git
from eawf.kernel.runtime.candidate import CandidateRefusal
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.candidate import CANDIDATE_REPORT_BIND_METHOD
from eawf.runtime.daemon.methods.delivery import DELIVERY_INTEGRATE_METHOD
from eawf.runtime.integration.git_workspace import COMMIT_ARTIFACT_PREFIX, candidate_pin_ref
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    root_context,
)
from tests.integration.runtime.daemon.methods.test_delivery_candidate_sealing import (
    SUCCESS,
    report_params,
)
from tests.integration.runtime.daemon.methods.test_delivery_integrate_workspace import (
    integrate_params,
)
from tests.integration.runtime.daemon.test_native_dispatch import call_verb
from tests.integration.runtime.daemon.test_semantic_submit_candidate import (
    bind,
    candidate_payload,
    commit_in_seeded_lease,
    executor_capsule,
    invoke,
    make_canary,
    receipt_lines,
    seal_call,
    seed_lease,
    submission_lines,
)
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration


def pinned_refs(canary: CanaryProvision) -> list[str]:
    """Return every candidate ref the canary's repository holds."""
    listed = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/eawf/candidates/"],
        cwd=canary.root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in listed.splitlines() if line]


def submitted_over_mcp(
    tmp_path: Path,
) -> tuple[CanaryProvision, Path, MethodContext, str, str, str]:
    """Seed a leased worktree and submit its commit through the semantic gateway.

    Returns:
        The canary, its runtime directory, the daemon context, the
        lease's base commit, the submitted commit's artifact ref and the
        answered candidate ref.
    """
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    ctx = method_context(runtime)
    capsule = executor_capsule()
    bind(ctx, canary, capsule)
    lease = seed_lease(canary, runtime)
    paths = tuple(SUCCESS["submission"]["changed_paths"])
    ref = commit_in_seeded_lease(canary, runtime, paths=paths)
    payload = candidate_payload(
        submission_ref=ref,
        changed_paths=tuple(SUCCESS["submission"]["changed_paths"]),
        resulting_tree_digest=SUCCESS["submission"]["resulting_tree_digest"],
    )
    answer = invoke(ctx, canary, seal_call(capsule=capsule, payload=payload), capsule)
    candidate_ref = answer["result"]["bounded_output"]["candidate_ref"]
    return canary, runtime, ctx, lease.base_commit, ref, candidate_ref


# ---------------------------------------------------------------------------
# CR-01: the handler pins, seals and delivers exactly as the native verb does
# ---------------------------------------------------------------------------


def test_a_semantic_submission_pins_the_leased_commit(tmp_path: Path) -> None:
    """The handler pins the submitted commit under the candidate's own ref."""
    canary, _runtime, _ctx, _base, ref, candidate = submitted_over_mcp(tmp_path)

    pinned = git.commit_sha(canary.root, candidate_pin_ref(candidate))

    assert f"{COMMIT_ARTIFACT_PREFIX}{pinned}" == ref


def test_the_pinned_candidate_seals_and_is_delivered(tmp_path: Path) -> None:
    """A candidate submitted over MCP reaches integration like any other."""
    canary, runtime, ctx, base, _ref, candidate = submitted_over_mcp(tmp_path)
    world.seed_task(root_context(canary, runtime), world.task_row())
    sealed = call_verb(
        CANDIDATE_REPORT_BIND_METHOD,
        ctx,
        report_params(canary, claim=SUCCESS["submission"], report=SUCCESS["report"]),
    )
    assert sealed["sealed"] is True
    assert sealed["candidate_ref"] == candidate

    answer = call_verb(
        DELIVERY_INTEGRATE_METHOD, ctx, integrate_params(canary, base=base, candidate=candidate)
    )

    assert answer["delivered"] is True


# ---------------------------------------------------------------------------
# Gate-fire proof: a submission the pin check will not hold is refused inert
# ---------------------------------------------------------------------------


def test_a_submission_naming_no_commit_is_refused_inert(tmp_path: Path) -> None:
    """A reference that names no commit is pinned nowhere and recorded nowhere."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    ctx = method_context(runtime)
    capsule = executor_capsule()
    bind(ctx, canary, capsule)
    seed_lease(canary, runtime)
    commit_in_seeded_lease(canary, runtime)
    before_receipts = receipt_lines(canary, runtime)

    with pytest.raises(DaemonValidationError, match=CandidateRefusal.SUBMISSION_NOT_COMMIT.value):
        invoke(
            ctx,
            canary,
            seal_call(
                capsule=capsule,
                payload=candidate_payload(submission_ref="artifact://candidate/not-a-commit"),
            ),
            capsule,
        )

    assert submission_lines(canary, runtime) == []
    assert pinned_refs(canary) == []
    assert receipt_lines(canary, runtime) == before_receipts


def test_a_submission_naming_a_commit_that_is_not_the_lease_head_is_refused_inert(
    tmp_path: Path,
) -> None:
    """A real commit that is not the leased worktree's current HEAD is not this claim's work."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    ctx = method_context(runtime)
    capsule = executor_capsule()
    bind(ctx, canary, capsule)
    seed_lease(canary, runtime)
    stale = commit_in_seeded_lease(canary, runtime)
    commit_in_seeded_lease(canary, runtime, content="x = 3\n")
    before_receipts = receipt_lines(canary, runtime)

    with pytest.raises(DaemonValidationError, match=CandidateRefusal.SUBMISSION_NOT_HEAD.value):
        invoke(
            ctx,
            canary,
            seal_call(capsule=capsule, payload=candidate_payload(submission_ref=stale)),
            capsule,
        )

    assert submission_lines(canary, runtime) == []
    assert pinned_refs(canary) == []
    assert receipt_lines(canary, runtime) == before_receipts


def test_a_submission_naming_the_untouched_base_is_refused_inert(tmp_path: Path) -> None:
    """A lease that committed nothing has no work on its base to propose."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    ctx = method_context(runtime)
    capsule = executor_capsule()
    bind(ctx, canary, capsule)
    lease = seed_lease(canary, runtime)

    with pytest.raises(DaemonValidationError, match=CandidateRefusal.SUBMISSION_OFF_BASE.value):
        invoke(
            ctx,
            canary,
            seal_call(
                capsule=capsule,
                payload=candidate_payload(
                    submission_ref=f"{COMMIT_ARTIFACT_PREFIX}{lease.base_commit}"
                ),
            ),
            capsule,
        )

    assert submission_lines(canary, runtime) == []
    assert pinned_refs(canary) == []
