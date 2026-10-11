"""Home, the Milestone route and Activity tell the truth once a Task completes and compacts.

A Task leaves ``state.json`` on the commit that completes it, into its generation's Task
ledger. The tree here is a canary seeded with one Track, Milestone and Batch whose Tasks
are completed and then compacted by the store's own compaction, each Task with a Run the
document still holds. Each route is read fresh through the daemon's read verb and drawn
the way the console draws it, so the words checked are the words an operator reads.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.store.compaction import compact_terminal_task, document_rows, read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.task_progress import TaskTally
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.workflow.projection.acceptance import build_acceptance_view
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    rekeyed,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_native_dispatch import make_canary

AT: Final = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
MILESTONE: Final = "MLS-0030"
TITLE: Final = "Publish the wheel to the index"
WIDTH: Final = 120


def _task(key: str, status: str) -> dict[str, Any]:
    return rekeyed(seed_row("task", status), key=key)


def _run(key: str, task: str) -> dict[str, Any]:
    row = rekeyed(seed_row("run", "COMPLETED"), key=key)
    row["scope"]["task_ref"] = f"{str(row['scope']['task_ref']).rsplit('/', 1)[0]}/{task}"
    return row


def _tree(
    tmp_path: Path, tasks: dict[str, str], *, runs: dict[str, str] | None = None
) -> CanaryProvision:
    """Seed one Track, Milestone and Batch over *tasks*, then compact the completed ones.

    Args:
        tmp_path: Where the canary is provisioned.
        tasks: Each Task's status, by key.
        runs: Each Run's Task, by Run key.
    """
    canary = make_canary(tmp_path / "repo", rows={})
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {MILESTONE: seed_row("milestone", "ACTIVE")},
            "batch": {"BAT-0007": seed_row("batch", "ACTIVE")},
            "task": {key: _task(key, status) for key, status in tasks.items()},
            "run": {key: _run(key, task) for key, task in (runs or {}).items()},
        },
    )
    path = document_path(canary)
    held = document_rows(read_document(path), Epoch2Collection.TASK)
    for key, status in tasks.items():
        if status == "COMPLETED":
            compact_terminal_task(path, task=Task.model_validate(held[key]), recorded_at=AT)
    return canary


def _read(canary: CanaryProvision, tmp_path: Path, route: str, **params: Any) -> RouteProjection:
    ctx = method_context(tmp_path / "runtime")
    answer = asyncio.run(
        methods.dispatch(f"projection.{route}.read", ctx, {"repo_root": str(canary.root), **params})
    )
    return RouteProjection.model_validate(answer)


def _paint(route: str, model: Any, *, subject: str | None = None) -> str:
    session = Session()
    session.route = route
    session.subj_id = subject
    bound = {"register": model} if route == "activity" else {"projection": model}
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=WIDTH,
        h=dict(SIZES)[WIDTH],
        linked=True,
        **bound,
    )
    return "\n".join(render_route(view))


def _home(canary: CanaryProvision, tmp_path: Path) -> str:
    text = _paint("scope.home", build_spine_view(_read(canary, tmp_path, "scope.home")))
    return next(line for line in text.splitlines() if "TRK-RUNTIME" in line)


def _milestone(canary: CanaryProvision, tmp_path: Path) -> str:
    urn = seed_row("milestone", "ACTIVE")["urn"]
    model = build_acceptance_view(_read(canary, tmp_path, "milestone", key=urn))
    text = _paint("milestone", model, subject=MILESTONE)
    return next(line for line in text.splitlines() if "PROGRESS" in line)


# ---- home and the Milestone agree ------------------------------------------------


def test_every_task_completed_and_compacted_reads_completed_on_home_and_milestone(
    tmp_path: Path,
) -> None:
    canary = _tree(tmp_path, {"EAWF-0042": "COMPLETED", "EAWF-0043": "COMPLETED"})
    home, milestone = _home(canary, tmp_path), _milestone(canary, tmp_path)
    assert "2 of 2 tasks completed · 0 of 1 batch done" in home
    assert "ready" not in home
    assert "2 completed · 0 ready to integrate" in milestone
    assert "of 2 tasks" in milestone


def test_mixed_ready_and_completed_tasks_are_counted_apart_in_the_same_words(
    tmp_path: Path,
) -> None:
    canary = _tree(tmp_path, {"EAWF-0042": "COMPLETED", "EAWF-0043": "READY_TO_INTEGRATE"})
    home, milestone = _home(canary, tmp_path), _milestone(canary, tmp_path)
    assert "1 completed · 1 ready of 2 tasks" in home
    assert "1 completed · 1 ready to integrate" in milestone


def test_a_milestone_with_no_task_is_counted_by_its_milestones_on_home(tmp_path: Path) -> None:
    canary = _tree(tmp_path, {})
    assert "0 of 1 milestone done" in _home(canary, tmp_path)
    assert "of 0 tasks" in _milestone(canary, tmp_path)


def test_the_headline_of_an_empty_tally_names_no_task() -> None:
    assert TaskTally.of([]).headline() == "0 of 0 tasks ready"


def test_the_headline_of_one_completed_task_is_singular_and_completed() -> None:
    assert TaskTally.of(["COMPLETED"]).headline() == "1 of 1 task completed"


def test_the_headline_one_short_of_completed_names_the_ready_one_apart() -> None:
    tally = TaskTally.of(["COMPLETED", "COMPLETED", "READY_TO_INTEGRATE"])
    assert tally.headline() == "2 completed · 1 ready of 3 tasks"


def test_the_headline_with_none_completed_counts_ready_of_all() -> None:
    assert TaskTally.of(["READY_TO_INTEGRATE", "PLANNED"]).headline() == "1 of 2 tasks ready"


def test_the_headline_names_no_completed_task_when_a_completed_one_was_dropped() -> None:
    assert TaskTally.of(["DROPPED", "READY_TO_INTEGRATE"]).headline() == "1 of 1 task ready"


# ---- Activity keeps the Task's title ---------------------------------------------


def _activity_row(canary: CanaryProvision, tmp_path: Path, run: str) -> tuple[Any, str]:
    model = build_register_view(_read(canary, tmp_path, "activity"))
    row = next(row for row in model.rows if row.key == run)
    line = next(line for line in _paint("activity", model).splitlines() if run in line)
    return row, line


def test_a_run_whose_task_compacted_keeps_the_task_title(tmp_path: Path) -> None:
    canary = _tree(tmp_path, {"EAWF-0042": "COMPLETED"}, runs={"RUN-00000001": "EAWF-0042"})
    assert "EAWF-0042" not in document_rows(
        read_document(document_path(canary)), Epoch2Collection.TASK
    )
    row, line = _activity_row(canary, tmp_path, "RUN-00000001")
    assert row.facts["task_title"] == TITLE
    assert row.facts["task_status"] == "COMPLETED"
    # the TASK column clips the title at a word, so the frame shows its first words
    assert "EAWF-0042 Publish the" in line


def test_a_run_whose_task_is_held_keeps_the_document_title(tmp_path: Path) -> None:
    canary = _tree(tmp_path, {"EAWF-0044": "RUNNING"}, runs={"RUN-00000002": "EAWF-0044"})
    row, _line = _activity_row(canary, tmp_path, "RUN-00000002")
    assert row.facts["task_title"] == TITLE
    assert row.facts["task_status"] == "RUNNING"


def test_a_run_whose_task_is_nowhere_names_only_the_key(tmp_path: Path) -> None:
    canary = _tree(tmp_path, {}, runs={"RUN-00000003": "EAWF-0099"})
    row, line = _activity_row(canary, tmp_path, "RUN-00000003")
    assert "task_title" not in row.facts
    assert "EAWF-0099" in line
