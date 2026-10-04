"""The native route bodies draw what the tree records, for the principal the console acts as.

The frames around them already follow the packet; these are the bodies. A body cell the
epoch-2 tree already records -- a Run's instants, the Task it runs and that Task's own
state, the attempt it is, the Runs under a Track -- is now read through the projection
and drawn, and a cell no producer feeds still wears its unknown token. The Attention
register is read for one principal: what this principal may act on is ``mine``, what only
another principal may act on is counted apart and never offered.

Requirement ids proven here: CON-082, CON-084, CON-085, CON-088, CON-089 and CON-108. The
last section serves a walked epoch-2 tree over a real daemon socket and reads the facts
off the live frames.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.attention import attention_mine
from eawf.kernel.projection.compute import FACTS_FIELD, build_route_projection, row_document
from eawf.kernel.projection.registers import RegisterView, build_register_view
from eawf.kernel.projection.spine import build_spine_view
from eawf.surfaces.tui.console.attention import audience_refusal
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, needs_count
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.activity import EMPTY_NEXT, run_reason
from eawf.surfaces.tui.console.renderers.attention import (
    EMPTY_NEXT as ATTENTION_NEXT,
)
from eawf.surfaces.tui.console.renderers.attention import (
    NOT_A_WORK_LIST,
    NOTHING_NEEDS_YOU,
)
from eawf.surfaces.tui.console.renderers.backlog import EMPTY_NEXT as BACKLOG_NEXT
from eawf.surfaces.tui.console.renderers.read_model import UNKNOWN_WORD
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from tests.tui.surfaces.tui.console.test_console_live_smoke import (
    live_console,
    render_setup,
    walk_canary_isolated,
)

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
CURSOR = 41208
ROOT = "eawf://EAWF/EAWF/EAWF"
ME, OTHER = "OP-0001", "OP-0002"


def _row(kind: str, key: str, status: str, revision: int = 1, **extra: Any) -> dict[str, Any]:
    return {"urn": f"{ROOT}/{kind}/{key}", "revision": revision, "status": status, **extra}


def _action(key: str, status: str, subject: str, **extra: Any) -> dict[str, Any]:
    return {
        "urn": f"{ROOT}/pending-action/{key}",
        "revision": 1,
        "status": status,
        "kind": "operator_decision",
        "question": f"which way for {subject}?",
        "subject_ref": f"{ROOT}/{subject}",
        "requested_by": {"principal_kind": "human", "principal_id": ME},
        "created_at": "2026-09-17T11:00:00Z",
        **extra,
    }


#: A Track, a Milestone and a Batch under it, two Tasks, three Runs (two attempts at one
#: Task, one suspended and one failed), and three pending actions: one mine, one only
#: another principal may act on, one sealed.
DOCUMENT: dict[str, Any] = {
    "track": {"TRK-CORE": _row("track", "TRK-CORE", "ACTIVE", title="Core framework")},
    "milestone": {
        "MLS-0100": _row(
            "milestone",
            "MLS-0100",
            "ACTIVE",
            title="Cut the candidate",
            primary_track_ref=f"{ROOT}/track/TRK-CORE",
        )
    },
    "batch": {
        "BAT-0100": _row("batch", "BAT-0100", "ACTIVE", milestone_ref=f"{ROOT}/milestone/MLS-0100")
    },
    "task": {
        "TSK-0001": _row(
            "task",
            "TSK-0001",
            "RUNNING",
            intent="Bound the replay window",
            batch_ref=f"{ROOT}/batch/BAT-0100",
        ),
        "TSK-0002": _row("task", "TSK-0002", "PLANNED", intent="Seal the ledger"),
    },
    "run": {
        "RUN-00000001": _row(
            "run",
            "RUN-00000001",
            "COMPLETED",
            3,
            created_at="2026-09-17T09:00:00Z",
            started_at="2026-09-17T09:00:05Z",
            ended_at="2026-09-17T09:12:05Z",
            updated_at="2026-09-17T09:12:05Z",
            scope={"purpose": "implement", "task_ref": f"{ROOT}/task/TSK-0001"},
        ),
        "RUN-00000002": _row(
            "run",
            "RUN-00000002",
            "SUSPENDED",
            2,
            created_at="2026-09-17T10:00:00Z",
            started_at="2026-09-17T10:00:00Z",
            updated_at="2026-09-17T11:58:00Z",
            suspension_reason="AWAITING_PERMISSION_GRANT",
            scope={"purpose": "implement", "task_ref": f"{ROOT}/task/TSK-0001"},
        ),
        "RUN-00000003": _row(
            "run",
            "RUN-00000003",
            "FAILED",
            2,
            created_at="2026-09-17T10:30:00Z",
            updated_at="2026-09-17T10:40:00Z",
            failure={"code": "report-rejected", "message": "final report rejected"},
            scope={"purpose": "review", "task_ref": f"{ROOT}/task/TSK-0002"},
        ),
    },
    "pending_action": {
        "ACT-0001": _action("ACT-0001", "WAITING", "run/RUN-00000002"),
        "ACT-0002": _action("ACT-0002", "WAITING", "milestone/MLS-0100", assignee_ref=OTHER),
        "ACT-0003": _action("ACT-0003", "SEALED", "run/RUN-00000001"),
    },
}


def _projection(route: str, document: dict[str, Any] | None = None) -> Any:
    return build_route_projection(
        route=route,
        document=DOCUMENT if document is None else document,
        cursor=CURSOR,
        scope_id=SCOPE,
        generated_at=AT,
    )


def _attention(document: dict[str, Any] | None = None) -> RegisterView:
    """Return the Attention register over the probe tree, as its producer writes it."""
    return build_register_view(_projection("attention", document))


def _view(
    route: str,
    *,
    w: int = 120,
    conn: str = "LIVE",
    principal: str | None = ME,
    document: dict[str, Any] | None = None,
    subject: str | None = None,
    now: datetime | None = None,
    attention: RegisterView | None = None,
) -> View:
    h = dict(SIZES)[w]
    session = Session()
    session.route = route
    session.conn = conn
    session.subj_id = subject
    attention = attention if attention is not None else _attention(document)
    held: dict[str, Any] = {}
    if route in ("activity", "attention"):
        held["register"] = (
            attention if route == "attention" else build_register_view(_projection(route, document))
        )
    else:
        held["projection"] = build_spine_view(_projection(route, document))
    return View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=w,
        h=h,
        linked=True,
        principal=principal,
        now=now,
        attention=attention,
        **held,
    )


def _frame(route: str, **kwargs: Any) -> list[str]:
    view = _view(route, **kwargs)
    frame = render_route(view)
    assert len(frame) == view.h
    return frame


def _text(frame: list[str]) -> str:
    return "\n".join(frame)


def _starts(frame: list[str], prefix: str) -> str:
    return next(row for row in frame if row.startswith(prefix))


# ---------- the projection reads the facts the tree records ----------


def test_a_run_row_carries_its_instants_its_task_and_its_attempt() -> None:
    rows = {row.key: row for row in _projection("activity").rows}
    first, second = rows["RUN-00000001"].facts, rows["RUN-00000002"].facts
    assert first["started_at"] == "2026-09-17T09:00:05Z"
    assert first["ended_at"] == "2026-09-17T09:12:05Z"
    assert (first["task_title"], first["task_status"]) == ("Bound the replay window", "RUNNING")
    assert (first["batch"], first["milestone"], first["track"]) == (
        "BAT-0100",
        "MLS-0100",
        "TRK-CORE",
    )
    # two Runs of one Task are two attempts, counted in the order they were created
    assert (first["attempt"], first["attempts"]) == ("1", "2")
    assert (second["attempt"], second["attempts"]) == ("2", "2")
    assert rows["RUN-00000002"].suspension_reason == "AWAITING_PERMISSION_GRANT"


def test_a_fact_the_tree_does_not_state_is_absent_rather_than_blank() -> None:
    facts = {row.key: row.facts for row in _projection("activity").rows}["RUN-00000003"]
    assert "started_at" not in facts
    assert "ended_at" not in facts
    assert all(value.strip() for value in facts.values())


def test_a_track_and_a_milestone_count_the_runs_filed_under_them() -> None:
    rows = {row.key: row for row in _projection("scope.home").rows}
    assert rows["TRK-CORE"].facts["runs"] == "2"
    assert rows["MLS-0100"].facts["runs"] == "2"


def test_an_action_row_names_its_kind_question_assignee_and_where_its_subject_sits() -> None:
    rows = {row.key: row for row in _projection("attention").rows}
    mine, theirs = rows["ACT-0001"].facts, rows["ACT-0002"].facts
    assert (mine["kind"], mine["subject"], mine["track"]) == (
        "operator_decision",
        "RUN-00000002",
        "TRK-CORE",
    )
    assert rows["ACT-0001"].assignee_ref is None
    assert (rows["ACT-0002"].assignee_ref, theirs["milestone"]) == (OTHER, "MLS-0100")


def test_a_replay_keeps_the_facts_its_rows_were_projected_with() -> None:
    """A replay rebuilds from the held rows alone, so the other collections are gone."""
    held = _projection("activity")
    spelled = {row.key: row_document(row) for row in held.rows}
    assert all(FACTS_FIELD in stored for stored in spelled.values())
    again = _projection("activity", {"run": spelled})
    assert [row.facts for row in again.rows] == [row.facts for row in held.rows]


# ---------- CON-088: three lifecycles, three values ----------


def test_con_088_connection_run_and_task_state_are_three_separate_values() -> None:
    running = _frame("run.detail", subject="RUN-00000002")
    assert running[0].rstrip().endswith("● LIVE")
    assert _starts(running, " STATE").split()[1] == "SUSPENDED"
    assert _starts(running, " TASK").rstrip().endswith("task RUNNING")
    frame = _frame("run.detail", subject="RUN-00000001")
    assert _starts(frame, " STATE").split()[1] == "COMPLETED"
    task = _starts(frame, " TASK")
    assert "TSK-0001 Bound the replay window" in task
    assert task.rstrip().endswith("task RUNNING")


def test_con_088_a_finished_run_never_reads_as_a_completed_task() -> None:
    text = _text(_frame("run.detail", subject="RUN-00000001"))
    assert "task COMPLETED" not in text
    assert "task RUNNING" in text


def test_con_088_the_run_frame_states_what_the_tree_records_about_the_run() -> None:
    frame = _frame("run.detail", subject="RUN-00000001")
    assert _starts(frame, " STARTED").split()[1] == "09:00:05"
    assert _starts(frame, " ENDED").split()[1] == "09:12:05"
    assert "BAT-0100 · MLS-0100 · TRK-CORE" in _starts(frame, " SCOPE")
    assert "elapsed 12m" in _starts(frame, " USAGE")
    assert "attempt 1 of 2" in _starts(frame, " LINEAGE")
    # the sealed action about this Run is answered, so nothing is being asked of it
    assert "nothing · no open action names this Run" in _starts(frame, " ASKED")


def test_con_088_a_running_run_is_measured_to_now_and_says_as_of_without_a_clock() -> None:
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    live = _frame("run.detail", subject="RUN-00000002", now=now)
    assert "elapsed 2h ·" in _starts(live, " USAGE")
    assert "still running" in _starts(live, " ENDED")
    held = _frame("run.detail", subject="RUN-00000002")
    assert "as of 12:00:00" in _starts(held, " USAGE")


def test_con_088_the_run_frame_names_what_it_is_asked() -> None:
    frame = _frame("run.detail", subject="RUN-00000002")
    assert "which way for run/RUN-00000002?" in _starts(frame, " ASKED")


def test_con_088_a_fact_nothing_produces_keeps_its_unknown_token() -> None:
    frame = _frame("run.detail", subject="RUN-00000003")
    assert _starts(frame, " RUNTIME").split()[1:4] == ["runtime", "not", "recorded"]
    assert "controls are not gated on a certification" in _starts(frame, " RUNTIME")
    assert _starts(frame, " STARTED").split()[1:3] == UNKNOWN_WORD.split()


def test_the_run_frame_draws_the_runtime_tuple_and_session_the_run_records() -> None:
    document = copy.deepcopy(DOCUMENT)
    document["run"]["RUN-00000001"]["runtime_tuple"] = {
        "harness": "claude-code",
        "harness_version": "2.1.274",
        "model": "claude-sonnet-4-5",
    }
    document["run"]["RUN-00000001"]["vendor_session"] = {
        "harness": "claude-code",
        "session_digest": "vsid-0123456789abcdef0123456789abcdef",
    }
    frame = _frame("run.detail", subject="RUN-00000001", document=document)
    assert _starts(frame, " RUNTIME").split()[1:] == [
        "claude-code",
        "2.1.274",
        "·",
        "provider",
        "not",
        "recorded",
        "·",
        "claude-sonnet-4-5",
        "·",
        "session",
        "vsid-01234567",
    ]


# ---------- Activity draws each Run's Task, reason and instant ----------


def test_activity_names_each_runs_task_reason_and_instant() -> None:
    frame = _frame("activity")
    suspended = _starts(frame, "   RUN-00000002")
    assert "TSK-0001 Bound the replay" in suspended
    assert "needs permission" in suspended
    assert "11:58" in suspended
    failed = _starts(frame, "   RUN-00000003")
    assert "final report rejected" in failed


@pytest.mark.parametrize(
    ("status", "facts", "reason"),
    [
        ("RUNNING", {"purpose": "integrate"}, "integrating"),
        ("QUEUED", {}, "waiting for a slot"),
        ("SUSPENDED", {"suspension_reason": "AWAITING_OPERATOR_INPUT"}, "needs your answer"),
        ("FAILED", {"failure": "worker lost"}, "worker lost"),
        ("RUNNING", {}, UNKNOWN_WORD),
        ("SUSPENDED", {"suspension_reason": "SOMETHING_NEW"}, UNKNOWN_WORD),
    ],
)
def test_a_runs_reason_is_read_off_its_facts_or_is_unknown(
    status: str, facts: dict[str, str], reason: str
) -> None:
    row = _projection("activity").rows[0]
    waiting = facts.pop("suspension_reason", None)
    moved = row.model_copy(
        update={
            "status": row.status.model_copy(update={"value": status}),
            "facts": facts,
            "suspension_reason": waiting,
        }
    )
    assert run_reason(moved) == reason


# ---------- CON-085: every count is mine or all principals ----------


def test_con_085_the_attention_counts_are_mine_and_all_principals() -> None:
    frame = _frame("attention")
    assert frame[1].rstrip() == " 1 mine · 2 all principals"
    assert "fleet" not in frame[1]


def test_con_085_an_incomplete_read_labels_the_second_count_known() -> None:
    frame = _frame("attention", conn="GAP DETECTED")
    assert frame[1].startswith(" 1 mine · 2 known")


def test_con_085_home_names_the_principal_under_more_than_one() -> None:
    frame = _frame("scope.home")
    assert frame[3].startswith(f" PRINCIPAL  you are {ME} · operator class · 1 action is")
    head = next(row for row in frame if "MILESTONES" in row)
    assert re.search(r"RUNS\s+MINE\s+ALL PRINCIPALS\s+PROGRESS", head)
    # a Track is a container, so its row never carries the caret
    track = _starts(frame, "   TRK-CORE")
    # the Run count, then this principal's one open action, then both principals' two
    assert re.search(r"Core framework\s+2\s+!1\s+!2\s+0 of 1 milestone", track)


def test_con_085_home_under_one_principal_keeps_its_one_attention_column() -> None:
    document = {**DOCUMENT, "pending_action": {"ACT-0001": DOCUMENT["pending_action"]["ACT-0001"]}}
    frame = _frame("scope.home", document=document)
    assert not any(row.startswith(" PRINCIPAL") for row in frame)
    head = next(row for row in frame if "MILESTONES" in row)
    assert re.search(r"RUNS\s+ATTENTION\s+PROGRESS", head)


def test_con_085_the_same_mine_count_reconciles_across_home_attention_and_header() -> None:
    view = _view("attention")
    assert needs_count(view) == 1
    assert attention_mine(_attention(), principal=ME).value == "1"
    assert _text(render_route(view)).count("NEEDS YOU") == 1
    assert "1 action is yours" in _text(_frame("scope.home"))


def test_con_085_a_console_acting_as_nobody_has_no_mine_count() -> None:
    assert attention_mine(_attention(), principal=None).state.value == "unknown"
    frame = _frame("attention", principal=None)
    assert frame[1].startswith(f" {UNKNOWN_WORD} mine · 2 all principals")


# ---------- CON-084 and CON-108: three empty frames, three questions ----------


def _no_open_actions() -> dict[str, Any]:
    return {**DOCUMENT, "pending_action": {"ACT-0003": DOCUMENT["pending_action"]["ACT-0003"]}}


def test_con_084_the_empty_attention_route_names_its_revision_and_the_next_move() -> None:
    frame = _frame("attention", document=_no_open_actions())
    assert frame[1].startswith(f" {NOTHING_NEEDS_YOU}")
    nothing = _starts(frame, " NOTHING YET")
    assert "revision 41,208" in nothing
    # the counted buckets stay on the rail, each reading 0; the one no record feeds is not
    # drawn, and none of them is drawn as a body row
    buckets = [row for row in frame if re.search(r"│ [ ▸][a-z][a-z ]+\s+(0|∅)\s*$", row)]
    assert len(buckets) == 7
    assert not [row for row in frame if re.match(r"^   [a-z ]+\s+(0|∅)\s*$", row)]
    assert re.search(r"needs operator\s+0", _text(frame))
    assert ATTENTION_NEXT in _starts(frame, " WHAT TO DO")


def test_con_084_the_empty_attention_route_withdraws_its_verbs_and_the_badge() -> None:
    frame = _frame("attention", document=_no_open_actions())
    assert "NEEDS YOU" not in frame[0]
    assert "!0" not in frame[0]
    assert "a answer" not in frame[-1]


def test_con_084_home_says_nothing_needs_you_in_its_own_words() -> None:
    frame = _frame("scope.home", document=_no_open_actions())
    at = frame.index(_starts(frame, " ATTENTION   nothing needs you"))
    assert frame[at + 1].strip() == "nothing is waiting on you · runs continue without you"


def test_con_084_a_withheld_register_is_never_drawn_as_empty() -> None:
    # a register whose collection is not read yet states no count, never an empty queue
    withheld = dataclasses.replace(_attention(), rows=(), counts={}, withheld=("pending_action",))
    frame = _frame("attention", attention=withheld)
    text = _text(frame)
    assert NOTHING_NEEDS_YOU not in text
    assert "NOTHING YET" not in text
    assert not any(re.search(r"needs operator\s+0", row) for row in frame)


def test_con_108_an_empty_activity_route_names_its_revision_and_next_move() -> None:
    text = _text(_frame("activity", document={"run": {}}))
    assert "this scope holds no record of a Run at revision 41,208" in text
    assert EMPTY_NEXT in text


def test_con_108_a_filter_that_hides_every_run_is_not_an_empty_scope() -> None:
    view = _view("activity")
    view.session.filters["activity"] = "no such run"
    text = _text(render_route(view))
    assert "nothing matches the filter · 3 runs hidden" in text
    assert "NOTHING RUNS" not in text


def test_con_108_a_gap_is_not_an_empty_scope() -> None:
    text = _text(_frame("activity", document={"run": {}}, conn="GAP DETECTED"))
    assert "this read cannot vouch for every Run" in text
    assert "NOTHING RUNS" not in text


def test_con_108_the_three_empty_frames_answer_three_questions() -> None:
    attention = _text(_frame("attention", document=_no_open_actions()))
    activity = _text(_frame("activity", document={"run": {}}))
    assert "NOTHING YET" in attention and "NOTHING YET" not in activity
    assert "NOTHING RUNS" in activity and "NOTHING RUNS" not in attention


# ---------- CON-126 in the frame: an action another principal holds ----------


def test_the_attention_route_groups_open_rows_by_bucket_and_lists_no_sealed_row() -> None:
    frame = _frame("attention")
    heads = [row.split("  ")[0].strip() for row in frame if re.match(r"^ [A-Z][A-Z ]+  \d", row)]
    assert heads == ["NEEDS OPERATOR"]
    listed = [m.group(1) for row in frame if (m := re.match(r"^ [ ▸] (ACT-\d{4})", row))]
    assert listed == ["ACT-0001", "ACT-0002"]
    # the row another principal holds names its owner on the detail line, not a section
    assert NOT_A_WORK_LIST in _text(frame)


def test_an_action_another_principal_holds_is_refused_naming_who_holds_it() -> None:
    row = {r.key: r for r in _projection("attention").rows}["ACT-0002"]
    refusal = audience_refusal(row.assignee_ref, ME)
    assert refusal.startswith(f"{OTHER} only")
    assert f"--actor {OTHER}" in refusal
    assert audience_refusal(row.assignee_ref, OTHER) == ""


# ---------- CON-082: the reads contract in the body ----------


def test_con_082_disconnected_states_revision_age_known_and_cause() -> None:
    now = AT + timedelta(minutes=4, seconds=8)
    frame = _frame("activity", conn="DISCONNECTED", now=now)
    assert frame[1].startswith(" 3 runs · known")
    assert _starts(frame, " ATTACHED").rstrip().endswith("revision 41,208 · 4m 08s old")
    assert "the daemon cannot be reached" in frame[1]


def test_con_082_disconnected_attention_labels_its_count_known_and_offers_no_verb() -> None:
    frame = _frame("attention", conn="DISCONNECTED")
    assert frame[1].startswith(" 1 mine · 2 known")
    assert "the daemon cannot be reached" in frame[1]
    for verb in ("a answer", "x deny", "z snooze", "v resolve"):
        assert verb not in frame[-1]


def test_con_082_a_live_frame_carries_no_reads_rows() -> None:
    text = _text(_frame("activity"))
    assert " ATTACHED" not in text
    assert "cannot be reached" not in text


# ---------- CON-089: a wider frame adds disclosure, never a different answer ----------


def _answers(frame: list[str]) -> list[tuple[str, ...]]:
    """Return each Run row's answer fields: its id, its Task, its state and its reason."""
    found = []
    for row in frame:
        match = re.match(r"^ [ ▸] (RUN-\d{8}) (TSK-\d{4})\b.*?\b([A-Z_]{5,})\s", row)
        if match:
            found.append(match.groups())
    return found


@pytest.mark.parametrize("route", ["activity", "attention", "run.detail", "scope.home"])
def test_con_089_every_width_gives_the_same_summary_line(route: str) -> None:
    subject = "RUN-00000002" if route == "run.detail" else None
    lines = [_frame(route, w=w, subject=subject)[1].rstrip() for w, _h in SIZES]
    assert len(set(lines)) == 1


def test_con_089_activity_answers_the_same_at_every_width() -> None:
    answers = [_answers(_frame("activity", w=w)) for w, _h in SIZES]
    assert answers[0]
    assert answers[0] == answers[1] == answers[2]


@pytest.mark.parametrize("label", [" STATE", " STARTED", " ENDED", " LINEAGE"])
def test_con_089_the_run_frame_answers_the_same_at_every_width(label: str) -> None:
    rows = {
        _starts(_frame("run.detail", w=w, subject="RUN-00000001"), label).rstrip()
        for w, _h in SIZES
    }
    assert len(rows) == 1


def test_con_089_the_attention_mine_count_does_not_change_with_width() -> None:
    counts = {_frame("attention", w=w)[1].split()[0] for w, _h in SIZES}
    assert counts == {"1"}


# ---------- the live path: a walked tree over a real daemon socket ----------


def test_the_live_bodies_read_the_walked_trees_own_facts(tmp_path: Path) -> None:
    """The facts a live frame draws are the ones the served projection read off the tree."""
    walk, runtime_root = walk_canary_isolated(tmp_path)

    async def body() -> tuple[dict[str, str], Any, Any]:
        frames: dict[str, str] = {}
        async with (
            live_console(walk.canary.root, runtime_root) as (app, seam),
            app.run_test(size=SIZES[1]) as pilot,
        ):
            for route in ("activity", "scope.home"):
                frames[route] = await render_setup(app, pilot, SessionSetup(route=route, size=1))
            activity, home = seam.projection_for("activity"), seam.projection_for("scope.home")
            run = activity.rows[0].key if activity is not None and activity.rows else None
            frames["run.detail"] = await render_setup(
                app, pilot, SessionSetup(route="run.detail", subjId=run, size=1)
            )
        return frames, activity, home

    frames, activity, home = asyncio.run(body())
    assert activity is not None and activity.rows, "the walked tree dispatches a Run"
    run = activity.rows[0]
    row = next(line for line in frames["activity"].split("\n") if run.key in line)
    if "updated_at" in run.facts:
        stamp = datetime.fromisoformat(run.facts["updated_at"]).astimezone(UTC)
        assert f"{stamp:%H:%M}" in row, row
    if "task_title" in run.facts:
        assert run.facts["task_title"].split()[0] in row, row
    detail = frames["run.detail"].split("\n")
    started = _starts(detail, " STARTED")
    if "started_at" in run.facts:
        stamp = datetime.fromisoformat(run.facts["started_at"]).astimezone(UTC)
        assert f"{stamp:%H:%M:%S}" in started, started
    else:
        assert UNKNOWN_WORD in started
    assert f"attempt {run.facts['attempt']} of {run.facts['attempts']}" in _starts(
        detail, " LINEAGE"
    )
    assert home is not None
    track = next(r for r in home.rows if r.facts.get("runs"))
    assert re.search(rf"\b{track.key}\b.*\s{track.facts['runs']}\s", frames["scope.home"])


# ---------- CON-108: the empty backlog is a frame of its own ----------


def _backlog_document(*statuses: str) -> dict[str, Any]:
    return {
        "task": {
            f"TSK-01{i:02d}": _row(
                "task",
                f"TSK-01{i:02d}",
                status,
                intent=f"Queued task {i}",
                due_scope=f"{ROOT}/milestone/MLS-0100",
                updated_at="2026-09-12T10:00:00Z",
            )
            for i, status in enumerate(statuses)
        }
    }


def test_the_backlog_lists_its_drafts_and_its_deferred_tasks_with_their_facts() -> None:
    frame = _frame("backlog", document=_backlog_document("DRAFT", "DEFERRED", "PLANNED"))
    assert frame[1].rstrip() == " 1 drafts · 1 deferred"
    text = _text(frame)
    draft = _starts(frame, " ▸ TSK-0100")
    assert "Queued task 0" in draft and "MLS-0100" in draft
    deferred = next(row for row in frame if "TSK-0101" in row)
    assert "Sep 12" in deferred
    assert "TSK-0102" not in text
    assert not re.search(r"^\s+ROW\s+KIND\s+STATUS", text, re.MULTILINE)


def test_con_108_an_empty_backlog_names_its_revision_and_the_next_move() -> None:
    text = _text(_frame("backlog", document=_backlog_document("PLANNED", "DROPPED")))
    assert "NOTHING QUEUED  no draft and no deferred Task at revision 41,208" in text
    assert BACKLOG_NEXT in text


def test_con_108_a_backlog_read_that_cannot_vouch_is_not_called_empty() -> None:
    text = _text(_frame("backlog", document=_backlog_document(), conn="GAP DETECTED"))
    assert "NOTHING QUEUED" not in text
    assert "this read cannot vouch for every Task" in text


def test_con_108_the_empty_backlog_is_not_the_empty_attention_or_activity_frame() -> None:
    backlog = _text(_frame("backlog", document=_backlog_document()))
    for other in ("NOTHING YET", "NOTHING RUNS", NOTHING_NEEDS_YOU):
        assert other not in backlog
