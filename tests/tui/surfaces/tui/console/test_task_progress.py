"""The console counts Task progress under each Track, Milestone and Batch.

A Batch stays PLANNED while another Batch of its repository is active, so counting only
completed parents reads "0 of N batches done" over work that is all ready. These tests
draw the home, Milestone and Batch frames from projections built the way the daemon
builds them, patch a Task live through the seam, and time the frames over a thousand
Tasks against the console's own frame budgets.
"""

from __future__ import annotations

import asyncio
import dataclasses
import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final

import pytest

from eawf.kernel.projection.compute import (
    ROUTE_READ_MODELS,
    KeyedPatch,
    PatchEntry,
    build_route_projection,
)
from eawf.kernel.projection.registers import ATTENTION_ROUTE
from eawf.kernel.projection.row_facts import BATCH_CEILING_FACT
from eawf.kernel.projection.spine import SpineRow, SpineView, build_spine_view
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.app import Body, ConsoleApp
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.perf import KEY_FLOOR_CEILING_MS, ROW_INCREMENT_CEILING_US
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.task_progress import Progress, TaskTally, batches_done
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.workflow.projection.acceptance import build_acceptance_view
from tests.tui.surfaces.tui.console.test_seam_pool import FakeDaemon, seam_over, settle

AT: Final = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE: Final = "EAWF"
CURSOR: Final = 41208
ROOT: Final = "eawf://EAWF/EAWF/EAWF"
REPO: Final = f"{ROOT}/repository/REP-EAWF"
WAITING: Final = "waiting: one integrating batch per repository"
#: The Batches the fixture's Batch ledger files an integration generation for.
INTEGRATING: Final = frozenset({f"{ROOT}/batch/BAT-0100", f"{ROOT}/batch/BAT-0104"})


def _row(kind: str, key: str, status: str, revision: int = 1, **extra: Any) -> dict[str, Any]:
    return {"urn": f"{ROOT}/{kind}/{key}", "revision": revision, "status": status, **extra}


def _track(ceiling: int = 1) -> dict[str, Any]:
    return _row(
        "track",
        "TRK-CORE",
        "ACTIVE",
        title="Core framework",
        policy={"wip": {"active_milestones": 1, "active_batches_per_repo": ceiling}},
    )


def _batch(key: str, status: str, milestone: str = "MLS-0100") -> dict[str, Any]:
    return _row(
        "batch", key, status, milestone_ref=f"{ROOT}/milestone/{milestone}", repository_ref=REPO
    )


def _task(key: str, status: str, batch: str) -> dict[str, Any]:
    return _row("task", key, status, batch_ref=f"{ROOT}/batch/{batch}")


def document(*, ceiling: int = 1, running: str = "RUNNING") -> dict[str, Any]:
    """Return one Track, one Milestone, an integrating Batch and two planned ones with Tasks.

    BAT-0101 holds only ready Tasks behind the integrating BAT-0100; BAT-0102 still has a Task
    at *running*; BAT-0103 is cancelled and so is never counted.
    """
    tasks = {
        "TSK-0001": _task("TSK-0001", "COMPLETED", "BAT-0100"),
        "TSK-0002": _task("TSK-0002", "READY_TO_INTEGRATE", "BAT-0100"),
        "TSK-0003": _task("TSK-0003", "READY_TO_INTEGRATE", "BAT-0101"),
        "TSK-0004": _task("TSK-0004", "READY_TO_INTEGRATE", "BAT-0101"),
        "TSK-0005": _task("TSK-0005", "READY_TO_INTEGRATE", "BAT-0102"),
        "TSK-0006": _task("TSK-0006", running, "BAT-0102"),
        "TSK-0007": _task("TSK-0007", "PLANNED", "BAT-0102"),
        "TSK-0008": _task("TSK-0008", "CANCELLED", "BAT-0103"),
        "TSK-0009": _task("TSK-0009", "DROPPED", "BAT-0102"),
    }
    return {
        "track": {"TRK-CORE": _track(ceiling)},
        "milestone": {
            "MLS-0100": _row(
                "milestone",
                "MLS-0100",
                "ACTIVE",
                title="Close out the canary",
                primary_track_ref=f"{ROOT}/track/TRK-CORE",
            ),
        },
        "batch": {
            "BAT-0100": _batch("BAT-0100", "ACTIVE"),
            "BAT-0101": _batch("BAT-0101", "PLANNED"),
            "BAT-0102": _batch("BAT-0102", "PLANNED"),
            "BAT-0103": _batch("BAT-0103", "CANCELLED"),
        },
        "task": tasks,
    }


def _model(route: str, doc: dict[str, Any], *, proofs: dict[str, str] | None = None) -> Any:
    projection = build_route_projection(
        route=route,
        document=doc,
        cursor=CURSOR,
        scope_id=SCOPE,
        generated_at=AT,
        integrating=INTEGRATING,
        proofs=proofs or {},
    )
    return (
        build_acceptance_view(projection) if route == "milestone" else build_spine_view(projection)
    )


def frame(route: str, doc: dict[str, Any], *, subject: str | None = None, w: int = 120) -> str:
    """Return the native frame of *route* over *doc*, built the way the app builds it."""
    session = Session()
    session.route = route
    session.subj_id = subject
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=w,
        h=dict(SIZES)[w],
        linked=True,
        projection=_model(route, doc),
    )
    return "\n".join(render_route(view))


def _line(text: str, needle: str) -> str:
    return next(line for line in text.splitlines() if needle in line)


# ---- the tally ----------------------------------------------------------------


def test_an_empty_tally_counts_nothing_and_says_so() -> None:
    tally = TaskTally.of([])
    assert tally.total == 0
    assert tally.cell() == "no task"
    assert tally.line() == "0 completed · 0 ready to integrate · 0 running · 0 planned of 0 tasks"


def test_a_single_ready_task_is_one_of_one_ready() -> None:
    assert TaskTally.of(["READY_TO_INTEGRATE"]).cell() == "1/1 ready"


def test_every_task_completed_reads_done_not_ready() -> None:
    assert TaskTally.of(["COMPLETED", "COMPLETED"]).cell() == "2/2 done"


def test_one_task_short_of_ready_names_the_running_one() -> None:
    tally = TaskTally.of(["READY_TO_INTEGRATE", "COMPLETED", "RUNNING", "CLAIMED"])
    assert (tally.completed, tally.ready, tally.running) == (1, 1, 2)
    assert tally.cell() == "2/4 ready · 2 running"


def test_one_task_is_named_in_the_singular() -> None:
    assert TaskTally.of(["PLANNED"]).line().endswith("1 planned of 1 task")
    doc = document()
    doc["task"] = {"TSK-0001": _task("TSK-0001", "PLANNED", "BAT-0101")}
    assert "0 of 1 task ready" in _line(frame("scope.home", doc), "TRK-CORE")


def test_cancelled_and_dropped_tasks_are_not_work() -> None:
    tally = TaskTally.of(["CANCELLED", "DROPPED", "PLANNED"])
    assert tally.total == 1
    assert tally.planned == 1


def test_a_failed_task_is_named_on_the_line_only_when_one_failed() -> None:
    assert TaskTally.of(["FAILED", "PLANNED"]).line().endswith("1 planned · 1 failed of 2 tasks")
    assert "failed" not in TaskTally.of(["PLANNED"]).line()


def test_a_task_with_no_stated_status_counts_as_planned() -> None:
    assert TaskTally.of([None]).planned == 1


def test_batches_done_counts_one_batch_in_the_singular() -> None:
    model = _model("scope.home", document())
    batches = [row for row in model.rows if row.key == "BAT-0100"]
    assert batches_done(batches) == "0 of 1 batch done"
    assert batches_done([]) == "0 of 0 batches done"


# ---- the waiting note -----------------------------------------------------------


def _batch_row(spine: SpineView, key: str) -> SpineRow:
    return next(row for row in spine.rows if row.key == key)


def test_a_planned_batch_all_ready_behind_the_integrating_one_waits() -> None:
    spine = _model("scope.home", document())
    filed = Progress.of(spine.rows)
    assert filed.batch_cell(_batch_row(spine, "BAT-0101")) == f"2/2 ready · {WAITING}"


def test_a_batch_with_a_task_still_running_does_not_wait_on_the_ceiling() -> None:
    spine = _model("scope.home", document())
    filed = Progress.of(spine.rows)
    assert filed.batch_cell(_batch_row(spine, "BAT-0102")) == "1/3 ready · 1 running"


def test_the_integrating_batch_itself_never_waits() -> None:
    spine = _model("scope.home", document())
    filed = Progress.of(spine.rows)
    assert filed.batch_cell(_batch_row(spine, "BAT-0100")) == "2/2 ready"


def test_a_ceiling_of_two_admits_the_ready_batch_beside_one_integrating() -> None:
    spine = _model("scope.home", document(ceiling=2))
    filed = Progress.of(spine.rows)
    assert filed.batch_cell(_batch_row(spine, "BAT-0101")) == "2/2 ready"


def test_the_ceiling_is_read_from_the_track_policy_and_named_by_its_value() -> None:
    doc = document(ceiling=2)
    doc["batch"]["BAT-0104"] = _batch("BAT-0104", "ACTIVE")
    spine = _model("scope.home", doc)
    row = _batch_row(spine, "BAT-0101")
    assert row.facts[BATCH_CEILING_FACT] == "2"
    assert row.facts["repository"] == "REP-EAWF"
    assert row.facts["track"] == "TRK-CORE"
    note = Progress.of(spine.rows).batch_cell(row)
    assert note == "2/2 ready · waiting: 2 integrating batches per repository"


def test_a_batch_whose_track_states_no_ceiling_is_never_said_to_wait() -> None:
    doc = document()
    doc["track"]["TRK-CORE"].pop("policy")
    spine = _model("scope.home", doc)
    row = _batch_row(spine, "BAT-0101")
    assert BATCH_CEILING_FACT not in row.facts
    assert Progress.of(spine.rows).batch_cell(row) == "2/2 ready"


def test_a_batch_of_another_repository_does_not_hold_the_ceiling() -> None:
    doc = document()
    doc["batch"]["BAT-0100"]["repository_ref"] = f"{ROOT}/repository/REP-OTHER"
    spine = _model("scope.home", doc)
    assert Progress.of(spine.rows).batch_cell(_batch_row(spine, "BAT-0101")) == "2/2 ready"


def test_a_ceiling_that_is_not_a_count_is_never_read_as_one() -> None:
    spine = _model("scope.home", document())
    row = _batch_row(spine, "BAT-0101")
    unstated = dataclasses.replace(row, facts={**row.facts, BATCH_CEILING_FACT: "many"})
    filed = Progress.of(spine.rows)
    assert filed.waiting(unstated, TaskTally.of(["READY_TO_INTEGRATE"])) is None


@pytest.mark.parametrize(
    "note",
    ["proving 2 criteria at 0123456789ab", "CR-0042-1 unmet at 0123456789ab"],
)
def test_a_batch_held_by_its_proof_says_why_on_its_row(note: str) -> None:
    proofs = {f"{ROOT}/batch/BAT-0100": note}
    spine = _model("scope.home", document(), proofs=proofs)
    filed = Progress.of(spine.rows)
    assert filed.batch_cell(_batch_row(spine, "BAT-0100")) == f"2/2 ready · waiting: {note}"
    assert filed.batch_cell(_batch_row(spine, "BAT-0102")) == "1/3 ready · 1 running"


# ---- the frames -----------------------------------------------------------------


def test_home_track_row_counts_tasks_ready_and_batches_done() -> None:
    text = frame("scope.home", document())
    track = _line(text, "TRK-CORE")
    assert "1 completed · 4 ready of 7 tasks · 0 of" in track


def test_home_track_with_no_task_keeps_counting_its_milestones() -> None:
    doc = document()
    doc.pop("task")
    track = _line(frame("scope.home", doc), "TRK-CORE")
    assert "0 of 1 milestone done" in track


def test_home_track_progress_is_clipped_never_wrapped_at_eighty_columns() -> None:
    text = frame("scope.home", document(), w=80)
    track = _line(text, "TRK-CORE")
    assert "1 completed · 4 ready of 7" in track
    assert len(text.splitlines()) == dict(SIZES)[80]


def test_milestone_frame_states_tasks_by_status_and_batches_done() -> None:
    text = frame("milestone", document(), subject="MLS-0100")
    progress = _line(text, "PROGRESS")
    assert (
        "1 completed · 4 ready to integrate · 1 running · 1 planned of 7 tasks · "
        "0 of 3 batches done"
    ) in progress


def test_milestone_batch_rows_carry_their_task_progress_and_the_wait() -> None:
    text = frame("milestone", document(), subject="MLS-0100")
    assert "BAT-0100 · ACTIVE · 2/2 ready" in text
    assert f"BAT-0101 · PLANNED · 2/2 ready · {WAITING}" in text
    assert "BAT-0102 · PLANNED · 1/3 ready · 1 running" in text
    assert "BAT-0103 · CANCELLED · no task" in text


def test_batch_detail_counts_its_tasks_and_says_why_it_waits() -> None:
    text = frame("batch.detail", document(), subject="BAT-0101")
    assert f"2 tasks · 0 completed · 2/2 ready · {WAITING}" in _line(text, "COUNT")


def test_batch_detail_with_no_task_keeps_its_count_line() -> None:
    doc = document()
    doc["batch"]["BAT-0105"] = _batch("BAT-0105", "PLANNED")
    count = _line(frame("batch.detail", doc, subject="BAT-0105"), "COUNT")
    assert count.split("COUNT", 1)[1].strip() == "0 tasks · 0 completed"


# ---- live: a Task patch moves the counts on screen --------------------------------


class _HomeDaemon(FakeDaemon):
    """The stand-in daemon, serving the home route over the probe document."""

    def answer(self, method: str) -> dict[str, Any]:
        answer = super().answer(method)
        if answer.get("route") != "scope.home":
            return answer
        return build_route_projection(
            route="scope.home", document=document(), cursor=7, scope_id=SCOPE, generated_at=AT
        ).model_dump(mode="json")


def _task_patch(status: str, sequence: int) -> KeyedPatch:
    entry = PatchEntry(
        key="TSK-0006",
        urn=f"{ROOT}/task/TSK-0006",
        collection=Epoch2Collection.TASK,
        revision=2,
        status=status,
    )
    return KeyedPatch(
        schema_version="1.0",
        projection_kind=ROUTE_READ_MODELS["scope.home"],
        routes=("scope.home",),
        scope_id=SCOPE,
        canonical_sequence=sequence,
        entries=(entry,),
    )


def test_a_task_patch_moves_the_home_counts_without_a_blank_frame() -> None:
    daemon = _HomeDaemon()
    seam = seam_over(daemon)

    async def drive() -> tuple[str, list[str], int]:
        app = ConsoleApp(clock=FakeClock(), seam=seam)
        painted: list[str] = []
        async with app.run_test(size=SIZES[1]) as pilot:
            await settle(app, pilot)
            body = app.query_one("#body", Body)
            before = "\n".join(body.rows)
            renders = app.render_count
            await seam.apply_patch(_task_patch("READY_TO_INTEGRATE", 8))
            painted.append("\n".join(body.rows))
            await settle(app, pilot)
            painted.append("\n".join(body.rows))
            return before, painted, app.render_count - renders

    before, painted, repaints = asyncio.run(drive())
    assert "1 completed · 4 ready of 7 tasks" in before
    assert all("TRK-CORE" in shot for shot in painted), "a frame went blank during the patch"
    assert "1 completed · 5 ready of 7 tasks" in painted[-1]
    # the patch repaints at once, and the read it owes for what it could not state again
    assert repaints == 2
    assert daemon.reads == ["scope.home", ATTENTION_ROUTE, "scope.home"]


# ---- cost: O(rows), within the console's frame budgets ----------------------------


def big_document(tasks: int) -> dict[str, Any]:
    """Return the probe Track with *tasks* Tasks spread ten to a Batch."""
    doc = document()
    doc["batch"] = {}
    doc["task"] = {}
    for n in range(tasks // 10):
        key = f"BAT-{n:04d}"
        doc["batch"][key] = _batch(key, "ACTIVE" if n == 0 else "PLANNED")
        for m in range(10):
            task = f"TSK-{n * 10 + m:05d}"
            doc["task"][task] = _task(task, "READY_TO_INTEGRATE", key)
    return doc


def _median_ms(draw: Callable[[], object], samples: int = 15) -> float:
    draw()
    times = []
    for _ in range(samples):
        start = time.perf_counter()
        draw()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


@pytest.mark.parametrize("route", ["scope.home", "milestone"])
def test_a_thousand_tasks_render_within_the_frame_budget(route: str) -> None:
    subject = "MLS-0100" if route == "milestone" else None

    def draw(model: Any) -> Callable[[], object]:
        session = Session()
        session.route = route
        session.subj_id = subject
        view = View(
            session=session,
            fixture=Fixture.from_chrome(load_chrome()),
            w=120,
            h=dict(SIZES)[120],
            linked=True,
            projection=model,
        )
        return lambda: render_route(view)

    small = _median_ms(draw(_model(route, big_document(1000))))
    large = _median_ms(draw(_model(route, big_document(4000))))
    assert small < KEY_FLOOR_CEILING_MS, f"{route} took {small:.2f}ms over 1,000 Tasks"
    per_row_us = (large - small) * 1000 / 3000
    assert per_row_us < ROW_INCREMENT_CEILING_US, f"{route} adds {per_row_us:.2f}us per Task"
