"""Whether a commit's CI runs let a hand-run release tag it."""

from __future__ import annotations

import pytest

from eawf.workflow.release.ci_runs import CiRun, ci_green_refusal

SHA = "a" * 40


def _run(run_id: int, *, event: str = "push", sha: str = SHA, conclusion: str = "success") -> CiRun:
    return CiRun.model_validate(
        {
            "databaseId": run_id,
            "event": event,
            "headSha": sha,
            "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion,
        }
    )


def test_ci_green_refusal_admits_a_green_push_run_on_the_commit() -> None:
    assert ci_green_refusal((_run(1, conclusion="failure"), _run(2)), SHA) is None


def test_ci_green_refusal_refuses_a_commit_with_no_runs() -> None:
    assert ci_green_refusal((), SHA) == (f"commit {SHA} has no successful ci.yaml push run")


@pytest.mark.parametrize(
    "run",
    [
        pytest.param(_run(3, event="pull_request"), id="pull-request"),
        pytest.param(_run(4, sha="b" * 40), id="other-commit"),
        pytest.param(_run(5, conclusion=""), id="in-progress"),
    ],
)
def test_ci_green_refusal_refuses_a_run_that_does_not_prove_the_commit(run: CiRun) -> None:
    refusal = ci_green_refusal((run,), SHA)

    assert refusal == f"commit {SHA} has no successful ci.yaml push run"


def test_ci_green_refusal_names_every_failed_push_run() -> None:
    runs = (_run(6, conclusion="failure"), _run(7, conclusion="cancelled"))

    refusal = ci_green_refusal(runs, SHA)

    assert refusal == f"every ci.yaml push run of {SHA} failed (6 failure, 7 cancelled)"


def test_ci_green_refusal_reads_an_iterable_once() -> None:
    assert ci_green_refusal(iter((_run(8, conclusion="failure"),)), SHA) == (
        f"every ci.yaml push run of {SHA} failed (8 failure)"
    )
