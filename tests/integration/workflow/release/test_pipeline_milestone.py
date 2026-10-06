"""``eawf release pipeline --milestone`` checks a Milestone's Batches on main by trailer.

The options take exactly one of ``--phase`` and ``--milestone``. The repin
step of a Milestone release runs against a real repository: two Batches are
published by ``eawf publish``, which binds each to its squashed commit, the
published branch lands on ``refs/remotes/origin/main``, and both Batches are
marked COMPLETED. The step passes there, and refuses by name when main
carries no commit for a Batch, carries two, or carries one other than the
commit publish bound. It never reaches the epoch-1 wave-pin helpers.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.identity import IdentityError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import release_pipeline as cli_module
from eawf.workflow.lifecycle import wave_sha, wave_trailer_repin
from eawf.workflow.release import pipeline as pipeline_module
from eawf.workflow.release import pipeline_host as host_module
from eawf.workflow.release.pipeline import (
    PIPELINE_STEPS,
    PipelineJournalRow,
    PipelineLayout,
    PipelineOptions,
    PipelineRefusal,
    PipelineRefusalCode,
    PipelineStep,
    read_journal,
    run_post_merge_pipeline,
)
from eawf.workflow.release.pipeline_host import GitHubReleaseHost
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    rekeyed,
    seed,
    seed_row,
)
from tests.integration.workflow.integration.test_publish import (
    Scratch,
    canary_runtime_under_tmp,
    cli,
    eawf_publish,
    git,
    scratch,
)
from tests.integration.workflow.release.test_post_merge_pipeline import (
    NOW,
    VERSION,
    World,
    world,
)

pytestmark = pytest.mark.integration

__all__ = ["canary_runtime_under_tmp", "cli", "scratch", "world"]

#: The Milestone the seeded Batches belong to.
MILESTONE = "MLS-0030"

#: A fixed committer date, so a rebuilt commit never collides with the original.
_REBUILT_ENV = {"GIT_COMMITTER_DATE": "2001-01-01T00:00:00+00:00"}


def _refuse_rpc(method: str, params: Any) -> dict[str, Any]:
    raise AssertionError(f"the repin step calls no daemon method, got {method}")


@pytest.fixture
def landed(scratch: Scratch, cli: None) -> Scratch:
    """Two Batches published, landed on origin/main by fast-forward and COMPLETED."""
    code, answer = eawf_publish(scratch.root)
    assert code == 0, answer
    git(scratch.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    seed(
        scratch.canary,
        {
            "batch": {
                "BAT-0007": seed_row("batch", "COMPLETED"),
                "BAT-0008": rekeyed(seed_row("batch", "COMPLETED"), key="BAT-0008"),
            }
        },
    )
    return scratch


def _batch_commits(repo: Scratch) -> dict[str, str]:
    """Return Batch key -> the commit publish built for it."""
    commits: dict[str, str] = {}
    for sha in git(repo.root, "rev-list", f"{repo.base}..HEAD").splitlines():
        for line in git(repo.root, "show", "-s", "--format=%B", sha).splitlines():
            if line.startswith("Batch: "):
                commits[line.removeprefix("Batch: ").strip()] = sha
    return commits


def _rebuild(repo: Path, sha: str, *, parent: str) -> str:
    """Return a copy of *sha* on *parent* with another committer date, so another id."""
    message = git(repo, "show", "-s", "--format=%B", sha)
    return subprocess.run(
        ["git", "commit-tree", f"{sha}^{{tree}}", "-p", parent, "-F", "-"],
        cwd=repo,
        input=message,
        env={**os.environ, **_REBUILT_ENV},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repin(repo: Path, *, milestone: str = MILESTONE) -> PipelineRefusal | dict[str, str]:
    """Run the pipeline with every step but the repin journaled; return its outcome."""
    head = git(repo, "rev-parse", "HEAD")
    layout = PipelineLayout(repo, VERSION)
    layout.journal.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        PipelineJournalRow(
            version=VERSION,
            step=step,
            completed_at=NOW,
            facts=(
                {"source_sha": head, "milestone": milestone, "evidence_dir": "evidence"}
                if step is PipelineStep.PIN_SOURCE
                else {}
            ),
        )
        for step in PIPELINE_STEPS
        if step is not PipelineStep.REPIN
    ]
    layout.journal.write_text("".join(row.model_dump_json() + "\n" for row in rows))
    host = GitHubReleaseHost(repo, state_path=repo / ".ea" / "state.json")
    options = PipelineOptions(version=VERSION, milestone_key=milestone, publish=True)
    try:
        result = run_post_merge_pipeline(options, host=host, rpc=_refuse_rpc, now=lambda: NOW)
    except PipelineRefusal as refusal:
        assert refusal.step is PipelineStep.REPIN
        assert PipelineStep.REPIN not in [row.step for row in read_journal(layout.journal)]
        return refusal
    ran = [outcome for outcome in result.outcomes if outcome.disposition == "ran"]
    assert [outcome.step for outcome in ran] == [PipelineStep.REPIN]
    return dict(ran[0].facts)


# ---- CR-1: exactly one of --phase and --milestone ------------------------------------


def test_options_refuse_both_a_phase_and_a_milestone() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        PipelineOptions(version=VERSION, phase_id="P35", milestone_key=MILESTONE, publish=True)


def test_options_refuse_neither_a_phase_nor_a_milestone() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        PipelineOptions(version=VERSION, publish=True)


@pytest.mark.parametrize("key", ["", "MLS-1", "mls-0030", "BAT-0007", "MLS-0030 "])
def test_options_refuse_a_key_that_is_not_a_milestone_key(key: str) -> None:
    with pytest.raises(IdentityError):
        PipelineOptions(version=VERSION, milestone_key=key, publish=True)


def test_options_accept_a_milestone_alone() -> None:
    options = PipelineOptions(version=VERSION, milestone_key=MILESTONE, publish=True)
    assert (options.milestone_key, options.phase_id) == (MILESTONE, None)


@pytest.mark.parametrize(
    "argv",
    [
        ["--phase", "P35", "--milestone", MILESTONE],
        [],
    ],
    ids=["both", "neither"],
)
def test_cli_options_refuse_both_or_neither(
    world: World, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.chdir(world.root)
    monkeypatch.setattr(host_module, "GitHubReleaseHost", lambda *_a, **_k: world.host)
    monkeypatch.setattr(cli_module, "_daemon_rpc", world.daemon)
    result = CliRunner().invoke(app, ["release", "pipeline", VERSION, *argv])
    assert result.exit_code != 0
    assert "exactly one" in result.output
    assert world.calls == []


def test_cli_options_take_a_milestone_and_pin_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(world.root)
    monkeypatch.setattr(host_module, "GitHubReleaseHost", lambda *_a, **_k: world.host)
    monkeypatch.setattr(cli_module, "_daemon_rpc", world.daemon)
    result = CliRunner().invoke(app, ["release", "pipeline", VERSION, "--milestone", MILESTONE])
    # Without --publish the walk stops before the tag push, past the source pin.
    assert result.exit_code == 3, result.output
    assert "publish_flag_missing" in result.output
    pinned = read_journal(PipelineLayout(world.root, VERSION).journal)[0]
    assert pinned.step is PipelineStep.PIN_SOURCE
    assert pinned.facts["milestone"] == MILESTONE
    assert "phase" not in pinned.facts


# ---- CR-2: the repin step on a Milestone ---------------------------------------------


def test_repin_passes_when_each_batch_landed_as_its_bound_commit(landed: Scratch) -> None:
    outcome = _repin(landed.root)
    assert outcome == {"milestone": MILESTONE, "batches": "2"}


def test_repin_refuses_a_batch_with_no_trailer_commit_on_main(landed: Scratch) -> None:
    commits = _batch_commits(landed)
    git(landed.root, "update-ref", "refs/remotes/origin/main", commits["BAT-0007"])
    refusal = _repin(landed.root)
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.BATCH_TRAILER_MISSING
    assert "BAT-0008" in refusal.detail
    assert "BAT-0007" not in refusal.detail


def test_repin_refuses_a_batch_with_two_trailer_commits_on_main(landed: Scratch) -> None:
    commits = _batch_commits(landed)
    head = git(landed.root, "rev-parse", "refs/remotes/origin/main")
    again = _rebuild(landed.root, commits["BAT-0007"], parent=head)
    git(landed.root, "update-ref", "refs/remotes/origin/main", again)
    refusal = _repin(landed.root)
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.BATCH_TRAILER_DUPLICATED
    assert "BAT-0007=2" in refusal.detail


def test_repin_refuses_a_batch_whose_bound_head_differs(landed: Scratch) -> None:
    commits = _batch_commits(landed)
    first = _rebuild(landed.root, commits["BAT-0007"], parent=landed.base)
    second = _rebuild(landed.root, commits["BAT-0008"], parent=first)
    git(landed.root, "update-ref", "refs/remotes/origin/main", second)
    refusal = _repin(landed.root)
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.BATCH_HEAD_MISMATCH
    assert f"BAT-0007 bound {commits['BAT-0007'][:12]}, landed {first[:12]}" in refusal.detail


def test_repin_refuses_a_milestone_with_no_completed_batch(landed: Scratch) -> None:
    refusal = _repin(landed.root, milestone="MLS-0031")
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.MILESTONE_BATCHES_MISSING


def test_repin_skips_a_batch_that_is_not_completed(landed: Scratch) -> None:
    seed(
        landed.canary, {"batch": {"BAT-0008": rekeyed(seed_row("batch", "ACTIVE"), key="BAT-0008")}}
    )
    commits = _batch_commits(landed)
    git(landed.root, "update-ref", "refs/remotes/origin/main", commits["BAT-0007"])
    assert _repin(landed.root) == {"milestone": MILESTONE, "batches": "1"}


def test_repin_refuses_a_tree_that_is_not_native(tmp_path: Path) -> None:
    git_root = tmp_path / "plain"
    git_root.mkdir()
    git(git_root, "init", "-q", "-b", "main")
    git(
        git_root,
        "-c",
        "user.name=ci",
        "-c",
        "user.email=ci@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    refusal = _repin(git_root)
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.MILESTONE_UNREADABLE


def test_repin_reads_trailers_from_the_whole_message(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    identity = ["-c", "user.name=ci", "-c", "user.email=ci@example.com"]
    git(root, *identity, "commit", "-q", "--allow-empty", "-m", "chore: no trailer")
    git(root, *identity, "commit", "-q", "--allow-empty", "-m", "feat: a\n\nBatch: BAT-0001")
    first = git(root, "rev-parse", "HEAD")
    git(
        root,
        *identity,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "fix: b\n\nBatch: BAT-0002\n\nTask: EAWF-0001",
    )
    second = git(root, "rev-parse", "HEAD")
    git(root, *identity, "commit", "-q", "--allow-empty", "-m", "fix: says Batch: BAT-0009 inline")
    host = GitHubReleaseHost(root, state_path=root / ".ea" / "state.json")
    assert host.batch_trailer_commits("HEAD") == {"BAT-0001": (first,), "BAT-0002": (second,)}


# ---- CR-3: the Milestone path never touches the epoch-1 helpers ----------------------


def test_milestone_repin_passes_with_the_epoch1_helpers_patched_to_raise(
    landed: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("the milestone path must not reach the epoch-1 helpers")

    for module in (pipeline_module, wave_sha, wave_trailer_repin):
        for name in ("load_drift_acks", "resolve_trailer_repins"):
            monkeypatch.setattr(module, name, boom, raising=False)
    assert _repin(landed.root) == {"milestone": MILESTONE, "batches": "2"}
