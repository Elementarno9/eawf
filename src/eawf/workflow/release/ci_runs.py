"""Which CI runs prove a commit green before its release tag is pushed.

The pipeline's CI wait and its preflight refusal, and the hand-run
``release tag --push`` and ``release preflight``, all read the runs of
one commit through these, so none keeps its own idea of a commit CI
proved green. The module imports nothing from the release package, so
the pipeline and its forge adapter can both depend on it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from pydantic import ConfigDict, Field

from eawf.kernel.spec.common import _StrictModel

#: The workflow whose push run proves a commit green before it is tagged.
CI_WORKFLOW: Final[str] = "ci.yaml"


class CiRun(_StrictModel):
    """One run of the CI workflow, as ``gh run list --json`` reports it.

    Attributes:
        database_id: The forge's run id.
        event: The event that started the run, e.g. ``push``.
        head_sha: The commit the run checked out.
        status: ``completed`` once the run has finished.
        conclusion: ``success`` for a green run; empty until it finishes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    database_id: int = Field(alias="databaseId")
    event: str
    head_sha: str = Field(alias="headSha")
    status: str
    conclusion: str


def green_push_run(runs: Iterable[CiRun], sha: str) -> CiRun | None:
    """Return the first successful push-event run on exactly *sha*.

    Only a push run proves the commit itself: a pull-request run checks
    out a merge preview, and a scheduled or dispatched run is not the
    proof the merge to ``main`` earned.

    Args:
        runs: The CI runs the forge reports.
        sha: The pinned commit.

    Returns:
        The green push run, or ``None`` when no run proves *sha*.
    """
    for run in runs:
        if (run.event, run.head_sha, run.status, run.conclusion) == (
            "push",
            sha,
            "completed",
            "success",
        ):
            return run
    return None


def failed_push_runs(runs: Iterable[CiRun], sha: str) -> tuple[CiRun, ...]:
    """Return the push runs on *sha* once every one has finished without success.

    Args:
        runs: The CI runs the forge reports.
        sha: The pinned commit.

    Returns:
        Every push run on *sha* when there is at least one and none is
        green or still running; otherwise ``()``, since a run still in
        progress (a rerun of a failed one, say) may yet turn green.
    """
    pushes = tuple(run for run in runs if (run.event, run.head_sha) == ("push", sha))
    if any(run.status != "completed" or run.conclusion == "success" for run in pushes):
        return ()
    return pushes


def ci_green_refusal(runs: Iterable[CiRun], sha: str) -> str | None:
    """Return why *runs* do not prove *sha* green for a tag, or ``None`` when they do.

    Nothing waits here: a run still in flight leaves the commit unproven,
    and the operator tags once it finishes green.

    Args:
        runs: The CI runs the forge reports for *sha*.
        sha: The commit the tag would name.

    Returns:
        ``None`` when a successful push run is on *sha*; otherwise a
        sentence naming the failed push runs, or saying none succeeded.
    """
    held = tuple(runs)
    if green_push_run(held, sha) is not None:
        return None
    failed = ", ".join(f"{run.database_id} {run.conclusion}" for run in failed_push_runs(held, sha))
    if failed:
        return f"every {CI_WORKFLOW} push run of {sha} failed ({failed})"
    return f"commit {sha} has no successful {CI_WORKFLOW} push run"


__all__ = ["CI_WORKFLOW", "CiRun", "ci_green_refusal", "failed_push_runs", "green_push_run"]
