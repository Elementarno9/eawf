"""The spine frames: each draws its own subject and its own children.

The probe tree has two Tracks, so the
home tree can show one expanded and one collapsed; two Batches under one Milestone, so a
Batch frame that listed its siblings would show the other; and Tasks and Runs filed under
them by parent key, so a detail frame's list can be told apart from its register.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, date, datetime
from typing import Any

import pytest

from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, build_route_projection
from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.projection.transcript import RUN_NOT_ENDED
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.paint import Part, paint
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.history import UNREAD
from eawf.surfaces.tui.console.renderers.run_detail import NO_EVENTS, TIMELINE_LEGEND, timeline_head
from eawf.surfaces.tui.console.renderers.timeline import (
    DATED,
    DONE,
    NO_DATE,
    week_header,
    week_offset,
)
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.workflow.projection.acceptance import build_acceptance_view
from tests.tui.surfaces.tui.console.overlay_support import Host

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
ROOT = "eawf://EAWF/EAWF/EAWF"
LONG = "Cut the candidate with every console frame drawn to the packet and nothing borrowed"


def _row(kind: str, key: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"urn": f"{ROOT}/{kind}/{key}", "revision": 1, "status": status, **extra}


def _under(kind: str, key: str) -> str:
    return f"{ROOT}/{kind}/{key}"


DOCUMENT: dict[str, Any] = {
    "track": {
        "TRK-CORE": _row("track", "TRK-CORE", "ACTIVE", title="Core framework"),
        "TRK-DOCS": _row("track", "TRK-DOCS", "ACTIVE", title="Documentation"),
    },
    "milestone": {
        "MLS-0100": _row(
            "milestone",
            "MLS-0100",
            "ACTIVE",
            title=LONG,
            primary_track_ref=_under("track", "TRK-CORE"),
        ),
        "MLS-0200": _row(
            "milestone",
            "MLS-0200",
            "PLANNED",
            title="Write the guide",
            primary_track_ref=_under("track", "TRK-DOCS"),
        ),
    },
    "batch": {
        "BAT-0100": _row(
            "batch", "BAT-0100", "ACTIVE", milestone_ref=_under("milestone", "MLS-0100")
        ),
        "BAT-0101": _row(
            "batch", "BAT-0101", "PLANNED", milestone_ref=_under("milestone", "MLS-0100")
        ),
    },
    "task": {
        "TSK-0001": _row(
            "task",
            "TSK-0001",
            "RUNNING",
            intent="Bound the replay",
            batch_ref=_under("batch", "BAT-0100"),
        ),
        "TSK-0002": _row(
            "task",
            "TSK-0002",
            "COMPLETED",
            intent="Seal the ledger",
            batch_ref=_under("batch", "BAT-0100"),
        ),
        "TSK-0003": _row(
            "task",
            "TSK-0003",
            "PLANNED",
            intent="Other batch",
            batch_ref=_under("batch", "BAT-0101"),
        ),
    },
    "run": {
        "RUN-00000001": _row(
            "run",
            "RUN-00000001",
            "RUNNING",
            created_at="2026-09-17T09:00:00Z",
            started_at="2026-09-17T09:00:05Z",
            scope={"purpose": "implement", "task_ref": _under("task", "TSK-0001")},
        ),
        "RUN-00000002": _row(
            "run",
            "RUN-00000002",
            "COMPLETED",
            created_at="2026-09-17T08:00:00Z",
            scope={"purpose": "implement", "task_ref": _under("task", "TSK-0002")},
        ),
    },
}


def _projection(route: str, document: dict[str, Any] | None = None) -> Any:
    return build_route_projection(
        route=route,
        document=DOCUMENT if document is None else document,
        cursor=41208,
        scope_id=SCOPE,
        generated_at=AT,
    )


def _model(route: str, document: dict[str, Any] | None = None) -> Any:
    if route == "milestone":
        return build_acceptance_view(_projection(route, document))
    return build_spine_view(_projection("roadmap" if route == "timeline" else route, document))


def _session(route: str, subject: str | None) -> Session:
    session = Session()
    session.route = route
    session.subj_id = subject
    return session


def _view(session: Session, model: Any, w: int) -> View:
    h = dict(SIZES)[w]
    return View(
        session=session, fixture=Fixture.from_chrome(load_chrome()), w=w, h=h, projection=model
    )


def _frame(
    route: str,
    subject: str | None = None,
    *,
    w: int = 120,
    session: Session | None = None,
    document: dict[str, Any] | None = None,
) -> list[str]:
    session = session or _session(route, subject)
    frame = render_route(_view(session, _model(route, document), w))
    assert len(frame) == dict(SIZES)[w]
    return frame


def _press(session: Session, route: str, *keys: str, w: int = 120) -> list[str]:
    """Draw the frame, dispatch each key the way the app does, and return the last frame."""
    model = _model(route)
    for key in keys:
        compose_frame(_view(session, model, w))
        ctx = Ctx(
            session=session,
            fixture=Fixture.from_chrome(load_chrome()),
            host=Host(),
            w=w,
            h=dict(SIZES)[w],
            projection=model,
        )
        dispatch(ctx, key, False)
    if session.route != route:
        return []
    return compose_frame(_view(session, model, w))


def _starts(frame: list[str], label: str) -> str:
    return next(row for row in frame if row.lstrip().startswith(label))


def _caret(frame: list[str]) -> str:
    return next(row for row in frame[3:-1] if "▸ " in row)


# ---------- the home tree's columns and its grouping ----------


def test_home_keeps_the_packet_columns_at_80() -> None:
    frame = _frame("scope.home", w=80)
    head = next(row for row in frame if row.lstrip().startswith("MILESTONES"))
    assert (head.index("RUNS"), head.index("ATTENTION"), head.index("PROGRESS")) == (33, 40, 52)


@pytest.mark.parametrize("w", [120, 160])
def test_home_caps_the_name_column_near_48_and_keeps_the_counts_beside_it(w: int) -> None:
    frame = _frame("scope.home", w=w)
    head = next(row for row in frame if row.lstrip().startswith("MILESTONES"))
    assert head.index("RUNS") == 51, "the name column stops at 48 cells, whatever the width"
    leaf = next(row for row in frame if "MLS-0100" in row)
    # a title longer than its column is clipped with an ellipsis, never run into the status
    assert "…" in leaf and re.search(r"…\s{2,}ACTIVE", leaf)
    assert "done" not in leaf, "a Milestone row carries no progress cell"


def test_only_the_track_the_cursor_is_in_expands() -> None:
    frame = _frame("scope.home")
    text = "\n".join(frame)
    assert "MLS-0100" in text and "MLS-0200" not in text
    assert "TRK-DOCS Documentation" in text


def test_the_cursor_stepping_onto_a_collapsed_track_expands_it() -> None:
    session = _session("scope.home", None)
    frame = _press(session, "scope.home", "ArrowDown")
    assert "MLS-0200" in "\n".join(frame)
    assert "MLS-0100" not in "\n".join(frame)
    # a Track is a container: reading down lands on its first Milestone, never the Track
    assert session.sel_id == "MLS-0200"


def test_home_draws_no_window_row_while_every_row_is_on_screen() -> None:
    assert not any(row.startswith(" WINDOW") for row in _frame("scope.home", w=80))


# ---------- Batch and Task frames list their own children ----------


def test_the_batch_frame_lists_its_own_tasks_and_no_sibling() -> None:
    frame = _frame("batch.detail", "BAT-0100")
    text = "\n".join(frame)
    assert frame[0].startswith(" Eä ▸ EAWF ▸ MLS-0100 ▸ BAT-0100")
    assert frame[1].startswith(" Batch BAT-0100 · ACTIVE")
    assert "BAT-0101" not in text, "a sibling Batch is not the frame's subject"
    assert "TSK-0003" not in text, "a Task of another Batch is not listed"
    assert _starts(frame, "TASKS").startswith(" TASKS      ▸ TSK-0001 · RUNNING  Bound the replay")
    assert "TSK-0002 · COMPLETED" in text
    assert "Seal the ledger" not in text, "only the row under the caret carries its title"
    assert "2 tasks · 1 completed" in _starts(frame, "COUNT")
    assert not any(row.startswith((" REGIONS", "   ROW ")) for row in frame)


def test_the_batch_frame_drills_the_task_under_its_caret() -> None:
    session = _session("batch.detail", "BAT-0100")
    _press(session, "batch.detail", "ArrowDown", "Enter")
    assert (session.route, session.subj_id) == ("task.detail", "TSK-0002")


def test_a_batch_with_no_task_says_so_and_keeps_the_selection_on_itself() -> None:
    session = _session("batch.detail", "BAT-0101")
    document = {**DOCUMENT, "task": {}}
    frame = _frame("batch.detail", session=session, document=document)
    assert "∅ no Task is filed under this Batch" in _starts(frame, "TASKS")
    assert session.sel_id == "BAT-0101"


def test_the_task_frame_lists_its_own_runs() -> None:
    frame = _frame("task.detail", "TSK-0001")
    text = "\n".join(frame)
    assert frame[0].startswith(" Eä ▸ EAWF ▸ BAT-0100 ▸ TSK-0001")
    assert frame[1].startswith(" Task TSK-0001 Bound the replay · RUNNING")
    assert "RUN-00000001 · RUNNING · started 09:00:05" in _starts(frame, "RUNS")
    assert "RUN-00000002" not in text, "a Run of another Task is not listed"
    assert "TSK-0002" not in text
    assert _starts(frame, "BATCH").rstrip().endswith("BAT-0100")


@pytest.mark.parametrize("route", ["batch.detail", "task.detail", "track"])
def test_a_detail_route_binds_the_collection_it_lists(route: str) -> None:
    child = {
        "batch.detail": Epoch2Collection.TASK,
        "task.detail": Epoch2Collection.RUN,
        "track": Epoch2Collection.MILESTONE,
    }[route]
    assert child in ROUTE_COLLECTIONS[route]


def test_a_long_task_list_windows_and_says_what_it_hides() -> None:
    tasks = {
        f"TSK-{n:04d}": _row(
            "task", f"TSK-{n:04d}", "PLANNED", batch_ref=_under("batch", "BAT-0100")
        )
        for n in range(40)
    }
    frame = _frame("batch.detail", "BAT-0100", w=80, document={**DOCUMENT, "task": tasks})
    assert len(frame) == 24
    assert any(re.search(r"\b1–\d+ of 40\b", row) for row in frame)  # noqa: RUF001


# ---------- the Milestone frame is about its subject ----------


def test_the_milestone_frame_lists_its_batches_under_its_track() -> None:
    frame = _frame("milestone", "MLS-0100")
    text = "\n".join(frame)
    assert frame[0].startswith(" Eä ▸ EAWF ▸ TRK-CORE ▸ MLS-0100")
    assert frame[1].startswith(" Milestone MLS-0100 Cut the candidate")
    assert "ACTIVE" in frame[1] and "cursor" not in frame[1]
    assert _starts(frame, "TRACK").rstrip().endswith("TRK-CORE")
    assert _starts(frame, "BATCHES").startswith(" BATCHES    ▸ BAT-0100 · ACTIVE")
    assert "MLS-0200" not in text, "another Milestone is not listed"
    assert "2 batches · 0 completed" in _starts(frame, "BUILT")


def test_tab_moves_the_visible_section_focus() -> None:
    session = _session("milestone", "MLS-0100")
    before = _frame("milestone", session=session)
    assert "[glance]" in "\n".join(before)
    after = _press(session, "milestone", "Tab")
    assert "[try]" in "\n".join(after)
    assert any(row.lstrip().startswith("TRY") for row in after)


# ---------- the Track frame's three groups ----------


def test_the_track_frame_lists_its_own_milestones_in_the_focused_group() -> None:
    session = _session("track", "TRK-CORE")
    frame = _frame("track", session=session)
    text = "\n".join(frame)
    assert frame[1].startswith(" Track TRK-CORE Core framework · ACTIVE")
    assert "MLS-0200" not in text, "a Milestone of another Track is not listed"
    assert re.search(r"MLS-0100 Cut the candidate.*…\s+ACTIVE\s+2\s", _caret(frame))
    for group in ("MILESTONES", "CAMPAIGNS", "SHAPED QUEUE"):
        assert any(row.lstrip().startswith(group) for row in frame), group
    assert session.record_nav == ["MLS-0100"]


def test_tab_moves_the_focus_and_enter_drills_the_focused_group() -> None:
    session = _session("track", "TRK-CORE")
    after = _press(session, "track", "Tab")
    assert session.track_group == "CAMPAIGNS"
    assert not any("▸ MLS-0100" in row for row in after), "the receded group has no caret"
    _press(session, "track", "Enter")
    assert session.route == "track", "an empty group opens nothing, and no fixture record"
    session.track_group = "MILESTONES"
    _press(session, "track", "Enter")
    assert (session.route, session.subj_id) == ("milestone", "MLS-0100")


# ---------- the roadmap lane chart ----------


def test_the_timeline_draws_one_lane_per_track_over_the_weeks_around_now() -> None:
    frame = _frame("timeline", w=160)
    weeks = next(row for row in frame if "│" in row and "W38" in row)
    assert weeks.rstrip() == week_header(AT, 12).rstrip()
    lanes = [row for row in frame if row.startswith(("▸ TRK-", "  TRK-"))]
    assert [lane[2:10] for lane in lanes] == ["TRK-CORE", "TRK-DOCS"]
    assert not any("┼" in lane for lane in lanes), "the keyline crosses a lane as a plain line"
    assert lanes[0][weeks.index("│")] == "│", "the keyline sits under now"
    assert not any("ROW " in row for row in frame), "the chart is not the record table"


def test_every_undated_milestone_lands_in_the_undated_region() -> None:
    frame = _frame("timeline", w=160)
    assert _starts(frame, "UNDATED").rstrip().endswith("2 milestones with no date proposed")
    for key in ("MLS-0100", "MLS-0200"):
        row = next(r for r in frame if key in r)
        assert row.rstrip().endswith(NO_DATE)
    assert "∅ no release is recorded" in _starts(frame, "RELEASES")
    assert "0 of 2 milestones dated" in frame[1]


def test_enter_on_a_lane_with_no_dated_marker_opens_nothing() -> None:
    session = _session("timeline", None)
    _press(session, "timeline", "Enter")
    assert session.route == "timeline" and session.overlay is None


def test_an_undated_milestone_opens_on_enter() -> None:
    session = _session("timeline", None)
    _press(session, "timeline", "Tab", "Enter")
    assert (session.route, session.subj_id) == ("milestone", "MLS-0100")


def _dated_document() -> dict[str, Any]:
    """Return the probe tree with Milestones dated in, before, after and beyond the weeks."""
    document = copy.deepcopy(DOCUMENT)
    milestones = document["milestone"]
    milestones["MLS-0100"]["target_date"] = "2026-09-18"
    core, docs = _under("track", "TRK-CORE"), _under("track", "TRK-DOCS")
    for key, status, day, track in (
        ("MLS-0300", "COMPLETED", "2026-09-03", core),
        ("MLS-0400", "PLANNED", "2026-11-06", docs),
        ("MLS-0500", "PLANNED", "2026-07-01", docs),
        ("MLS-0600", "CANCELLED", "2026-10-01", docs),
    ):
        milestones[key] = _row(
            "milestone", key, status, title=f"Dated {key}", primary_track_ref=track, target_date=day
        )
    return document


def _lane_rows(frame: list[str], key: str) -> tuple[str, str]:
    at = next(i for i, row in enumerate(frame) if row[2:].startswith(f"{key} "))
    return frame[at], frame[at + 1]


def test_a_dated_milestone_is_a_marker_in_the_week_of_its_date() -> None:
    frame = _frame("timeline", w=160, document=_dated_document())
    weeks = next(row for row in frame if "│" in row and "W38" in row)
    lane, labels = _lane_rows(frame, "TRK-CORE")
    assert lane[weeks.index("W38") + 1] == DATED, "MLS-0100 sits under the week now falls in"
    assert lane[weeks.index("│")] == "│", "the now keyline survives the markers"
    assert labels[weeks.index("W38") + 1 :].startswith("MLS-0100"), "the key sits under it"
    assert labels.rstrip().endswith("2 dated · 0 undated below")


def test_a_closed_milestone_is_drawn_done_whichever_way_it_closed() -> None:
    frame = _frame("timeline", w=160, document=_dated_document())
    weeks = next(row for row in frame if "│" in row and "W38" in row)
    core, _ = _lane_rows(frame, "TRK-CORE")
    docs, _ = _lane_rows(frame, "TRK-DOCS")
    assert core[weeks.index("W36") + 1] == DONE, "a COMPLETED Milestone is done"
    assert docs[weeks.index("W40") + 1] == DONE, "a CANCELLED Milestone is done too"
    assert DONE in frame[-1] or any(f"{DONE} done" in row for row in frame), "the legend keys it"


def test_a_date_beyond_the_drawn_weeks_is_named_at_the_lane_end() -> None:
    frame = _frame("timeline", w=160, document=_dated_document())
    docs, labels = _lane_rows(frame, "TRK-DOCS")
    assert docs.rstrip().endswith("◂ W27 ▸ W45"), "an early and a late date both say so"
    assert labels.rstrip().endswith("3 dated · 1 undated below")


def test_only_undated_milestones_land_in_the_undated_region() -> None:
    frame = _frame("timeline", w=160, document=_dated_document())
    assert _starts(frame, "UNDATED").rstrip().endswith("1 milestone with no date proposed")
    undated = [row for row in frame if row.rstrip().endswith(NO_DATE)]
    assert [row.split()[0] for row in undated] == ["MLS-0200"]
    assert "5 of 6 milestones dated" in frame[1]


def test_two_milestones_in_one_week_share_a_marker_and_count_on_its_label() -> None:
    document = _dated_document()
    document["milestone"]["MLS-0300"]["target_date"] = "2026-09-17"
    frame = _frame("timeline", w=160, document=document)
    weeks = next(row for row in frame if "│" in row and "W38" in row)
    lane, labels = _lane_rows(frame, "TRK-CORE")
    assert lane[weeks.index("W38") + 1] == DATED, "an open Milestone keeps the cell open"
    assert labels[weeks.index("W38") + 1 :].startswith("MLS-0300+1")


@pytest.mark.parametrize(
    ("day", "offset"),
    [
        ("2026-09-14", 0),
        ("2026-09-20", 0),
        ("2026-09-13", -1),
        ("2026-09-21", 1),
        ("2027-01-04", 16),
    ],
)
def test_week_offset_counts_iso_weeks_from_now(day: str, offset: int) -> None:
    assert week_offset(date.fromisoformat(day), AT) == offset


# ---------- the Run frame opens on its timeline ----------


def test_the_run_frame_draws_the_timeline_pane_first_with_an_honest_empty_state() -> None:
    frame = _frame("run.detail", "RUN-00000001")
    assert frame[3] == timeline_head(120)
    assert frame[3].endswith(f"{TIMELINE_LEGEND} ")
    assert frame[4].strip() == NO_EVENTS
    labels = [row.split()[0] for row in frame[6:] if row.startswith(" ") and row[1].isupper()]
    order = [
        label
        for label in labels
        if label in {"STATE", "TASK", "SCOPE", "USAGE", "CONTROLS", "LINEAGE"}
    ]
    assert order == ["STATE", "TASK", "SCOPE", "USAGE", "CONTROLS", "LINEAGE"]
    assert "Enter transcript" in frame[-1]
    assert not any(re.match(r"^\s+TIMELINE\s+EVENT\s+DETAIL", row) for row in frame)


def test_enter_opens_the_runs_transcript() -> None:
    session = _session("run.detail", "RUN-00000001")
    _press(session, "run.detail", "Enter")
    assert (session.route, session.subj_id) == ("transcript", "RUN-00000001")


# ---------- the transcript agrees with its Run and speaks operator prose ----------


def test_the_outcome_reason_is_operator_prose_not_a_requirement_code() -> None:
    assert not re.search(r"\b[A-Z]{2,5}-\d{2,4}\b", RUN_NOT_ENDED)


def test_the_transcript_states_the_runs_own_status_and_climbs_through_it() -> None:
    from eawf.kernel.projection.transcript import build_transcript_view

    model = build_transcript_view(_projection("transcript"))
    session = _session("transcript", "RUN-00000001")
    frame = render_route(_view(session, model, 120))
    assert frame[0].startswith(" Eä ▸ EAWF ▸ RUN-00000001 ▸ Transcript")
    assert "RUNNING · no event recorded yet" in frame[1]
    assert "nothing running" not in frame[1]


# ---------- History is the fact ledger ----------


@pytest.mark.parametrize("w", [80, 120, 160])
def test_history_draws_the_ledger_columns_and_no_record_list(w: int) -> None:
    frame = _frame("history", w=w)
    head = next(row for row in frame if row.lstrip().startswith("FACT"))
    assert re.match(r"^\s+FACT\s+REVISION\s+SOURCE\s+WHEN$", head.rstrip())
    assert frame[1].startswith(" what changed, from what source, at which revision")
    assert any(UNREAD in row for row in frame)
    for key in ("TRK-CORE", "MLS-0100", "BAT-0100", "TSK-0001", "RUN-00000001"):
        assert not any(key in row for row in frame[3:]), key


def test_history_docks_the_source_pane_at_the_foot() -> None:
    frame = _frame("history", w=80)
    assert frame[-2].startswith(" SOURCE")
    assert frame[-3].startswith("─")
    assert frame[-1].split() == ["Esc", "back"]


def test_enter_on_the_held_ledger_opens_no_prototype_card() -> None:
    session = _session("history", None)
    _press(session, "history", "Enter")
    assert session.overlay is None


# ---------- the cursor row, the Run's groups and the caret's slot ----------


def _grounded(frame: list[str]) -> list[int]:
    return [
        i
        for i, row in enumerate(frame[1:-1], start=1)
        if any(stroke.ground == "cursor" for stroke in paint(row, Part.BODY))
    ]


def test_the_milestone_frame_grounds_its_cursor_batch_and_not_its_section_strip() -> None:
    session = _session("milestone", "MLS-0100")
    frame = _frame("milestone", session=session)
    assert [frame[i].lstrip()[:7] for i in _grounded(frame)] == ["BATCHES"]
    strip = next(row for row in frame if "[glance]" in row)
    assert "▸" not in strip, "the section strip marks a section, not the cursor"
    after = _press(session, "milestone", "ArrowDown")
    assert [after[i].strip()[:10] for i in _grounded(after)] == ["▸ BAT-0101"]


def test_the_batch_frame_grounds_its_cursor_task() -> None:
    frame = _frame("batch.detail", "BAT-0100")
    grounded = _grounded(frame)
    assert len(grounded) == 1 and "TSK-0001" in frame[grounded[0]]


def test_the_run_frame_rules_off_usage_and_controls_as_their_own_groups() -> None:
    frame = _frame("run.detail", "RUN-00000001")
    usage = next(i for i, row in enumerate(frame) if row.startswith(" USAGE"))
    controls = next(i for i, row in enumerate(frame) if row.startswith(" CONTROLS"))
    assert set(frame[usage - 1]) == {"─"}
    assert set(frame[controls - 1]) == {"─"}
    assert frame[controls + 1].startswith(" LINEAGE"), "controls and lineage are one group"


@pytest.mark.parametrize("w", [80, 120, 160])
def test_the_run_frame_keeps_every_fact_at_each_size(w: int) -> None:
    for run in ("RUN-00000001", "RUN-00000002"):
        text = "\n".join(_frame("run.detail", run, w=w))
        for label in (" USAGE", " CONTROLS", " LINEAGE"):
            assert label in text, (run, w, label)


def test_a_timeline_lane_keeps_a_space_between_its_caret_and_its_key() -> None:
    frame = _frame("timeline", w=160)
    lane = next(row for row in frame if "TRK-CORE" in row and "─" in row)
    assert lane.startswith("▸ TRK-CORE ")
