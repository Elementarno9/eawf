"""Home and the Milestone route count a Task as completed after it compacts out.

A Task leaves the document on the commit that completes it. A home read of the document
alone would count a shipped Milestone's work back toward zero, so the daemon's home read
merges the Batches and Tasks filed under the Milestones it holds from their ledgers.

The tree is a canary holding one Track, Milestone and Batch with four Tasks; two are
completed and then compacted by the store's own compaction, so ``state.json`` really no
longer holds them. Both routes are then read fresh through the daemon's read verbs and
counted the way the console counts them.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.store.compaction import compact_terminal_task, document_rows, read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.surfaces.tui.console.renderers.task_progress import Progress
from eawf.workflow.projection.acceptance import build_acceptance_view
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    rekeyed,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_native_dispatch import make_canary

pytestmark = pytest.mark.integration

AT: Final = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
MILESTONE: Final = "MLS-0030"
COMPLETED: Final = ("EAWF-0042", "EAWF-0043")


def _tree(tmp_path: Path, *, compact: bool = True) -> CanaryProvision:
    """Seed the tree, then compact its completed Tasks out of the document."""
    canary = make_canary(tmp_path / "repo", rows={})
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {MILESTONE: seed_row("milestone", "ACTIVE")},
            "batch": {"BAT-0007": seed_row("batch", "ACTIVE")},
            "task": {
                **{key: rekeyed(seed_row("task", "COMPLETED"), key=key) for key in COMPLETED},
                "EAWF-0044": rekeyed(seed_row("task", "RUNNING"), key="EAWF-0044"),
                "EAWF-0045": rekeyed(seed_row("task", "DRAFT"), key="EAWF-0045"),
                "EAWF-0046": rekeyed(seed_row("task", "DROPPED"), key="EAWF-0046"),
            },
        },
    )
    if compact:
        path = document_path(canary)
        held = document_rows(read_document(path), Epoch2Collection.TASK)
        for key in COMPLETED:
            compact_terminal_task(path, task=Task.model_validate(held[key]), recorded_at=AT)
    return canary


def _read(canary: CanaryProvision, tmp_path: Path, route: str, **params: Any) -> RouteProjection:
    ctx = method_context(tmp_path / "runtime")
    answer = asyncio.run(
        methods.dispatch(f"projection.{route}.read", ctx, {"repo_root": str(canary.root), **params})
    )
    return RouteProjection.model_validate(answer)


def test_the_completed_tasks_have_left_the_document(tmp_path: Path) -> None:
    held = document_rows(read_document(document_path(_tree(tmp_path))), Epoch2Collection.TASK)
    assert not set(COMPLETED) & set(held)
    assert "EAWF-0044" in held


def test_home_counts_compacted_tasks_as_completed_after_a_fresh_read(tmp_path: Path) -> None:
    spine = build_spine_view(_read(_tree(tmp_path), tmp_path, "scope.home"))
    tally, batches = Progress.of(spine.rows).milestones((MILESTONE,))
    assert (tally.completed, tally.running, tally.total) == (2, 1, 3)
    assert [batch.key for batch in batches] == ["BAT-0007"]


def test_the_milestone_route_counts_the_same_completed_tasks(tmp_path: Path) -> None:
    canary = _tree(tmp_path)
    urn = seed_row("milestone", "ACTIVE")["urn"]
    model = build_acceptance_view(_read(canary, tmp_path, "milestone", key=urn))
    tally, _batches = Progress.of(model.rows).milestones((MILESTONE,))
    assert (tally.completed, tally.running) == (2, 1)


def test_home_counts_the_same_before_and_after_compaction(tmp_path: Path) -> None:
    live = build_spine_view(_read(_tree(tmp_path / "a", compact=False), tmp_path, "scope.home"))
    closed = build_spine_view(_read(_tree(tmp_path / "b"), tmp_path, "scope.home"))
    assert (
        Progress.of(live.rows).milestones((MILESTONE,))[0]
        == Progress.of(closed.rows).milestones((MILESTONE,))[0]
    )


def test_home_holds_no_draft_or_dropped_task_and_its_count_agrees(tmp_path: Path) -> None:
    spine = build_spine_view(_read(_tree(tmp_path), tmp_path, "scope.home"))
    tasks = [row for row in spine.rows if row.collection is Epoch2Collection.TASK]
    assert sorted(row.key for row in tasks) == ["EAWF-0042", "EAWF-0043", "EAWF-0044"]
    assert spine.count("task") == 3
