"""The child lists pad each status to the widest status of the list.

A Milestone's Batches, a Batch's Tasks and a Task's Runs each read ``key · STATUS`` and
then one more column; padding the status to the widest status of that list, not of the
enum, starts the following column at one cell on every row without wasting the row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.spine import SpineRow, build_spine_view
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.inline import Inline, Marked
from eawf.surfaces.tui.console.paint import Part, paint
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.children import (
    padded_status,
    reference,
    status,
    status_width,
)
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.surfaces.tui.console.width import cell_len
from eawf.workflow.projection.acceptance import build_acceptance_view

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
ROOT = "eawf://EAWF/EAWF/EAWF"
W = 160


def _row(kind: str, key: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"urn": f"{ROOT}/{kind}/{key}", "revision": 1, "status": status, **extra}


def _under(kind: str, key: str) -> str:
    return f"{ROOT}/{kind}/{key}"


def _document(
    batches: tuple[str, ...] = (), tasks: tuple[str, ...] = (), runs: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Return one Milestone with ``batches``; BAT-0100 holds ``tasks``; TSK-0100 ``runs``."""
    return {
        "track": {"TRK-CORE": _row("track", "TRK-CORE", "ACTIVE", title="Core framework")},
        "milestone": {
            "MLS-0100": _row(
                "milestone",
                "MLS-0100",
                "ACTIVE",
                title="Close out the canary",
                primary_track_ref=_under("track", "TRK-CORE"),
            )
        },
        "batch": {
            f"BAT-{100 + n:04d}": _row(
                "batch", f"BAT-{100 + n:04d}", state, milestone_ref=_under("milestone", "MLS-0100")
            )
            for n, state in enumerate(batches or ("ACTIVE",))
        },
        "task": {
            f"TSK-{100 + n:04d}": _row(
                "task",
                f"TSK-{100 + n:04d}",
                state,
                intent=f"Task number {n}",
                batch_ref=_under("batch", "BAT-0100"),
            )
            for n, state in enumerate(tasks)
        },
        "run": {
            f"RUN-{100 + n:08d}": _row(
                "run",
                f"RUN-{100 + n:08d}",
                state,
                created_at=f"2026-09-17T09:0{n}:00Z",
                started_at=f"2026-09-17T09:0{n}:05Z",
                scope={"purpose": "implement", "task_ref": _under("task", "TSK-0100")},
            )
            for n, state in enumerate(runs)
        },
    }


def _frame(route: str, subject: str, document: dict[str, Any]) -> list[str]:
    projection = build_route_projection(
        route=route, document=document, cursor=41208, scope_id=SCOPE, generated_at=AT
    )
    model = (
        build_acceptance_view(projection) if route == "milestone" else build_spine_view(projection)
    )
    session = Session()
    session.route, session.subj_id = route, subject
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=W,
        h=dict(SIZES)[W],
        projection=model,
    )
    return render_route(view)


def _listed(frame: list[str], prefix: str) -> list[str]:
    """Return the child rows of the frame whose key starts with ``prefix``, in order."""
    return [row for row in frame if f" {prefix}" in row and " · " in row]


def _next_column(row: str, word: str) -> int:
    """Return the cell where the column after the status ``word`` starts."""
    end = row.index(f" · {word}") + len(f" · {word}")
    return end + len(row[end:]) - len(row[end:].lstrip(" "))


# ---------- each list starts its following column at one cell ----------

BATCHES = ("ACTIVE", "MERGED_PENDING_RECONCILIATION", "CANCELLED", "PLANNED")
TASKS = ("RUNNING", "READY_TO_INTEGRATE", "COMPLETED", "PLANNED")
RUNS = ("QUEUED", "SUSPENDED", "RUNNING", "FAILED")


@pytest.mark.parametrize(
    ("route", "subject", "prefix", "states", "document", "gap"),
    [
        ("milestone", "MLS-0100", "BAT-", BATCHES, _document(batches=BATCHES), 1),
        ("batch.detail", "BAT-0100", "TSK-", TASKS, _document(tasks=TASKS), 2),
        ("task.detail", "TSK-0100", "RUN-", RUNS, _document(tasks=("RUNNING",), runs=RUNS), 1),
    ],
    ids=["milestone-batches", "batch-tasks", "task-runs"],
)
def test_mixed_width_statuses_start_the_next_column_at_one_cell(
    route: str,
    subject: str,
    prefix: str,
    states: tuple[str, ...],
    document: dict[str, Any],
    gap: int,
) -> None:
    rows = _listed(_frame(route, subject, document), prefix)
    assert len(rows) == len(states)
    starts = {_next_column(row, state) for row, state in zip(rows, states, strict=True)}
    assert len(starts) == 1, rows
    widest = max(cell_len(state) for state in states)
    row, state = rows[0], states[0]
    # the widest status of the list decides the pad: one gap past it, no more
    assert starts.pop() == row.index(f" · {state}") + 3 + widest + gap


def test_the_widest_batch_status_is_the_list_width_not_the_enum_width() -> None:
    rows = _listed(
        _frame("milestone", "MLS-0100", _document(batches=("ACTIVE", "PLANNED"))), "BAT-"
    )
    assert "BAT-0100 · ACTIVE  · " in rows[0]
    assert "BAT-0101 · PLANNED · " in rows[1]


def test_the_widest_task_status_pads_the_others_to_it() -> None:
    rows = _listed(
        _frame("batch.detail", "BAT-0100", _document(tasks=("READY_TO_INTEGRATE", "PLANNED"))),
        "TSK-",
    )
    assert "TSK-0100 · READY_TO_INTEGRATE  Task number 0" in rows[0]
    assert f"TSK-0101 · {'PLANNED':<18}  Task number 1" in rows[1]


# ---------- a Batch's title comes after its status and tally ----------


def _titled(titles: dict[str, str], tasks: tuple[str, ...] = ()) -> dict[str, Any]:
    """Return a Milestone of four Batches, ``titles`` keyed by Batch, BAT-0100 with ``tasks``."""
    document = _document(batches=BATCHES, tasks=tasks)
    for key, title in titles.items():
        document["batch"][key]["title"] = title
    return document


def test_titled_and_untitled_batches_align_status_and_tally_and_end_with_the_title() -> None:
    titles = {"BAT-0101": "Re-key the ledgers", "BAT-0103": "Close the rc1 bookkeeping"}
    rows = _listed(
        _frame("milestone", "MLS-0100", _titled(titles, tasks=("RUNNING", "COMPLETED"))), "BAT-"
    )
    assert len(rows) == len(BATCHES)
    keys = [f"BAT-{100 + n:04d}" for n in range(len(BATCHES))]
    starts = {row.index(key) + len(f"{key} · ") for row, key in zip(rows, keys, strict=True)}
    assert len(starts) == 1, rows
    status_at = starts.pop()
    # the tally starts one separator past the widest status, on every row
    tally_at = status_at + max(cell_len(state) for state in BATCHES) + len(" · ")
    tallies = set()
    for row, key, state in zip(rows, keys, BATCHES, strict=True):
        assert row[status_at:].startswith(state)
        assert row[tally_at - len(" · ") : tally_at] == " · ", row
        tallies.add(row[tally_at:].split(" · ")[0].rstrip())
        if key in titles:
            assert row.rstrip().endswith(f" · {titles[key]}")
    assert tallies == {"1/2 ready", "no task"}
    # the tally of a titled row is padded so every title starts at one cell
    title_at = {
        row.index(titles[key]) for row, key in zip(rows, keys, strict=True) if key in titles
    }
    assert len(title_at) == 1, rows


def test_an_untitled_batch_row_has_no_trailing_separator() -> None:
    frame = _frame("milestone", "MLS-0100", _titled({"BAT-0101": "Re-key the ledgers"}))
    (row,) = [row for row in _listed(frame, "BAT-") if "BAT-0102" in row]
    assert "BAT-0102 · CANCELLED" in row
    assert not row.rstrip().endswith("·")
    assert row.rstrip().endswith("no task")


def test_an_untitled_batch_reference_ends_at_its_last_column() -> None:
    row = _spine("BAT-0100", "ACTIVE")
    assert reference(row, len("ACTIVE") + 4, ("1/2 done  ",)) == "BAT-0100 · ACTIVE     · 1/2 done"


def test_a_markdown_batch_title_keeps_its_spans_at_its_new_offset() -> None:
    frame = _frame("milestone", "MLS-0100", _titled({"BAT-0100": "Ship `eawf` **now**"}))
    (row,) = [row for row in frame if "BAT-0100" in row and " · " in row]
    assert isinstance(row, Marked)
    assert "`" not in row
    assert "*" not in row
    styled = {span.style: row[span.start : span.end] for span in row.spans}
    assert styled == {Inline.CODE: "eawf", Inline.BOLD: "now"}
    assert row.index("Ship eawf now") > row.index("ACTIVE")


def test_a_titled_batch_reference_shifts_its_spans_past_the_columns() -> None:
    document = _titled({"BAT-0100": "Ship `eawf`"})
    projection = build_route_projection(
        route="batch.detail", document=document, cursor=41208, scope_id=SCOPE, generated_at=AT
    )
    row = next(r for r in build_spine_view(projection).rows if r.key == "BAT-0100")
    laid = reference(row, 0, ("0/0 done",))
    assert laid == "BAT-0100 · ACTIVE · 0/0 done · Ship eawf"
    (span,) = laid.spans
    assert laid[span.start : span.end] == "eawf"


# ---------- boundary cases ----------


@pytest.mark.parametrize(
    ("route", "subject", "document", "empty"),
    [
        ("batch.detail", "BAT-0100", _document(), "∅ no Task is filed under this Batch"),
        ("task.detail", "TSK-0100", _document(tasks=("PLANNED",)), "∅ no Run of this Task"),
    ],
)
def test_an_empty_list_says_so(
    route: str, subject: str, document: dict[str, Any], empty: str
) -> None:
    assert any(empty in row for row in _frame(route, subject, document))


@pytest.mark.parametrize(
    ("route", "subject", "prefix", "document", "expected"),
    [
        ("milestone", "MLS-0100", "BAT-", _document(batches=("CANCELLED",)), "· CANCELLED · "),
        (
            "batch.detail",
            "BAT-0100",
            "TSK-",
            _document(tasks=("READY_TO_INTEGRATE",)),
            "· READY_TO_INTEGRATE  Task",
        ),
        (
            "task.detail",
            "TSK-0100",
            "RUN-",
            _document(tasks=("RUNNING",), runs=("SUSPENDED",)),
            "· SUSPENDED · started",
        ),
    ],
)
def test_a_single_row_is_padded_to_its_own_status(
    route: str, subject: str, prefix: str, document: dict[str, Any], expected: str
) -> None:
    (row,) = _listed(_frame(route, subject, document), prefix)
    assert expected in row


def test_statuses_of_one_width_take_no_extra_cells() -> None:
    rows = _listed(
        _frame("milestone", "MLS-0100", _document(batches=("PLANNED", "PLANNED", "PLANNED"))),
        "BAT-",
    )
    assert len(rows) == 3
    assert all("· PLANNED · no task" in row for row in rows)


def test_the_padding_is_not_tinted_with_the_status() -> None:
    rows = _listed(
        _frame("batch.detail", "BAT-0100", _document(tasks=("READY_TO_INTEGRATE", "PLANNED"))),
        "TSK-",
    )
    strokes = paint(rows[1], Part.BODY)
    tinted = [s.text for s in strokes if s.surface == "info"]
    assert tinted == ["PLANNED"]


# ---------- the column helpers ----------


def _spine(key: str, state: str | None) -> SpineRow:
    document = _document(batches=(state,) if state else ("ACTIVE",))
    if state is None:
        del document["batch"]["BAT-0100"]["status"]
    projection = build_route_projection(
        route="batch.detail", document=document, cursor=41208, scope_id=SCOPE, generated_at=AT
    )
    view = build_spine_view(projection)
    return next(row for row in view.rows if row.key == key)


def test_status_width_of_no_rows_is_zero() -> None:
    assert status_width([]) == 0


def test_status_width_is_the_widest_status() -> None:
    rows = [_spine("BAT-0100", "MERGED_PENDING_RECONCILIATION"), _spine("BAT-0100", "ACTIVE")]
    assert status_width(rows) == len("MERGED_PENDING_RECONCILIATION")


@pytest.mark.parametrize("width", [-1, 0, 3, len("ACTIVE")])
def test_a_width_at_or_below_the_status_never_clips_it(width: int) -> None:
    row = _spine("BAT-0100", "ACTIVE")
    assert padded_status(row, width) == "ACTIVE"
    assert reference(row, width) == "BAT-0100 · ACTIVE"


def test_a_width_past_the_status_pads_by_exactly_the_difference() -> None:
    row = _spine("BAT-0100", "ACTIVE")
    assert padded_status(row, len("ACTIVE") + 1) == "ACTIVE "
    assert reference(row) == "BAT-0100 · ACTIVE"


def test_a_row_with_no_stored_status_pads_its_unknown_token() -> None:
    row = _spine("BAT-0100", None)
    assert row.collection is Epoch2Collection.BATCH
    word = status(row)
    assert status_width([row]) == cell_len(word)
    assert cell_len(padded_status(row, cell_len(word) + 4)) == cell_len(word) + 4
