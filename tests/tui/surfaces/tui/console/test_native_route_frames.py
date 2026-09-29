"""The native route frames draw the packet layouts from a read model, not a generic table.

With a live read model every route used to hand its rows to one ``ROW KIND STATUS`` table.
Each route now draws the layout its packet row states -- its crumb, the one summary line,
its sections and their column heads, its rail where the registry declares one, its docked
readout and its keybar -- from the read model the daemon served. Where a producer has not
shipped, the cell wears the truth token with its word rather than a blank or a zero.

Requirement ids proven here: UI-020, UI-021, UI-022, UI-023, UI-045, UI-046, UI-047,
UI-048, UI-049, UI-063, UI-064, UI-065, UI-066, UI-067 and UI-073. The layout of the four
spine and register journeys the console opens on -- home, activity, attention and the Run
frame -- is held here too, and the last section renders them live over a served tree.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.connection import (
    ReconnectDisposition,
    ReconnectNegotiation,
    ReplayNote,
    replay_note,
)
from eawf.kernel.projection.integration import build_integration_view
from eawf.kernel.projection.operations import (
    DISPATCH_QUEUE_PRODUCER,
    SANDBOX_DECISION_PRODUCER,
    build_operations_view,
)
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.projection.verification import RuntimeTupleVerdict, build_verification_view
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.doctor.models import CheckResult
from eawf.surfaces.tui.console.cells import NO_VALUE
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.harness import capture_cells, grid_errors
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.campaign import replay_line
from eawf.surfaces.tui.console.renderers.crash_recovery import doors
from eawf.surfaces.tui.console.renderers.health import NOTHING_TO_REPAIR, checks_line, checks_of
from eawf.surfaces.tui.console.renderers.read_model import UNKNOWN_WORD
from eawf.surfaces.tui.console.renderers.run_detail import TIMELINE_HEAD
from eawf.surfaces.tui.console.renderers.scope_home import NO_TRACK, groups_of, tree_of
from eawf.surfaces.tui.console.renderers.search import matched
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.surfaces.tui.console.width import pad

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
CURSOR = 41208
ROOT = "eawf://EAWF/EAWF/EAWF"


def _row(kind: str, key: str, status: str, revision: int = 1, **extra: Any) -> dict[str, Any]:
    return {"urn": f"{ROOT}/{kind}/{key}", "revision": revision, "status": status, **extra}


#: A small live-shaped tree: one Track, its Milestones and one filed under none, their
#: Batches, the Runs with their Tasks, and one record of each verification collection.
DOCUMENT: dict[str, Any] = {
    "track": {"TRK-CORE": _row("track", "TRK-CORE", "ACTIVE", title="Core framework")},
    "milestone": {
        "MLS-0100": _row(
            "milestone",
            "MLS-0100",
            "ACTIVE",
            2,
            title="Close out the canary",
            primary_track_ref=f"{ROOT}/track/TRK-CORE",
        ),
        "MLS-0101": _row(
            "milestone",
            "MLS-0101",
            "COMPLETED",
            3,
            title="Cut the first candidate",
            primary_track_ref=f"{ROOT}/track/TRK-CORE",
        ),
        "MLS-0900": _row("milestone", "MLS-0900", "PLANNED", title="Filed under nothing"),
    },
    "batch": {
        "BAT-0100": _row(
            "batch", "BAT-0100", "COMPLETED", milestone_ref=f"{ROOT}/milestone/MLS-0100"
        ),
        "BAT-0101": _row("batch", "BAT-0101", "ACTIVE", milestone_ref=f"{ROOT}/milestone/MLS-0100"),
    },
    "run": {
        "RUN-00000001": _row(
            "run", "RUN-00000001", "RUNNING", 2, scope={"task_ref": f"{ROOT}/task/TSK-0001"}
        ),
        "RUN-00000002": _row(
            "run", "RUN-00000002", "QUEUED", scope={"task_ref": f"{ROOT}/task/TSK-0002"}
        ),
        "RUN-00000003": _row("run", "RUN-00000003", "CANCELLED"),
    },
    "claim": {"CLM-0004": _row("claim", "CLM-0004", "UNCERTIFIED", title="Replay keeps order")},
    "evidence": {"EVD-0001": _row("evidence", "EVD-0001", "RECORDED")},
    "sandbox_policy": {"SBX-0001": _row("sandbox_policy", "SBX-0001", "ACTIVE", 3)},
    "health_view": {"hv-0001": _row("health_view", "hv-0001", "OK")},
    "campaign": {
        "CAM-0001": _row("campaign", "CAM-0001", "ACTIVE", title="Does drift move digests?")
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


def _model(route: str, document: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    """Return ``route``'s read model over the probe tree, built the way the app builds it."""
    projection = _projection(route, document)
    if route in ("scope.home", "run.detail", "search", "history.diff", "campaign"):
        return build_spine_view(projection)
    if route in ("activity", "attention", "cost.ceiling"):
        return build_register_view(projection)
    if route in ("trust", "evidence", "health"):
        return build_verification_view(projection, verdicts=kwargs.get("verdicts", ()))
    if route == "git.pr":
        return build_integration_view(projection, generations=(), conflicts=())
    return build_operations_view(projection)


def _frame(
    route: str,
    *,
    w: int = 120,
    document: dict[str, Any] | None = None,
    subject: str | None = None,
    conn: str = "LIVE",
    sel: int = 0,
    **kwargs: Any,
) -> list[str]:
    """Return the native frame of ``route`` on a console that holds no prototype row."""
    h = dict(SIZES)[w]
    model = _model(route, document, **kwargs)
    session = Session()
    session.route = route
    session.subj_id = subject
    session.conn = conn
    session.sel = sel
    session.pq = kwargs.get("query", "")
    held = (
        {"register": model}
        if route in ("activity", "attention", "cost.ceiling")
        else {"projection": model}
    )
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=w,
        h=h,
        linked=True,
        attention=model if route == "attention" else kwargs.get("attention"),
        replay=kwargs.get("replay"),
        **held,
    )
    frame = render_route(view)
    assert len(frame) == h
    return frame


def _text(frame: list[str]) -> str:
    return "\n".join(frame)


def _starts(frame: list[str], prefix: str) -> str:
    return next(row for row in frame if row.startswith(prefix))


# ---------- no packet route draws the generic table any more ----------


PACKET_ROUTES: tuple[str, ...] = (
    "scope.home",
    "activity",
    "attention",
    "run.detail",
    "trust",
    "evidence",
    "health",
    "sandbox.log",
    "unattended",
    "git.pr",
    "cost.ceiling",
    "crash.recovery",
    "search",
    "history.diff",
    "campaign",
)


@pytest.mark.parametrize("w", [80, 120, 160])
@pytest.mark.parametrize("route", PACKET_ROUTES)
def test_no_packet_route_draws_the_generic_table(route: str, w: int) -> None:
    frame = _frame(route, w=w)
    assert not any(re.match(r"^\s+ROW\s+KIND\s+STATUS", row) for row in frame)
    assert not any(row.startswith((" UNSTATED", " WAITING", " REGIONS")) for row in frame)
    assert frame[0].startswith(f" Eä ▸ {SCOPE}")
    assert "cursor" not in frame[1]


# ---------- UI-020 / UI-063: the trust route ----------


def test_ui_020_trust_is_bound_to_its_read_model_and_draws_the_three_groups() -> None:
    frame = _frame("trust", subject="MLS-0101")
    assert frame[0].startswith(" Eä ▸ EAWF ▸ MLS-0101 ▸ Trust")
    assert "Milestone MLS-0101 · 3 truth-field groups" in frame[1]
    assert re.match(r"^\s+JURY\s+VERDICT\s+ANSWERED BY\s+FRESHNESS", frame[3])
    for group in (" CALIBRATION", " TRACK RECORD"):
        assert any(row.startswith(group) for row in frame)
    assert frame[-1].split() == ["Enter", "evidence", ".", "actions", "i", "inspect", "Esc", "back"]


def test_ui_063_an_unobserved_subject_is_unknown_never_a_negative() -> None:
    frame = _frame("trust", subject="MLS-0101")
    row = next(r for r in frame if "CLM-0004" in r)
    assert f"{UNKNOWN_WORD}" in row and "no outcome recorded" in row
    assert "fail" not in row.lower()
    assert "jury-" not in _text(frame)


def test_ui_063_no_calibration_report_renders_every_numeric_cell_unavailable() -> None:
    frame = _frame("trust")
    calibration = _starts(frame, " CALIBRATION")
    assert "∅ unavailable" in calibration
    assert not re.search(r"\d", calibration)
    assert "0.00" not in _text(frame)


def test_ui_063_the_field_readout_names_producer_basis_and_freshness() -> None:
    frame = _frame("trust")
    field = _starts(frame, " FIELD")
    assert "CLM-0004 verdict · answered by" in field
    basis = frame[frame.index(field) + 1]
    assert "basis" in basis and "freshness live" in basis
    # the readout is docked at the foot, over the keybar
    assert frame.index(field) == len(frame) - 3


def test_ui_063_a_milestone_with_no_claim_says_so() -> None:
    frame = _frame("trust", document={"claim": {}})
    assert any("no outcome recorded for any subject" in row for row in frame)
    assert "no field is focused" in _starts(frame, " FIELD")


# ---------- UI-021 / UI-064: the evidence route ----------


def test_ui_021_the_ladder_names_the_plan_rungs_never_the_runners() -> None:
    frame = _frame("evidence", subject="CLM-0004")
    text = _text(frame)
    for i, rung in enumerate(("resolve", "anchor", "screen", "entail"), start=1):
        assert f"{i} {rung}" in text
    for runner in ("probe", "canary", "certify "):
        assert runner not in text


def test_ui_064_a_rung_with_no_outcome_is_unknown_with_open_as_of() -> None:
    frame = _frame("evidence", subject="CLM-0004")
    rungs = [row for row in frame if re.search(r"\d (resolve|anchor|screen|entail)", row)]
    assert len(rungs) == 4
    assert all(UNKNOWN_WORD in row and row.rstrip().endswith("open") for row in rungs)
    assert not any("failed" in row or "blocked by" in row for row in rungs)
    assert "uncertified" in _starts(frame, " STANDING")
    assert "lifecycle UNCERTIFIED" in _text(frame)


@pytest.mark.parametrize(
    ("w", "as_of", "ran_over", "graph"),
    [(80, False, False, False), (120, True, False, True), (160, True, True, True)],
)
def test_ui_064_wider_frames_add_columns_and_the_graph(
    w: int, as_of: bool, ran_over: bool, graph: bool
) -> None:
    frame = _frame("evidence", w=w, subject="CLM-0004")
    head = next(row for row in frame if re.match(r"^\s+RUNG\s+OUTCOME\s+WHAT IT CHECKED", row))
    assert ("AS OF" in head) is as_of
    assert ("IT RAN OVER" in head) is ran_over
    assert any(row.startswith(" GRAPH") for row in frame) is graph
    assert frame[-1].split() == [
        "↑↓",
        "rung",
        "Enter",
        "what",
        "it",
        "found",
        "y",
        "copy",
        "Esc",
        "back",
    ]


def test_ui_064_the_claim_line_carries_key_and_title_and_supports_is_cited() -> None:
    frame = _frame("evidence", subject="CLM-0004")
    assert "CLM-0004 · Replay keeps order" in _starts(frame, " CLAIM")
    assert any(row.startswith(" SUPPORTS") for row in frame)


def test_ui_064_a_scope_with_no_claim_says_so() -> None:
    frame = _frame("evidence", document={"claim": {}, "evidence": {}})
    assert "No Claim held" in frame[1]
    assert "holds no Claim" in _starts(frame, " CLAIM")


# ---------- UI-022 / UI-065: the health route ----------


def _verdict(name: str, status: str) -> RuntimeTupleVerdict:
    return RuntimeTupleVerdict(
        check=CheckResult(name=name, status=status, detail=f"{name} said so")  # type: ignore[arg-type]
    )


VERDICTS = (
    _verdict("tuple_ok", "ok"),
    _verdict("tuple_warn", "warn"),
    _verdict("tuple_fail", "fail"),
)


def test_ui_065_the_checks_line_counts_the_list_it_heads() -> None:
    model = _model("health", verdicts=VERDICTS)
    checks = checks_of(model)
    assert [c.outcome for c in checks] == ["passed", "warn", "failed", "passed"]
    assert checks_line(checks) == "4 checks · 1 failed · 1 warn · 0 unknown · 2 passed"
    frame = _frame("health", verdicts=VERDICTS)
    assert _starts(frame, " CHECKS").rstrip().endswith(checks_line(checks))


def test_ui_065_a_stored_status_the_frame_cannot_vouch_for_is_unknown() -> None:
    document = {"health_view": {"hv-9": _row("health_view", "hv-9", "DEGRADED")}}
    checks = checks_of(_model("health", document))
    assert [c.outcome for c in checks] == ["unknown"]
    frame = _frame("health", document=document)
    row = next(r for r in frame if "hv-9" in r and "stored" in r)
    # the result cell reads unknown; the stored word is only quoted as the reason
    assert re.search(r"hv-9\s+\? unknown\s+stored DEGRADED", row), row


def test_ui_065_the_repair_is_named_at_the_foot_and_never_run() -> None:
    frame = _frame("health", verdicts=VERDICTS, sel=2)
    repair = _starts(frame, " REPAIR")
    assert frame.index(repair) == len(frame) - 3
    assert "tuple_fail" in repair
    assert "running it lives in settings" in frame[-2]
    passing = _starts(_frame("health", verdicts=VERDICTS, sel=0), " REPAIR")
    assert NOTHING_TO_REPAIR in passing
    # the focused check's detail is the docked readout itself, so Enter offers nothing more
    assert _frame("health")[-1].split() == ["Esc", "back"]


@pytest.mark.parametrize(("w", "runner"), [(80, False), (120, True)])
def test_ui_065_the_runner_group_renders_at_120_and_wider(w: int, runner: bool) -> None:
    frame = _frame("health", w=w, verdicts=VERDICTS)
    assert any(row.startswith(" TUPLES") for row in frame) is runner
    assert ("ANSWERED BY" in _text(frame)) is runner


def test_ui_065_an_empty_health_register_says_nothing_declared_reported() -> None:
    frame = _frame("health", document={"health_view": {}})
    assert "0 checks" in _starts(frame, " CHECKS")
    assert any("no declared check has reported" in row for row in frame)


# ---------- UI-022 / UI-066: the sandbox log ----------


def test_ui_066_the_window_states_its_counts_unknown_never_zero_decisions() -> None:
    frame = _frame("sandbox.log")
    window = _starts(frame, " WINDOW")
    assert f"{UNKNOWN_WORD} decisions" in window
    assert "0 decisions" not in window
    assert SANDBOX_DECISION_PRODUCER in _text(frame)


@pytest.mark.parametrize(("w", "revision"), [(80, False), (120, True)])
def test_ui_066_the_revision_is_a_column_at_120_and_wider(w: int, revision: bool) -> None:
    frame = _frame("sandbox.log", w=w)
    head = next(row for row in frame if re.match(r"^\s+TIME\s+DECISION\s+RUN\s+REASON", row))
    assert ("REVISION" in head) is revision


def test_ui_066_a_policy_is_cited_in_the_packet_form() -> None:
    frame = _frame("sandbox.log")
    assert "sandbox policy · rev 3" in _text(frame)
    assert "pol-" not in _text(frame) and "fs.write.scope" not in _text(frame)
    assert frame.index(_starts(frame, " DECISION")) == len(frame) - 3
    assert frame[-1].split() == ["p", "policy", "Esc", "back"]


# ---------- UI-023 / UI-067: the unattended route ----------


def test_ui_067_the_queue_holds_every_run_whose_lifecycle_has_not_ended() -> None:
    frame = _frame("unattended")
    assert re.match(r"^\s+RUN\s+TASK\s+STATE\s+PROGRESS", frame[4])
    text = _text(frame)
    assert "RUN-00000001" in text and "RUN-00000002" in text
    assert "RUN-00000003" not in text
    assert "1 queued · 1 running · ? forced sequential" in _starts(frame, " QUEUE")


def test_ui_067_progress_is_never_a_bare_percentage() -> None:
    frame = _frame("unattended")
    queued = next(row for row in frame if "RUN-00000002" in row)
    running = next(row for row in frame if "RUN-00000001" in row)
    assert "∅ not started" in queued
    assert UNKNOWN_WORD in running and "TSK-0001" in running
    assert "%" not in _text(frame)


def test_ui_023_the_route_observes_and_names_the_daemon_as_authority() -> None:
    frame = _frame("unattended")
    assert "derived from the dependency graph" in _starts(frame, " PLAN")
    assert DISPATCH_QUEUE_PRODUCER in _text(frame)
    assert "every verb is a daemon request" in _starts(frame, " CONTROL")
    # no dispatch-queue record is read yet, so a pause or drain has nothing to address
    assert frame[-1].split() == [
        "↑↓",
        "row",
        "Enter",
        "run",
        "Esc",
        "back",
    ]


def test_ui_067_an_empty_queue_says_so() -> None:
    frame = _frame("unattended", document={"run": {}})
    assert any("the queue holds no record" in row for row in frame)
    assert "0 queued · 0 running" in _starts(frame, " QUEUE")


# ---------- UI-045: the git.pr route ----------


def test_ui_045_repository_facts_render_unavailable_never_clean() -> None:
    frame = _frame("git.pr", subject="BAT-0100")
    assert frame[0].startswith(" Eä ▸ EAWF ▸ BAT-0100 ▸ Git")
    assert "∅ unavailable" in _starts(frame, " BRANCH")
    checks = _starts(frame, " CHECKS")
    assert UNKNOWN_WORD in checks and "pass" not in checks.replace("passed", "")
    assert "happen in your git tool" in _starts(frame, " ACTION")
    assert frame[-1].split() == ["m", "conflict", "y", "copy", "Esc", "back"]


# ---------- UI-046: the cost.ceiling route ----------


def test_ui_046_the_ceiling_observes_and_no_zero_stands_for_unpriced() -> None:
    frame = _frame("cost.ceiling")
    assert UNKNOWN_WORD in _starts(frame, " CEILING")
    assert "settings" in _starts(frame, " OWNED BY")
    assert UNKNOWN_WORD in _starts(frame, " STOPPED")
    assert "∅ unmetered" in _text(frame)
    assert "80%" not in _text(frame) and "eighty" not in _text(frame)
    assert "ceiling moves in settings" in _starts(frame, " AUTHORITY")
    assert frame[-1].split() == ["Esc", "back"]


# ---------- UI-047: the crash.recovery route ----------


def test_ui_047_three_doors_each_state_where_they_leave_the_console() -> None:
    assert [(name, leaves) for name, _lost, leaves in doors(41208)] == [
        ("reattach", "GAP DETECTED"),
        ("replay", "REPLAYING"),
        ("read-only", "OFFLINE SNAPSHOT"),
    ]
    frame = _frame("crash.recovery")
    assert "41,209" in _text(frame)
    assert "2 runs were active then" in _text(frame)
    assert len([row for row in frame if re.search(r"(reattach|replay|read-only)\s+\?", row)]) == 3
    assert "No door discards work" in _starts(frame, " NO LOSS")
    assert frame[-1].split() == ["↑↓", "door", "i", "inspect", "Esc", "later"]


@pytest.mark.parametrize(("sel", "door"), [(0, "reattach"), (2, "read-only"), (9, "read-only")])
def test_ui_047_the_chosen_readout_follows_the_cursor(sel: int, door: str) -> None:
    frame = _frame("crash.recovery", sel=sel)
    assert _starts(frame, " CHOSEN").split()[1] == door


# ---------- UI-048: the search route ----------


def test_ui_048_matched_names_the_field_the_query_was_found_in() -> None:
    row = _model("search").rows[0]
    assert matched(row, "") == NO_VALUE
    assert matched(row, "trk-core") == "id"
    assert matched(row, "framework") == "name"
    assert matched(row, "nowhere") is None


def test_ui_048_hits_counts_by_kind_and_the_window_line() -> None:
    frame = _frame("search", query="cut")
    assert _starts(frame, " KINDS").split()[1:] == ["milestones", "1"]
    assert "MLS-0101" in _text(frame) and "MLS-0100" not in _text(frame)
    window = _starts(frame, " WINDOW")
    assert "1 of 1 · sorted by kind, then id · every count exact" in window
    assert "event text is not searched" in _starts(frame, " SCOPE")
    assert frame[-1].split() == ["Enter", "drill", "Esc", "back"]


def test_ui_048_a_query_nothing_carries_is_zero_hits_stated() -> None:
    frame = _frame("search", query="zzz")
    assert "0 hits" in frame[1]
    assert "0 of 0" in _starts(frame, " WINDOW")


def test_ui_048_an_incomplete_projection_never_calls_a_count_exact() -> None:
    model = dataclasses.replace(_model("search"), complete=False)
    session = Session()
    session.route = "search"
    view = View(
        session=session, fixture=Fixture.from_chrome(load_chrome()), w=120, h=30, projection=model
    )
    assert "every count known" in _starts(render_route(view), " WINDOW")


# ---------- UI-049: the history.diff route ----------


def test_ui_049_one_entity_at_two_of_its_own_revisions() -> None:
    frame = _frame("history.diff", subject="MLS-0101")
    assert frame[0].startswith(" Eä ▸ EAWF ▸ History ▸ Diff")
    assert "rev 2 → rev 3" in _starts(frame, " BETWEEN")
    field = next(row for row in frame if re.match(r"^ [▸ ] status", row))
    assert "COMPLETED" in field and field.count(UNKNOWN_WORD) == 2
    assert "system" not in _text(frame)
    assert "counted here, never hidden" in _starts(frame, " UNCHANGED")
    assert frame[-1].split() == ["Enter", "field", "e", "entity", "p", "revisions", "Esc", "back"]


def test_ui_049_a_first_revision_has_nothing_to_pair() -> None:
    frame = _frame("history.diff", subject="TRK-CORE")
    assert "nothing to pair" in _starts(frame, " BETWEEN")
    assert not any(re.match(r"^\s+FIELD\s+THEN\s+NOW", row) for row in frame)


# ---------- UI-073: the campaign route under REPLAYING ----------


def _negotiation(disposition: ReconnectDisposition) -> ReconnectNegotiation:
    gap = (
        None
        if disposition is ReconnectDisposition.CURRENT
        else {"first_sequence": 41191, "last_sequence": 41208}
    )
    return ReconnectNegotiation.model_validate(
        {
            "schema_version": "1.0",
            "route": "campaign",
            "disposition": disposition,
            "client_cursor": 41190 if gap else 41208,
            "server_cursor": 41208,
            "gap": gap,
            "retention": {"first_sequence": 1, "last_sequence": 41208},
        }
    )


def test_ui_073_a_replay_negotiation_carries_its_start_and_head() -> None:
    note = replay_note(_negotiation(ReconnectDisposition.REPLAY))
    assert note == ReplayNote(replaying_from_sequence=41190, head_sequence=41208)
    assert replay_note(_negotiation(ReconnectDisposition.CURRENT)) is None


@pytest.mark.parametrize(
    ("note", "line"),
    [
        (
            ReplayNote(replaying_from_sequence=41190, head_sequence=41208),
            f"replaying 41,190 → 41,208 · {UNKNOWN_WORD} findings promoted after this point",
        ),
        (
            ReplayNote(
                replaying_from_sequence=1, head_sequence=2, findings_promoted_after_cursor=0
            ),
            "replaying 1 → 2 · 0 findings promoted after this point",
        ),
        (None, f"replaying · the head it replays toward is {UNKNOWN_WORD}"),
    ],
)
def test_ui_073_the_status_line_states_the_replay_and_what_lies_past_it(
    note: ReplayNote | None, line: str
) -> None:
    assert replay_line(note) == line


def test_ui_073_the_campaign_frame_heads_with_the_replay_only_while_replaying() -> None:
    note = ReplayNote(replaying_from_sequence=41190, head_sequence=41208)
    replaying = _frame("campaign", conn="REPLAYING", replay=note)
    assert _starts(replaying, " REPLAYING").rstrip().endswith(replay_line(note))
    live = _frame("campaign", replay=note)
    assert not any(row.startswith(" REPLAYING") for row in live)
    for section in (" PLAN", " EVIDENCE", " ARTIFACTS"):
        assert any(row.startswith(section) for row in live)
    assert "Does drift move digests?" in _starts(live, " QUESTION")


# ---------- the four journeys the console opens on ----------


def test_home_nests_each_milestone_under_its_track_with_title_and_progress() -> None:
    groups = groups_of(_model("scope.home"))
    assert [(t.row.key if t.row else None, t.depth) for t in tree_of(groups, 0)] == [
        ("TRK-CORE", 0),
        ("MLS-0100", 1),
        ("MLS-0101", 1),
        (None, 0),
    ]
    # only the focused group expands; the unfiled group opens when the cursor is in it
    assert [(t.row.key if t.row else None, t.depth) for t in tree_of(groups, 1)] == [
        ("TRK-CORE", 0),
        (None, 0),
        ("MLS-0900", 1),
    ]
    frame = _frame("scope.home")
    assert re.match(r"^\s+MILESTONES\s+RUNS\s+ATTENTION\s+PROGRESS", frame[3])
    track = next(row for row in frame if "TRK-CORE" in row)
    assert "Core framework" in track and "1 of 2 milestones done" in track
    leaf = next(row for row in frame if "MLS-0100" in row)
    # the cursor lands on the first Milestone leaf, never on the Track above it
    assert leaf.startswith("   ▸ MLS-0100 Close out the canary")
    assert "ACTIVE" in leaf and "batches done" not in leaf
    assert NO_TRACK in _text(frame)
    assert frame[1].rstrip() == " 1 track · 3 milestones · 2 batches"


def test_home_attention_region_states_why_it_has_no_count() -> None:
    frame = _frame("scope.home")
    assert "has not been read" in _starts(frame, " ATTENTION")
    quiet = _frame("scope.home", attention=_model("attention"))
    assert _starts(quiet, " ATTENTION").startswith(" ATTENTION   nothing here opened itself")


def test_home_cursor_never_rests_on_the_group_heading() -> None:
    frame = _frame("scope.home", sel=3)
    caret = next(row for row in frame[3:] if row.lstrip().startswith("▸ "))
    assert "MLS-0900" in caret


def test_home_restores_the_selection_by_id_through_the_tree() -> None:
    model = _model("scope.home")
    session = Session()
    session.route = "scope.home"
    session.sel_id = "MLS-0900"
    view = View(
        session=session, fixture=Fixture.from_chrome(load_chrome()), w=120, h=30, projection=model
    )
    frame = render_route(view)
    caret = next(row for row in frame[3:] if row.lstrip().startswith("▸ "))
    assert "MLS-0900" in caret
    assert (session.sel, session.sel_id) == (2, "MLS-0900")


@pytest.mark.parametrize("w", [80, 120, 160])
def test_activity_draws_the_rail_where_the_registry_declares_it(w: int) -> None:
    frame = _frame("activity", w=w)
    rail = REGISTRY.rail_at("activity", w) is not None
    assert any("│ BUCKETS" in row for row in frame) is rail
    assert any(row.startswith(" BUCKETS") for row in frame) is not rail
    head = next(row for row in frame if re.match(r"^\s+RUN\s+TASK\s+STATE\s+REASON\s+AS OF", row))
    assert head
    run = next(row for row in frame if "RUN-00000001" in row)
    assert "TSK-0001" in run and "RUNNING" in run and UNKNOWN_WORD in run


def test_attention_states_mine_unknown_for_a_console_acting_as_nobody() -> None:
    frame = _frame("attention")
    assert frame[0].startswith(" Eä ▸ EAWF ▸ Needs you")
    # with nothing open the sub line is the frozen phrase, whoever the console acts as
    assert frame[1].startswith(" nothing needs you")
    open_one = {
        "pending_action": {
            "ACT-0001": {
                "urn": f"{ROOT}/pending-action/ACT-0001",
                "revision": 1,
                "status": "WAITING",
                "subject_ref": f"{ROOT}/run/RUN-00000001",
            }
        }
    }
    asked = _frame("attention", document=open_one)
    assert asked[1].startswith(f" {UNKNOWN_WORD} mine · 1 all principals")


def test_run_frame_draws_one_runs_facts_under_its_task() -> None:
    frame = _frame("run.detail", subject="RUN-00000001")
    assert frame[0].startswith(" Eä ▸ EAWF ▸ TSK-0001 ▸ RUN-00000001")
    assert frame[1].startswith(" Run RUN-00000001 · RUNNING")
    for label in (" STATE", " TASK", " PROVIDER", " USAGE", " CONTROLS", " LINEAGE"):
        assert any(row.startswith(label) for row in frame), label
    assert frame[3] == pad(TIMELINE_HEAD, len(frame[3])), "the timeline pane comes first"


def test_run_frame_with_no_run_says_so() -> None:
    frame = _frame("run.detail", document={"run": {}})
    assert "this scope holds no Run" in _starts(frame, " RUN")


# ---------- the live path, over a served epoch-2 tree ----------


def test_the_journeys_draw_the_packet_layouts_live_over_a_served_tree(tmp_path: Path) -> None:
    """A real daemon serves the canary tree, and each journey draws its packet layout."""
    from tests.tui.surfaces.tui.console.test_console_live_smoke import (
        live_console,
        render_setup,
        walk_canary_isolated,
    )

    walk, runtime_root = walk_canary_isolated(tmp_path)
    routes = ("scope.home", "activity", "run.detail", "trust", "unattended", "health")

    async def body() -> tuple[dict[str, str], dict[str, Any]]:
        frames: dict[str, str] = {}
        held: dict[str, Any] = {}
        async with (
            live_console(walk.canary.root, runtime_root) as (app, seam),
            app.run_test(size=SIZES[1]) as pilot,
        ):
            for route in routes:
                text = await render_setup(app, pilot, SessionSetup(route=route, size=1))
                assert not grid_errors(text, SIZES[1], capture_cells(app)), route
                frames[route] = text
                held[route] = seam.projection_for(route)
        return frames, held

    frames, held = asyncio.run(body())

    home = frames["scope.home"].splitlines()
    assert re.match(r"^\s+MILESTONES\s+RUNS\s+ATTENTION\s+PROGRESS", home[3])
    tracks = [r for r in held["scope.home"].rows if r.collection is Epoch2Collection.TRACK]
    milestones = [r for r in held["scope.home"].rows if r.collection is Epoch2Collection.MILESTONE]
    assert tracks and milestones
    for milestone in milestones:
        leaf = next(row for row in home if milestone.key in row)
        assert re.match(rf"^   [▸ ] {milestone.key}", leaf)
        assert milestone.status.value in leaf
    activity = frames["activity"].splitlines()
    assert any(re.match(r"^\s+RUN\s+TASK\s+STATE\s+REASON\s+AS OF", row) for row in activity)
    for run in held["activity"].rows:
        line = next(row for row in activity if run.key in row)
        assert run.parent_key is None or run.parent_key in line
    run_frame = frames["run.detail"].splitlines()
    assert any(row.startswith(" PROVIDER") for row in run_frame)
    assert any(re.match(r"^\s+JURY\s+VERDICT", row) for row in frames["trust"].splitlines())
    assert any(row.startswith(" QUEUE") for row in frames["unattended"].splitlines())
    assert any(row.startswith(" CHECKS") for row in frames["health"].splitlines())
    for text in frames.values():
        assert "ROW                               KIND" not in text
