"""The Milestone repin step reads closed Batches from the ledger and their commits by Task.

A Batch closes on landing: it pins the phase-branch head, walks to COMPLETED and
compacts out of the document into the generation's Batch ledger, and its Tasks follow
into the Task ledger. Its work lands as one commit per Task carrying only a ``Task:``
trailer, or squashed into one commit naming the Batch and each of its Tasks. The host
therefore reads Batches and Tasks from both tiers, maps a
commit to its Batch through the Task's ``batch_ref``, and the step accepts a Batch of
several commits when main carries exactly the ones its pinned head carries.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import pytest

from eawf.kernel.store.compaction import compact_terminal_record, document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.workflow.release.pipeline import (
    PIPELINE_STEPS,
    PipelineHost,
    PipelineHostError,
    PipelineJournalRow,
    PipelineLayout,
    PipelineOptions,
    PipelineRefusal,
    PipelineRefusalCode,
    PipelineStep,
    run_post_merge_pipeline,
)
from eawf.workflow.release.pipeline_host import GitHubReleaseHost
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    provision,
    rekeyed,
    seed,
    seed_row,
)

VERSION: Final = "0.7.0.dev4"
NOW: Final = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
MILESTONE: Final = "MLS-0030"
SOURCE: Final = "f" * 40
TARGET: Final = "refs/remotes/origin/main"

#: Commits named by role, so a failing assertion reads as which one moved.
A1, A2, B1, B2, PHASE = ("a1" * 20, "a2" * 20, "b1" * 20, "b2" * 20, "9" * 40)


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _batch(key: str, status: str, *, head: str = PHASE, milestone: str = MILESTONE) -> Any:
    row = rekeyed(seed_row("batch", status), key=key)
    row["milestone_ref"] = f"{row['milestone_ref'].rsplit('/', 1)[0]}/{milestone}"
    if row.get("current_head_binding") is not None:
        row["current_head_binding"]["head_sha"] = head
    return row


def _task(key: str, status: str, *, batch: str) -> Any:
    row = rekeyed(seed_row("task", status), key=key)
    row["batch_ref"] = f"{row['batch_ref'].rsplit('/', 1)[0]}/{batch}"
    return row


def _compact(canary: CanaryProvision, collection: Epoch2Collection, key: str, row: Any) -> None:
    """Seed *row* into the document, then compact it into its ledger as a close does."""
    seed(canary, {collection.value: {key: row}})
    compact_terminal_record(
        document_path(canary),
        record=LedgerRecord(
            collection=collection,
            record_key=key,
            status=row["status"],
            recorded_at=NOW,
            payload=row,
        ),
    )


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """BAT-0007 and its Task closed and compacted; BAT-0008 closed but its Task live."""
    repo = tmp_path / "repo"
    repo.mkdir()
    made = provision(repo)
    seed(
        made,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {MILESTONE: seed_row("milestone", "ACTIVE")},
            "task": {"EAWF-0043": _task("EAWF-0043", "READY_TO_INTEGRATE", batch="BAT-0008")},
        },
    )
    _compact(made, Epoch2Collection.BATCH, "BAT-0007", _batch("BAT-0007", "COMPLETED", head=A2))
    _compact(
        made, Epoch2Collection.TASK, "EAWF-0042", _task("EAWF-0042", "COMPLETED", batch="BAT-0007")
    )
    _compact(made, Epoch2Collection.BATCH, "BAT-0008", _batch("BAT-0008", "COMPLETED"))
    return made


def _host(canary: CanaryProvision, listing: str = "") -> GitHubReleaseHost:
    host = GitHubReleaseHost(canary.root, state_path=canary.root / ".ea" / "state.json")
    host._run = lambda argv, **_kwargs: listing  # type: ignore[method-assign]
    return host


def _log(*entries: tuple[str, str]) -> str:
    """Return ``git log --format=%H%x00%B%x1e`` output for ``(sha, message)`` pairs."""
    return "".join(f"{sha}\x00{message}\n\x1e\n" for sha, message in entries)


# ---- the host reads closed Batches from the ledger -----------------------------------


def test_document_no_longer_holds_the_closed_batches(canary: CanaryProvision) -> None:
    held = document_rows(read_document(document_path(canary)), Epoch2Collection.BATCH)
    assert not {"BAT-0007", "BAT-0008"} & set(held)


def test_heads_read_completed_batches_compacted_into_the_ledger(canary: CanaryProvision) -> None:
    assert _host(canary).milestone_batch_heads(MILESTONE) == {"BAT-0007": A2, "BAT-0008": PHASE}


def test_heads_read_a_completed_batch_the_document_still_holds(canary: CanaryProvision) -> None:
    seed(canary, {"batch": {"BAT-0009": _batch("BAT-0009", "COMPLETED", head=B1)}})
    assert _host(canary).milestone_batch_heads(MILESTONE)["BAT-0009"] == B1


def test_heads_let_the_documents_live_row_win_over_the_ledger(canary: CanaryProvision) -> None:
    seed(canary, {"batch": {"BAT-0008": _batch("BAT-0008", "ACTIVE")}})
    assert _host(canary).milestone_batch_heads(MILESTONE) == {"BAT-0007": A2}


def test_heads_are_empty_for_another_milestone(canary: CanaryProvision) -> None:
    assert _host(canary).milestone_batch_heads("MLS-0031") == {}


def test_heads_skip_a_batch_of_another_milestone(canary: CanaryProvision) -> None:
    seed(canary, {"batch": {"BAT-0009": _batch("BAT-0009", "COMPLETED", milestone="MLS-0031")}})
    assert set(_host(canary).milestone_batch_heads(MILESTONE)) == {"BAT-0007", "BAT-0008"}


def test_heads_refuse_a_completed_row_that_does_not_validate(canary: CanaryProvision) -> None:
    row = _batch("BAT-0009", "COMPLETED")
    del row["current_head_binding"]
    seed(canary, {"batch": {"BAT-0009": row}})
    with pytest.raises(PipelineHostError, match="current_head_binding"):
        _host(canary).milestone_batch_heads(MILESTONE)


def test_heads_refuse_a_tree_that_is_not_native(tmp_path: Path) -> None:
    host = GitHubReleaseHost(tmp_path, state_path=tmp_path / ".ea" / "state.json")
    with pytest.raises(PipelineHostError, match=f"milestone {MILESTONE} batches"):
        host.milestone_batch_heads(MILESTONE)


# ---- the host maps commits to Batches through their Tasks ----------------------------


def test_carriers_map_task_trailers_through_compacted_and_live_tasks(
    canary: CanaryProvision,
) -> None:
    listing = _log(
        (B1, "fix: b\n\nTask: EAWF-0043"),
        (A2, "feat: a again\n\nTask: EAWF-0042"),
        (A1, "feat: a\n\nTask: EAWF-0042"),
    )
    assert _host(canary, listing).batch_carrier_commits(TARGET) == {
        "BAT-0007": (A2, A1),
        "BAT-0008": (B1,),
    }


def test_carriers_keep_a_batch_trailer_and_count_a_commit_once(canary: CanaryProvision) -> None:
    listing = _log(
        (A1, "feat: a\n\nBatch: BAT-0007\nTask: EAWF-0042"),
        (B2, "chore: legacy\n\nBatch: BAT-0001"),
    )
    assert _host(canary, listing).batch_carrier_commits(TARGET) == {
        "BAT-0001": (B2,),
        "BAT-0007": (A1,),
    }


def test_carriers_map_a_squashed_batch_commit_naming_several_tasks_once(
    canary: CanaryProvision,
) -> None:
    seed(
        canary, {"task": {"EAWF-0044": _task("EAWF-0044", "READY_TO_INTEGRATE", batch="BAT-0007")}}
    )
    listing = _log(
        (A1, "feat: a\n\nBatch: BAT-0007\nTask: EAWF-0042\nTask: EAWF-0044"),
        (B1, "fix: b\n\nTask: EAWF-0043"),
    )
    assert _host(canary, listing).batch_carrier_commits(TARGET) == {
        "BAT-0007": (A1,),
        "BAT-0008": (B1,),
    }


def test_carriers_skip_an_unplaced_task_and_an_inline_mention(canary: CanaryProvision) -> None:
    listing = _log(
        (A1, "chore: c\n\nTask: EAWF-9999"),
        (A2, "fix: names Task: EAWF-0042 inline"),
    )
    assert _host(canary, listing).batch_carrier_commits(TARGET) == {}


def test_carriers_are_empty_for_an_empty_log(canary: CanaryProvision) -> None:
    assert _host(canary).batch_carrier_commits(TARGET) == {}


def test_carriers_refuse_a_tree_that_is_not_native(tmp_path: Path) -> None:
    host = GitHubReleaseHost(tmp_path, state_path=tmp_path / ".ea" / "state.json")
    host._run = lambda argv, **_kwargs: ""  # type: ignore[method-assign]
    with pytest.raises(PipelineHostError, match=f"tasks behind {TARGET}"):
        host.batch_carrier_commits(TARGET)


# ---- the repin step accepts multi-commit Batches -------------------------------------


@dataclass
class _Host:
    """The repin step's view of git: pinned heads and the carriers below each ref."""

    repo_root: Path
    heads: dict[str, str]
    carriers: dict[str, dict[str, tuple[str, ...]]]
    reads: list[str] = field(default_factory=list)

    def head(self) -> str:
        return SOURCE

    def milestone_batch_heads(self, milestone_key: str) -> dict[str, str]:
        return dict(self.heads) if milestone_key == MILESTONE else {}

    def batch_carrier_commits(self, ref: str) -> dict[str, tuple[str, ...]]:
        self.reads.append(ref)
        if ref not in self.carriers:
            raise PipelineHostError(f"git log {ref} exited 128: unknown revision")
        return self.carriers[ref]


def _repin(host: _Host) -> PipelineRefusal | dict[str, str]:
    """Run the pipeline with every step but the repin journaled; return its outcome."""
    layout = PipelineLayout(host.repo_root, VERSION)
    layout.journal.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        PipelineJournalRow(
            version=VERSION,
            step=step,
            completed_at=NOW,
            facts=(
                {"source_sha": SOURCE, "milestone": MILESTONE}
                if step is PipelineStep.PIN_SOURCE
                else {}
            ),
        )
        for step in PIPELINE_STEPS
        if step is not PipelineStep.REPIN
    ]
    layout.journal.write_text("".join(row.model_dump_json() + "\n" for row in rows))
    options = PipelineOptions(version=VERSION, milestone_key=MILESTONE, publish=True)

    def refuse(method: str, params: Any) -> dict[str, Any]:
        raise AssertionError(f"the repin step calls no daemon method, got {method}")

    try:
        result = run_post_merge_pipeline(
            options, host=cast(PipelineHost, host), rpc=refuse, now=lambda: NOW
        )
    except PipelineRefusal as refusal:
        assert refusal.step is PipelineStep.REPIN
        return refusal
    return dict(result.outcomes[PIPELINE_STEPS.index(PipelineStep.REPIN)].facts)


def test_repin_passes_batches_of_several_commits_on_one_phase_head(tmp_path: Path) -> None:
    landed = {"BAT-0007": (A2, A1), "BAT-0008": (B2, B1)}
    host = _Host(tmp_path, {"BAT-0007": PHASE, "BAT-0008": PHASE}, {TARGET: landed, PHASE: landed})
    assert _repin(host) == {"milestone": MILESTONE, "batches": "2"}
    # Two Batches pinned on one head read that head's carriers once.
    assert host.reads == [TARGET, PHASE]


def test_repin_passes_a_single_batch_of_a_single_commit(tmp_path: Path) -> None:
    host = _Host(tmp_path, {"BAT-0007": A1}, {TARGET: {"BAT-0007": (A1,)}, A1: {"BAT-0007": (A1,)}})
    assert _repin(host) == {"milestone": MILESTONE, "batches": "1"}


def test_repin_refuses_a_commit_main_carries_past_the_pinned_head(tmp_path: Path) -> None:
    host = _Host(
        tmp_path,
        {"BAT-0007": A1},
        {TARGET: {"BAT-0007": (A2, A1)}, A1: {"BAT-0007": (A1,)}},
    )
    refusal = _repin(host)
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.BATCH_HEAD_MISMATCH
    assert f"BAT-0007 bound {A1[:12]}, landed {A2[:12]}" in refusal.detail


def test_repin_refuses_a_pinned_commit_main_does_not_carry(tmp_path: Path) -> None:
    host = _Host(
        tmp_path,
        {"BAT-0007": A2},
        {TARGET: {"BAT-0007": (A1,)}, A2: {"BAT-0007": (A2, A1)}},
    )
    refusal = _repin(host)
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.BATCH_HEAD_MISMATCH
    assert f"BAT-0007 bound {A2[:12]}, landed {A1[:12]}" in refusal.detail


def test_repin_refuses_a_batch_main_carries_no_commit_of(tmp_path: Path) -> None:
    host = _Host(tmp_path, {"BAT-0007": A1, "BAT-0008": B1}, {TARGET: {"BAT-0007": (A1,)}})
    refusal = _repin(host)
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.BATCH_TRAILER_MISSING
    assert refusal.detail.endswith("BAT-0008")
    assert host.reads == [TARGET]


def test_repin_refuses_a_milestone_with_no_completed_batch(tmp_path: Path) -> None:
    refusal = _repin(_Host(tmp_path, {}, {}))
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.MILESTONE_BATCHES_MISSING


def test_repin_refuses_a_pinned_head_it_cannot_read(tmp_path: Path) -> None:
    host = _Host(tmp_path, {"BAT-0007": A1}, {TARGET: {"BAT-0007": (A1,)}})
    refusal = _repin(host)
    assert isinstance(refusal, PipelineRefusal)
    assert refusal.code is PipelineRefusalCode.MILESTONE_UNREADABLE
    assert "unknown revision" in refusal.detail
