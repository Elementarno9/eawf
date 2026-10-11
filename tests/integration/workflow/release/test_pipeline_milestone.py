"""``eawf release pipeline --milestone`` checks a Milestone's Batches on main by trailer.

The options take exactly one of ``--phase`` and ``--milestone``. The repin
step of a Milestone release runs against a real repository: two Batches
land as five commits each carrying only its Task's ``Task:`` trailer, the
branch lands on ``refs/remotes/origin/main``, and both Batches close on
their last commit and compact out of the document into the Batch ledger,
as a Batch closed on landing does. The step passes there, and refuses by
name when main carries no commit for a Batch or carries other commits than
the head it pinned. It never reaches the epoch-1 wave-pin helpers.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.identity import IdentityError
from eawf.kernel.store.compaction import compact_terminal_record, document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.vcs import trailer_repin, wave_sha
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import release_pipeline as cli_module
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
    document_path,
    rekeyed,
    seed,
    seed_row,
)
from tests.integration.workflow.integration.test_publish import (
    Scratch,
    canary_runtime_under_tmp,
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

__all__ = ["canary_runtime_under_tmp", "scratch", "world"]

#: The Milestone the seeded Batches belong to.
MILESTONE = "MLS-0030"

#: A fixed committer date, so a rebuilt commit never collides with the original.
_REBUILT_ENV = {"GIT_COMMITTER_DATE": "2001-01-01T00:00:00+00:00"}


def _refuse_rpc(method: str, params: Any) -> dict[str, Any]:
    raise AssertionError(f"the repin step calls no daemon method, got {method}")


def _closed(repo: Scratch, key: str, *, head: str) -> dict[str, Any]:
    """Return Batch *key* COMPLETED on the pinned *head*."""
    row = rekeyed(seed_row("batch", "COMPLETED"), key=key)
    row["current_head_binding"]["head_sha"] = head
    row["current_head_binding"]["tree_sha"] = git(repo.root, "rev-parse", f"{head}^{{tree}}")
    return row


@pytest.fixture
def landed(scratch: Scratch) -> Scratch:
    """Two Batches landed on origin/main as Task-trailered commits, closed and compacted.

    BAT-0007 holds EAWF-0042's two commits and pins the second; BAT-0008 holds
    EAWF-0043's three and pins the branch tip.
    """
    git(scratch.root, "update-ref", "refs/remotes/origin/main", scratch.tip)
    path = document_path(scratch.canary)
    for key, head in (("BAT-0007", scratch.commits[1]), ("BAT-0008", scratch.tip)):
        row = _closed(scratch, key, head=head)
        seed(scratch.canary, {"batch": {key: row}})
        compact_terminal_record(
            path,
            record=LedgerRecord(
                collection=Epoch2Collection.BATCH,
                record_key=key,
                status="COMPLETED",
                recorded_at=NOW,
                payload=row,
            ),
        )
    return scratch


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


def test_the_completed_batches_have_left_the_document(landed: Scratch) -> None:
    held = document_rows(read_document(document_path(landed.canary)), Epoch2Collection.BATCH)
    assert not {"BAT-0007", "BAT-0008"} & set(held)


def test_repin_passes_when_each_batch_landed_as_its_pinned_commits(landed: Scratch) -> None:
    outcome = _repin(landed.root)
    assert outcome == {"milestone": MILESTONE, "batches": "2"}


def test_repin_refuses_a_batch_with_no_trailer_commit_on_main(landed: Scratch) -> None:
    git(landed.root, "update-ref", "refs/remotes/origin/main", landed.commits[1])
    refusal = _repin(landed.root)
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.BATCH_TRAILER_MISSING
    assert "BAT-0008" in refusal.detail
    assert "BAT-0007" not in refusal.detail


def test_repin_refuses_a_batch_whose_commit_landed_twice(landed: Scratch) -> None:
    again = _rebuild(landed.root, landed.commits[1], parent=landed.tip)
    git(landed.root, "update-ref", "refs/remotes/origin/main", again)
    refusal = _repin(landed.root)
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.BATCH_HEAD_MISMATCH
    assert refusal.detail.endswith(f"BAT-0007 bound {landed.commits[1][:12]}, landed {again[:12]}")


def test_repin_refuses_batches_whose_commits_were_rewritten(landed: Scratch) -> None:
    parent = landed.base
    rebuilt: list[str] = []
    for sha in landed.commits:
        parent = _rebuild(landed.root, sha, parent=parent)
        rebuilt.append(parent)
    git(landed.root, "update-ref", "refs/remotes/origin/main", parent)
    refusal = _repin(landed.root)
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.BATCH_HEAD_MISMATCH
    assert f"BAT-0007 bound {landed.commits[1][:12]}, landed {rebuilt[1][:12]}" in refusal.detail
    assert f"BAT-0008 bound {landed.tip[:12]}, landed {rebuilt[4][:12]}" in refusal.detail


def test_repin_refuses_a_milestone_with_no_completed_batch(landed: Scratch) -> None:
    refusal = _repin(landed.root, milestone="MLS-0031")
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.MILESTONE_BATCHES_MISSING


def test_repin_skips_a_batch_that_is_not_completed(landed: Scratch) -> None:
    # The document's live row wins over the row the ledger compacted.
    seed(
        landed.canary, {"batch": {"BAT-0008": rekeyed(seed_row("batch", "ACTIVE"), key="BAT-0008")}}
    )
    git(landed.root, "update-ref", "refs/remotes/origin/main", landed.commits[1])
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


def test_carriers_read_task_and_batch_trailers_from_the_whole_message(
    scratch: Scratch,
) -> None:
    root = scratch.root
    git(root, "commit", "-q", "--allow-empty", "-m", "feat: a\n\nBatch: BAT-0001")
    first = git(root, "rev-parse", "HEAD")
    git(root, "commit", "-q", "--allow-empty", "-m", "fix: b\n\nBatch: BAT-0002\n\nTask: EAWF-0042")
    second = git(root, "rev-parse", "HEAD")
    git(root, "commit", "-q", "--allow-empty", "-m", "fix: says Batch: BAT-0009 inline")
    git(root, "commit", "-q", "--allow-empty", "-m", "chore: c\n\nTask: EAWF-9999")
    host = GitHubReleaseHost(root, state_path=root / ".ea" / "state.json")
    commits = scratch.commits
    assert host.batch_carrier_commits("HEAD") == {
        "BAT-0001": (first,),
        "BAT-0002": (second,),
        "BAT-0007": (second, commits[1], commits[0]),
        "BAT-0008": (commits[4], commits[3], commits[2]),
    }


# ---- CR-3: the Milestone path never touches the epoch-1 helpers ----------------------


def test_milestone_repin_passes_with_the_epoch1_helpers_patched_to_raise(
    landed: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("the milestone path must not reach the epoch-1 helpers")

    for module in (pipeline_module, wave_sha, trailer_repin):
        for name in ("load_drift_acks", "resolve_trailer_repins"):
            monkeypatch.setattr(module, name, boom, raising=False)
    assert _repin(landed.root) == {"milestone": MILESTONE, "batches": "2"}
