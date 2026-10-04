"""CON-130 to CON-137 and CON-141: the decision overlays and cards, drawn from their records.

``-`` is the console's: it clears the rack under every overlay and drawer, leaving the
surface beneath exactly as it was. An overlay that draws no cursor advertises no arrow and
no ``Enter`` and swallows the arrows; an overlay that draws one advertises the arrows it
binds, moves that cursor with them and names the row it is on in its foot.

A decision overlay states ``STATE`` and ``ENDS WHEN`` as facts of the record it is about
and prints no ordinal, no cycling hint and no impossible-transition legend; ``s`` binds
nothing. Each overlay and card renders its record's own projection, one frame per
situation, and its keybar is every key it acts on.

PRX-062 closes the file: each decision overlay reachable by key has a driven journey in
the contract's port journey file, replayed through the golden harness into the real app.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.release import ReleaseStatus
from eawf.surfaces.tui.console.clock import FakeClock, notify
from eawf.surfaces.tui.console.decisions import (
    ClaimRecord,
    DecisionRecords,
    PauseRecord,
    QuestionRecord,
)
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.navigation import open_overlay
from eawf.surfaces.tui.console.normalisation import REWRITES, PackFrame
from eawf.surfaces.tui.console.operations import QuestionAnswer
from eawf.surfaces.tui.console.overlays.chassis import cursor_foot
from eawf.surfaces.tui.console.overlays.situations import (
    claim_standing,
    pause_situation,
    question_situation,
)
from eawf.surfaces.tui.console.registry import DRAWERS, OVERLAY_ARROWS, OVERLAYS, SURFACES
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.surfaces.tui.console.tokens import Severity

from . import decision_support as ds
from .journey_support import load_port_journeys, replayed
from .overlay_support import Link, chrome, frame_of, press, prototype, session_on
from .test_console_live_smoke import (
    ATTENTION_ACTION_KEY,
    live_console,
    render_setup,
    walk_canary_isolated,
)

#: The overlays CON-141 names cursorless, whatever their option rows.
CURSORLESS: tuple[str, ...] = ("help", "resolution", "marker", "question", "pause")
#: The route each cursorless overlay is opened from.
OPENED_FROM: dict[str, str] = {
    "help": "activity",
    "resolution": "history",
    "marker": "timeline",
    "question": "attention",
    "pause": "attention",
}
#: The overlays CON-141 names as drawing a cursor, each with its route and its foot label.
CURSORED: tuple[tuple[str, str, str], ...] = (
    ("evidence", "campaign", "RECEIPT"),
    ("acceptance", "milestone", "RECEIPT"),
    ("readiness", "release", "SIGNAL"),
    ("draft", "backlog", "FIELD"),
)
_FOOT = re.compile(r"^ (?P<label>[A-Z]+)\s+(?P<n>\d+) of (?P<total>\d+)\s*$")


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the prototype registers the golden contract replays."""
    return prototype()


def _foot(rows: list[str]) -> tuple[str, int, int]:
    """Return the label, the row and the total a cursor overlay's foot names."""
    found = [m for m in (_FOOT.match(row) for row in rows[1:-1]) if m is not None]
    assert len(found) == 1, "a cursor overlay names its row exactly once"
    return found[0]["label"], int(found[0]["n"]), int(found[0]["total"])


def _with_rack(session: Session) -> Session:
    notify(session, FakeClock(), text="copied", title="copied", sev=Severity.OK)
    return session


def test_con_141_the_registry_declares_a_cursor_per_entry() -> None:
    assert {name for name, _route, _label in CURSORED} == set(OVERLAY_ARROWS)
    for name in CURSORLESS:
        assert not SURFACES[name].cursor


@pytest.mark.parametrize("size", range(len(SIZES)))
@pytest.mark.parametrize("name", CURSORLESS)
def test_con_141_a_cursorless_overlay_advertises_no_arrow_and_no_enter(
    name: str, size: int, fixture: Fixture
) -> None:
    bar = frame_of(session_on("attention", overlay=name), fixture, size)[-1]
    assert "↑" not in bar
    assert "↓" not in bar
    assert "Enter" not in bar


@pytest.mark.parametrize("name", CURSORLESS)
@pytest.mark.parametrize("key", ["ArrowDown", "ArrowUp", "j", "k"])
def test_con_141_a_cursorless_overlay_swallows_the_arrows(
    name: str, key: str, fixture: Fixture
) -> None:
    session = session_on(OPENED_FROM[name], overlay=name, sel=1)
    before = frame_of(session, fixture)
    sel = session.sel
    press(session, fixture, key)
    assert (session.overlay, session.sel) == (name, sel)
    assert frame_of(session, fixture) == before


@pytest.mark.parametrize("size", range(len(SIZES)))
@pytest.mark.parametrize(("name", "route", "label"), CURSORED)
def test_con_141_a_cursor_overlay_advertises_its_arrows_and_names_its_row(
    name: str, route: str, label: str, size: int, fixture: Fixture
) -> None:
    rows = frame_of(session_on(route, overlay=name), fixture, size)
    assert "↑↓" in rows[-1]
    found, n, total = _foot(rows)
    assert (found, n) == (label, 1)
    assert total > 1


@pytest.mark.parametrize(("name", "route", "label"), CURSORED)
def test_con_141_every_arrow_moves_the_cursor_the_foot_names(
    name: str, route: str, label: str, fixture: Fixture
) -> None:
    session = session_on(route, overlay=name)
    total = _foot(frame_of(session, fixture))[2]
    press(session, fixture, "ArrowDown")
    assert _foot(frame_of(session, fixture)) == (label, 2, total)
    press(session, fixture, *["ArrowDown"] * (total + 2))
    assert _foot(frame_of(session, fixture)) == (label, total, total)
    press(session, fixture, "ArrowUp")
    assert _foot(frame_of(session, fixture)) == (label, total - 1, total)


def test_con_141_the_foot_is_one_labelled_row() -> None:
    assert cursor_foot("RECEIPT", 1, 3) == " RECEIPT   1 of 3"
    assert cursor_foot("", 0, 0) == "           0 of 0"


@pytest.mark.parametrize("name", [n for n in OVERLAYS if n != "palette"])
def test_con_141_the_rack_clear_acts_under_every_overlay(name: str, fixture: Fixture) -> None:
    session = _with_rack(session_on("attention", sel=1))
    open_overlay(session, name, subject="ACT-0031")
    press(session, fixture, "-")
    assert session.toasts == []
    assert (session.overlay, session.sel) == (name, 1)


@pytest.mark.parametrize("drawer", [d for d in DRAWERS if d != "go"])
def test_con_141_the_rack_clear_acts_under_every_drawer(drawer: str, fixture: Fixture) -> None:
    session = _with_rack(session_on("run.detail", subj_id="RUN-538453eb", overlay=drawer))
    press(session, fixture, "-")
    assert (session.toasts, session.overlay) == ([], drawer)


def test_con_141_the_rack_clear_keeps_the_go_prefix_armed(fixture: Fixture) -> None:
    session = _with_rack(session_on("run.detail", subj_id="RUN-538453eb"))
    press(session, fixture, "g", "-")
    assert (session.toasts, session.prefix) == ([], "g")


def test_con_141_the_rack_clear_acts_under_an_overlay_holding_nothing() -> None:
    session = _with_rack(session_on("activity", overlay="question"))
    press(session, chrome(), "-")
    assert (session.toasts, session.overlay) == ([], "question")


def test_con_141_the_palette_query_takes_the_dash_that_ids_carry(fixture: Fixture) -> None:
    """The palette's query is a text field and an entity id carries ``-``, so it types it."""
    session = _with_rack(session_on("scope.home", overlay="palette"))
    press(session, fixture, "r", "u", "n", "-")
    assert session.pq == "run-"
    assert len(session.toasts) == 1


# ---------- CON-130: a decision overlay states its record's state, never a model's ----------

QUESTIONS = ds.records("overlays/question.json")
PAUSES = ds.records("overlays/pause.json")
CLAIMS = ds.records("overlays/evidence.json")
DRAFTS = ds.records("overlays/draft.json")
MARKERS = ds.records("overlays/marker.json")
_HARNESS = re.compile(r"\(\d+ of \d+|s cycles|IMPOSSIBLE")
_STATE_ROWS = re.compile(r"^ (STATE |ENDS WHEN|IMPOSSIBLE)")


def _decision_frame(name: str, size: int = 1) -> tuple[list[str], dict[str, Any]]:
    """Return a decision overlay's frame over its first record, with what it was drawn from."""
    if name == "readiness":
        model = ds.release_view("candidate")
        return ds.frame(ds.opened("release", name, "REL-0001"), projection=model, size=size), {
            "projection": model
        }
    subject = {"question": "QST-0001", "pause": "RUN-a708a7d6", "evidence": "CLM-0001"}[name]
    held = {"question": QUESTIONS, "pause": PAUSES, "evidence": CLAIMS}[name]
    session = ds.opened("attention", name, subject)
    return ds.frame(session, decisions=held, size=size), {"decisions": held}


def _expected_state(name: str) -> tuple[str, str]:
    if name == "question":
        q = QUESTIONS.questions[0]
        sit = question_situation(q, principal="OP-0001", run_state="RUNNING")
    elif name == "pause":
        sit = pause_situation(PAUSES.pauses[0], "LOST")
    elif name == "evidence":
        sit = claim_standing(CLAIMS.claims[0])
    else:
        return "candidate · pinned · awaiting approval", "an operator approves against"
    return sit.name, sit.ends


@pytest.mark.parametrize("size", range(len(SIZES)))
@pytest.mark.parametrize("name", ["question", "pause", "evidence", "readiness"])
def test_con_130_state_and_ends_when_are_the_records_own_facts(name: str, size: int) -> None:
    rows, _held = _decision_frame(name, size)
    state, ends = _expected_state(name)
    assert ds.row(rows, "STATE").split("STATE", 1)[1].strip() == state
    joined = " ".join(r.strip() for r in rows[-4:-1])
    assert ends in joined
    assert not _HARNESS.search(ds.text(rows))


@pytest.mark.parametrize("name", ["question", "pause", "evidence", "readiness"])
def test_con_130_s_binds_nothing_on_a_decision_overlay(name: str) -> None:
    rows, held = _decision_frame(name)
    session = ds.opened("release" if name == "readiness" else "attention", name, None)
    session.ov_subject = {"readiness": "REL-0001", "question": "QST-0001"}.get(
        name, "RUN-a708a7d6" if name == "pause" else "CLM-0001"
    )
    before = ds.frame(session, **held)
    ds.press(session, "s", **held)
    assert session.overlay == name
    assert ds.frame(session, **held) == before
    assert "s" not in rows[-1].split()


@pytest.mark.parametrize("name", ["question", "pause", "evidence", "readiness"])
def test_con_130_the_prototype_frames_carry_no_harness_rows(name: str, fixture: Fixture) -> None:
    rows = frame_of(session_on("activity", overlay=name), fixture, 0)
    assert not [r for r in rows if _STATE_ROWS.match(r)]
    assert not _HARNESS.search("\n".join(rows))


@pytest.mark.parametrize("name", ["question", "pause", "evidence", "readiness"])
def test_con_130_a_golden_is_admitted_with_its_harness_rows_stripped(name: str) -> None:
    pack = json.loads(
        (Path(ds.FIXTURES) / "golden/sequences/frames-overlays.json").read_text(encoding="utf-8")
    )
    state = next(x for x in pack["states"] if x["id"] == f"overlay/{name}@80")
    rewrite = next(r for r in REWRITES if r.entry == "decision overlay harness rows")
    assert rewrite.rows is not None
    raw = tuple(state["frame"].split("\n"))
    assert _HARNESS.search("\n".join(raw)), "the pack records the harness rows"
    admitted = rewrite.rows(PackFrame(contract_id=state["id"], rows=raw, pack={}))
    assert not _HARNESS.search("\n".join(admitted))
    assert len(admitted) == len(raw)


# ---------- CON-131: the question detail renders the question projection ----------

QUESTION_STATES: dict[str, str] = {
    "QST-0001": "open",
    "QST-0002": "open · blocking",
    "QST-0003": "open · escalated",
    "QST-0004": "answered by you",
    "QST-0005": "answered elsewhere",
    "QST-0006": "defaulted · override open until 16:00",
    "QST-0007": "defaulted · sealed",
    "QST-0008": "withdrawn · by OP-0002",
    "QST-0009": "replaced by QST-0012",
    "QST-0010": "unanswerable · recover RUN-0000000d first",
}
_RAW_WORDS = re.compile(r"\b(OPEN|BLOCKED|ANSWERED|AUTO_RESOLVED|SEALED|DROPPED|auto_resolved)\b")


def _question(key: str, size: int = 1) -> list[str]:
    return ds.frame(ds.opened("attention", "question", key), decisions=QUESTIONS, size=size)


@pytest.mark.parametrize(("key", "state"), QUESTION_STATES.items())
def test_con_131_one_frame_per_projection_row(key: str, state: str) -> None:
    rows = _question(key)
    assert rows[0].startswith(f" Eä ▸ question · {key}")
    assert ds.row(rows, "STATE") == f" STATE     {state}"
    assert ds.row(rows, "ENDS WHEN").split("ENDS WHEN", 1)[1].strip()
    assert not _RAW_WORDS.search(ds.text(rows))
    assert "expired" not in ds.text(rows)


def test_con_131_the_ten_renders_are_pairwise_distinguishable() -> None:
    states = [ds.row(_question(key), "STATE") for key in QUESTION_STATES]
    assert len(set(states)) == len(QUESTION_STATES) == 10


@pytest.mark.parametrize("key", ["QST-0006", "QST-0007"])
def test_con_131_a_defaulted_question_never_reads_as_answered(key: str) -> None:
    rows = _question(key)
    assert "answered" not in ds.row(rows, "STATE")
    assert "selected by policy, not an answer" in ds.row(rows, "DEFAULT")
    assert ds.row(rows, "OVERRIDE")


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_con_131_the_header_run_options_and_recommendation(size: int) -> None:
    rows = _question("QST-0001", size)
    assert "asked by RUN-0000000a under BAT-0002 · 10:02" in rows[1]
    assert ds.row(rows, "QUESTION").endswith("the wave shim, or the alias table?")
    assert "retiring the wrong one breaks it" in ds.row(rows, "WHY")
    options = [r for r in rows if re.match(r"^ (OPTIONS)?\s+[1-4]  ", r)]
    assert options[0].rstrip().endswith("1  the wave shim only")
    assert len(options) == 3
    recommended = [o for o in options if "recommended · not consent" in o]
    assert len(recommended) == 1 and "2  the alias table only" in recommended[0]


def test_con_131_the_keybar_is_the_allowlist_of_an_open_question() -> None:
    rows = _question("QST-0001")
    # decline has no daemon verb, so the bar never offers it
    assert ds.keys(rows) == [" 1..3 pick an answer", "Esc back — it stays open"]
    session = ds.opened("attention", "question", "QST-0001")
    ds.press(session, "w", "4", "a", "Enter", decisions=QUESTIONS)
    assert session.reply is None
    assert session.overlay == "question"
    assert ds.frame(session, decisions=QUESTIONS) == rows


def test_con_131_a_reply_is_advertised_and_opens_only_where_it_is_legal() -> None:
    assert "w reply" in _question("QST-0011")[-1]
    session = ds.opened("attention", "question", "QST-0011")
    ds.press(session, "w", decisions=QUESTIONS)
    assert session.reply == {"text": ""}


def test_con_131_plan_046_a_typed_reply_is_sent_verbatim_through_the_answer_verb() -> None:
    link = Link()
    session = ds.opened("attention", "question", "QST-0011")
    ds.press(session, "w", "n", "o", "Enter", decisions=QUESTIONS, link=link)
    assert link.sent == [QuestionAnswer(target="QST-0011", reply="no")]
    assert session.reply is None


def test_con_131_plan_046_a_digit_answers_its_row_through_the_answer_verb() -> None:
    link = Link()
    session = ds.opened("attention", "question", "QST-0001")
    ds.press(session, "2", decisions=QUESTIONS, link=link)
    assert link.sent == [QuestionAnswer(target="QST-0001", option_key="alias_table")]
    assert session.overlay == "question"


def test_con_131_an_answered_question_is_immutable() -> None:
    rows = _question("QST-0004")
    assert rows[-1].strip() == "Esc back"
    assert "the alias table only · immutable" in ds.row(rows, "ANSWER")
    session = ds.opened("attention", "question", "QST-0004")
    ds.press(session, "1", "2", "x", "w", decisions=QUESTIONS)
    assert (session.overlay, session.reply, session.c_target) == ("question", None, None)
    assert ds.frame(session, decisions=QUESTIONS) == rows


def test_con_131_decline_opens_its_consequence_card() -> None:
    session = ds.opened("attention", "question", "QST-0001")
    ds.press(session, "x", decisions=QUESTIONS)
    assert (session.overlay, session.ov_subject) == ("consequence", "QST-0001")
    card = session.mutation
    assert card is not None and card.action == "decline"
    assert card.items[0].refusal is not None and card.items[0].request is None
    rows = ds.frame(session, decisions=QUESTIONS)
    assert "refused before sending · unbound_verb" in rows[1]
    assert ds.keys(rows) == [" Esc back — nothing happens"]


def test_con_131_escape_leaves_the_question_open() -> None:
    session = ds.opened("attention", "question", "QST-0001", sel=2)
    ds.press(session, "Escape", decisions=QUESTIONS)
    assert (session.overlay, session.sel) == (None, 2)
    assert QUESTIONS.questions[0].status.value == "OPEN"
    assert "stays as it is" in session.log[0].note


def test_con_131_the_two_principal_case_names_the_winner_and_supersedes_the_loser() -> None:
    rows = _question("QST-0005")
    assert ds.row(rows, "WON") == " WON       the wave shim only · by OP-0002"
    assert ds.row(rows, "YOURS") == " YOURS     both — they are independent · superseded"
    assert "not disclosed" not in ds.text(rows)


def test_con_131_not_disclosed_appears_only_for_an_authority_denial() -> None:
    assert "∅ not disclosed" in ds.row(_question("QST-0012"), "WON")
    for key in QUESTION_STATES:
        assert "not disclosed" not in ds.text(_question(key))


def test_con_131_a_pending_action_row_never_opens_the_question_detail() -> None:
    document = {
        "pending_action": {
            "ACT-0001": {
                "urn": "eawf://WSP-A/EAWF/REP-A/pending-action/ACT-0001",
                "revision": 1,
                "status": "WAITING",
            }
        }
    }
    held = ds.projection("attention", document)
    session = ds.opened("attention")
    # the Attention frame publishes the row under its caret by id; Enter reads only that
    session.sel_id = "ACT-0001"
    ds.press(session, "Enter", attention=held, decisions=QUESTIONS)
    assert (session.overlay, session.ov_subject) == ("consequence", "ACT-0001")
    assert session.sel_id == "ACT-0001"


def test_con_131_j2_01_live_a_sealed_pending_action_opens_no_card(tmp_path: Path) -> None:
    """The canary walk seals a real acceptance approval; Enter neither asks nor re-answers it.

    A sealed action has nothing left to confirm, so the Attention frame lists no open row
    and Enter opens neither the question detail nor an answer card over it.
    """
    walk, runtime_root = walk_canary_isolated(tmp_path)

    async def body() -> tuple[str | None, str | None, str | None, str]:
        async with (
            live_console(walk.canary.root, runtime_root) as (app, seam),
            app.run_test(size=SIZES[1]) as pilot,
        ):
            await render_setup(app, pilot, SessionSetup(route="attention", size=1))
            held = seam.projection_for("attention")
            assert held is not None
            assert [r.key for r in held.rows] == [ATTENTION_ACTION_KEY]
            app.press_key("Enter")
            header = app.frame_rows[0]
            return app.session.overlay, app.session.ov_subject, app.session.sel_id, header

    overlay, subject, selected, header = asyncio.run(body())
    assert (overlay, subject, selected) == (None, None, None)
    assert "consequence" not in header


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"status": "DROPPED"}, "drop reason"),
        ({"drop_reason": "superseded", "status": "DROPPED"}, "successor"),
        ({"status": "AUTO_RESOLVED"}, "defaulted"),
        ({"options": [{"key": f"o{i}", "label": f"o{i}"} for i in range(5)]}, "at most 4"),
        ({"id": "TSK-0001"}, "pattern"),
        ({"reply": "x" * 2001}, "2000"),
    ],
)
def test_con_131_a_question_the_projection_cannot_render_is_refused(
    change: dict[str, object], error: str
) -> None:
    raw = QUESTIONS.questions[0].model_dump(mode="json") | change
    with pytest.raises(ValidationError, match=error):
        QuestionRecord.model_validate(raw)


def test_con_131_a_reply_of_exactly_the_limit_is_admitted() -> None:
    raw = QUESTIONS.questions[0].model_dump(mode="json") | {"reply": "x" * 2000}
    assert QuestionRecord.model_validate(raw).reply == "x" * 2000


# ---------- CON-132: the pause detail renders the pause projection ----------

PAUSE_STATES: dict[str, str] = {
    "RUN-a708a7d6": "waiting on a check · the control outcome is unknown",
    "RUN-0000000b": "waiting on a check",
    "BAT-0003": "waiting on a person",
    "BAT-0004": "held · by OP-0002 · Hold HLD-0007",
    "BAT-0005": "escalated · budget · now waiting on ACT-0009",
    "BAT-0006": "resolved · the disk has room observed at 15:30",
    "BAT-0007": "cancelled · enclosing work cancelled",
}
_BACK_PAUSE = "Esc back — the pause stays as it is"


def _pause(scope: str, size: int = 1) -> list[str]:
    return ds.frame(ds.opened("attention", "pause", scope), decisions=PAUSES, size=size)


@pytest.mark.parametrize(("scope", "state"), PAUSE_STATES.items())
def test_con_132_one_frame_per_projection_row(scope: str, state: str) -> None:
    rows = _pause(scope)
    assert rows[0].startswith(f" Eä ▸ pause · {scope}")
    assert ds.row(rows, "STATE") == f" STATE     {state}"
    assert ds.row(rows, "ENDS WHEN").split("ENDS WHEN", 1)[1].strip()
    assert "paused by you" not in ds.text(rows)


def test_con_132_the_seven_renders_are_pairwise_distinguishable() -> None:
    states = {ds.row(_pause(scope), "STATE") for scope in PAUSE_STATES}
    assert len(states) == len(PAUSE_STATES) == 7


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_con_132_the_unknown_outcome_card(size: int) -> None:
    rows = _pause("RUN-a708a7d6", size)
    body = ds.text(rows)
    assert "requested   14:03 · accepted 14:03" in ds.row(rows, "PAUSE")
    assert "confirmed   —  the run never answered" in body
    assert "2 of 3 attempts used" in body
    assert "retry is not offered while the" in body
    assert ds.row(rows, "UNKNOWN") and ds.row(rows, "NOT")
    # let go has no daemon verb, so the bar offers only the reconcile that is sent
    assert ds.keys(rows) == [" n reconcile", _BACK_PAUSE]
    assert "cancel" not in rows[-1] and "retry" not in rows[-1]


@pytest.mark.parametrize("scope", [s for s in PAUSE_STATES if s != "RUN-a708a7d6"])
def test_con_132_no_other_card_offers_reconcile_or_let_go(scope: str) -> None:
    rows = _pause(scope)
    assert rows[-1].strip() == _BACK_PAUSE
    session = ds.opened("attention", "pause", scope)
    ds.press(session, "n", "c", "r", decisions=PAUSES)
    assert (session.overlay, session.c_target) == ("pause", None)


def test_con_132_a_person_wait_names_its_record_and_principals() -> None:
    rows = _pause("BAT-0003")
    assert "▸ ACT-0004" in ds.row(rows, "WAITS ON")
    assert ds.row(rows, "ELIGIBLE").endswith("OP-0001 · OP-0002")
    assert "ACT-0004" in ds.row(rows, "ENDS WHEN")


def test_con_132_held_and_escalated_are_told_apart() -> None:
    held, escalated = _pause("BAT-0004"), _pause("BAT-0005")
    assert "held" in ds.row(held, "STATE") and "escalated" not in ds.text(held)
    assert "escalated" in ds.row(escalated, "STATE") and "held ·" not in ds.text(escalated)
    assert "▸ ACT-0009" in ds.row(escalated, "RAISED")


def test_con_132_reconcile_opens_the_run_control_consequence_card() -> None:
    session = ds.opened("attention", "pause", "RUN-a708a7d6")
    ds.press(session, "n", decisions=PAUSES)
    assert (session.overlay, session.ov_subject) == ("consequence", "RUN-a708a7d6")
    assert session.c_target is not None
    assert (session.c_target["verb"], session.c_target["kind"]) == ("reconcile", "run")


def test_con_132_let_go_opens_a_consequence_card_refused_with_its_reason() -> None:
    session = ds.opened("attention", "pause", "RUN-a708a7d6")
    ds.press(session, "c", decisions=PAUSES)
    assert (session.overlay, session.ov_subject) == ("consequence", "RUN-a708a7d6")
    card = session.mutation
    assert card is not None and (card.action, card.kind) == ("let go", "control")
    assert card.items[0].request is None and card.items[0].refusal is not None
    assert "cancel" not in card.action


def test_con_132_escape_leaves_the_pause_unchanged() -> None:
    session = ds.opened("attention", "pause", "RUN-a708a7d6", sel=1)
    ds.press(session, "Escape", decisions=PAUSES)
    assert (session.overlay, session.sel) == (None, 1)
    assert session.log[0].note == "back — the pause stays as it is"


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"reason": "permission"}, "waits on"),
        ({"status": "HELD"}, "holder"),
        ({"status": "ESCALATED"}, "escalation"),
        ({"retry_used": -1}, "greater than or equal"),
        ({"surplus": 1}, "Extra inputs"),
    ],
)
def test_con_132_a_pause_the_projection_cannot_render_is_refused(
    change: dict[str, object], error: str
) -> None:
    raw = PAUSES.pauses[0].model_dump(mode="json") | change
    with pytest.raises(ValidationError, match=error):
        PauseRecord.model_validate(raw)


# ---------- CON-133: the evidence viewer renders one claim and its ladder ----------

STANDINGS: dict[str, str] = {
    "CLM-0001": "certified",
    "CLM-0002": "uncertified",
    "CLM-0003": "refuted",
    "CLM-0004": "attested",
    "CLM-0005": "unresolved reference · ∅ unavailable",
}
_STAGES = re.compile(r"\b(probe|canary|certify)\b")


def _claim(key: str, size: int = 1, held: DecisionRecords = CLAIMS) -> list[str]:
    return ds.frame(ds.opened("evidence", "evidence", key), decisions=held, size=size)


@pytest.mark.parametrize(("key", "standing"), STANDINGS.items())
def test_con_133_one_frame_per_standing(key: str, standing: str) -> None:
    rows = _claim(key)
    assert rows[0].startswith(f" Eä ▸ evidence · {key}")
    assert ds.row(rows, "STANDING") == f" STANDING  {standing}"
    assert ds.row(rows, "STATE") == f" STATE     {standing}"
    assert not _STAGES.search(ds.text(rows))
    assert "≠" not in ds.text(rows)


@pytest.mark.parametrize("size", [1, 2])
def test_con_133_the_claim_rows_and_the_four_rung_ladder(size: int) -> None:
    rows = _claim("CLM-0002", size)
    body = ds.text(rows)
    assert "drift is real on retry" in rows[3]
    for label in ("IN WORDS", "IT PROVES", "BREAKS IF", "FROM", "SUPPORT"):
        assert ds.row(rows, label)
    assert " CONTRADICTION" in [r.rstrip() for r in rows]
    for n, name in enumerate(("resolve", "anchor", "screen", "entail"), start=1):
        assert re.search(rf"{n} {name}\s", body)
    assert "? unknown" in body
    assert "not run · ∅ awaiting rung 3" in body
    assert "⊘ denied · authority class" in body
    assert ds.keys(rows) == [" ↑↓ row", "y copy", "Esc back"]


def test_con_133_a_short_frame_windows_the_ladder_around_its_cursor() -> None:
    session = ds.opened("evidence", "evidence", "CLM-0002")
    rows = ds.frame(session, decisions=CLAIMS, size=0)
    assert re.search(r"… \d+ rows below", ds.text(rows))
    assert " STANDING" not in ds.text(rows)
    ds.press(session, *["ArrowDown"] * 4, decisions=CLAIMS, size=0)
    rows = ds.frame(session, decisions=CLAIMS, size=0)
    assert re.search(r"▸ 4 entail", ds.text(rows))
    assert re.search(r"… \d+ rows above", ds.text(rows))
    assert ds.row(rows, "ROW") == " ROW       5 of 5"


def test_con_133_an_absent_prose_field_renders_no_row() -> None:
    claim = CLAIMS.claims[0].model_copy(update={"in_words": None, "breaks_if": None})
    rows = _claim("CLM-0001", held=DecisionRecords(claims=(claim,)))
    assert not [r for r in rows if r.startswith((" IN WORDS", " BREAKS IF"))]
    assert ds.row(rows, "IT PROVES")


def test_con_133_a_rung_3_negative_is_advisory_and_routes_to_rung_4() -> None:
    rows = _claim("CLM-0006")
    assert re.search(r"3 screen +advisory negative · routed to rung 4", ds.text(rows))
    assert "blocked" not in ds.text(rows)
    assert ds.row(rows, "STANDING") == " STANDING  uncertified"


def test_con_133_copy_yields_the_claim_or_the_rung_under_the_cursor() -> None:
    session = ds.opened("evidence", "evidence", "CLM-0001")
    ds.press(session, "y", decisions=CLAIMS)
    urn = CLAIMS.claims[0].urn
    assert session.toasts[-1].text == urn
    ds.press(session, "ArrowDown", "ArrowDown", "y", decisions=CLAIMS)
    assert session.toasts[-1].text == f"{urn}#rung-2"
    ds.press(session, "Enter", decisions=CLAIMS)
    assert session.overlay == "evidence"


def test_con_133_the_foot_names_the_row_the_cursor_is_on() -> None:
    session = ds.opened("evidence", "evidence", "CLM-0001")
    ds.press(session, *["ArrowDown"] * 9, decisions=CLAIMS)
    assert ds.row(ds.frame(session, decisions=CLAIMS), "ROW") == " ROW       5 of 5"


@pytest.mark.parametrize(
    "outcomes",
    [
        ["pass", "unknown", "pass", "not_run"],
        ["unknown", "pass", "not_run", "pass"],
        ["pass", "attested", "not_run", "not_run"],
    ],
)
def test_con_133_an_outcome_is_never_inferred(outcomes: list[str]) -> None:
    raw = CLAIMS.claims[0].model_dump(mode="json")
    for rung, outcome in zip(raw["rungs"], outcomes, strict=True):
        rung["outcome"] = outcome
    with pytest.raises(ValidationError):
        ClaimRecord.model_validate(raw)


def test_con_133_a_ladder_that_is_not_four_rungs_in_order_is_refused() -> None:
    raw = CLAIMS.claims[0].model_dump(mode="json")
    raw["rungs"] = raw["rungs"][:3]
    with pytest.raises(ValidationError, match="rungs 1 to 4"):
        ClaimRecord.model_validate(raw)


# ---------- CON-134: the acceptance evidence renders one sealed bundle ----------


def _acceptance_model() -> Any:
    return ds.milestone_view(
        ds.bundle(
            ds.step("AS-01", passed=True, evidence=("EVD-2201", "EVD-2204")),
            ds.step("AS-02", passed=False, evidence=()),
            ds.step("AS-03", passed=False, evidence=("EVD-2209",)),
        )
    )


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_con_134_the_bundle_at_its_digest(size: int) -> None:
    model = _acceptance_model()
    rows = ds.frame(ds.opened("milestone", "acceptance", "MLS-0030"), projection=model, size=size)
    assert rows[0].startswith(" Eä ▸ evidence · MLS-0030")
    assert re.fullmatch(r" at digest \w{4}…\w{3} · quotable at this exact revision\s*", rows[1])
    head = next(r for r in rows if "EVIDENCE" in r and "CRITERION" in r)
    first = next(r for r in rows if "EVD-2201" in r)
    for name, value in (("CRITERION", "AS-01"), ("KIND", "artifact"), ("WHEN", "Sep 18")):
        assert head.index(name) == first.index(value), name
    assert ds.row(rows, "CRITERIA") == " CRITERIA  1 met · 1 unmet · 1 failed · 0 denied"
    assert ds.row(rows, "DENIED")
    assert ds.row(rows, "NOT") == " NOT       Reading evidence does not accept the milestone."
    body = ds.text(rows)
    assert "EVT-" not in body and "STATE" not in body and "rung" not in body
    assert ds.keys(rows) == [" ↑↓ row", "y copy", "Esc back"]


def test_con_134_every_cited_evidence_is_a_row_and_an_unmet_criterion_is_not_hidden() -> None:
    rows = ds.frame(
        ds.opened("milestone", "acceptance", "MLS-0030"), projection=_acceptance_model()
    )
    for key in ("EVD-2201", "EVD-2204", "EVD-2209"):
        assert any(key in r for r in rows)
    assert any("∅ none" in r and "AS-02" in r for r in rows)
    assert ds.row(rows, "EVIDENCE") == " EVIDENCE  1 of 4"


def test_con_134_copy_yields_the_evidence_row_under_the_cursor() -> None:
    model = _acceptance_model()
    session = ds.opened("milestone", "acceptance", "MLS-0030")
    ds.press(session, "ArrowDown", "y", projection=model)
    assert session.toasts[-1].text == "EVD-2204"


def test_con_134_no_sealed_bundle_states_the_absence() -> None:
    rows = ds.frame(
        ds.opened("milestone", "acceptance", "MLS-0030"), projection=ds.milestone_view(None)
    )
    assert "no bundle is sealed for MLS-0030" in rows[1]
    assert rows[-1].strip() == "Esc back"


def test_con_134_it_binds_only_to_the_milestones_own_read_model() -> None:
    model = ds.release_view("candidate")
    rows = ds.frame(ds.opened("release", "acceptance", "MLS-0030"), projection=model)
    assert "is not held" in rows[1]


# ---------- CON-135: the readiness matrix renders the release's readiness ----------


def _readiness(status: str, *, approved: bool = True, size: int = 1) -> list[str]:
    model = ds.release_view(status, approved=approved)
    return ds.frame(ds.opened("release", "readiness", "REL-0001"), projection=model, size=size)


_IN_FLIGHT = ("publishing", "publish_timeout", "verifying", "recovering")


def test_con_135_every_release_status_renders_distinctly() -> None:
    states = {s.value: ds.row(_readiness(s.value), "STATE") for s in ReleaseStatus}
    assert len(set(states.values())) == len(ReleaseStatus) == 12
    for status in _IN_FLIGHT:
        assert "published" not in states[status]
        assert "released" in states[status] and "nothing" in states[status]
    assert "never success, never failure" in " ".join(_readiness("partially_released"))
    assert "pinned · awaiting approval" in states["candidate"]
    assert "bound to head aaaaaaa · exact" in states["approved"]
    assert "preflight failed" in states["preflight_failed"]


def test_con_135_draft_ends_when_the_manifest_is_pinned() -> None:
    rows = _readiness("draft")
    ends = " ".join(r.strip() for r in rows[-4:-1])
    assert "an operator pins the manifest with accepted membership" in ends
    assert "every member reaches ready" not in ds.text(rows)


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_con_135_the_matrix_rows(size: int) -> None:
    rows = _readiness("approved", size=size)
    assert rows[0].startswith(" Eä ▸ readiness · REL-0001 · approved")
    assert "MLS-0030 · completed · as of revision 41208" in ds.row(rows, "MEMBERS")
    body = ds.text(rows)
    assert "granted at head aaaaaaa · still exact" in ds.row(rows, "APPROVAL")
    assert "returns to DRAFT with the cause named" in body
    assert "returns to CANDIDATE" not in body
    assert not re.search(r"approvals \d+ of \d+|quorum", body)
    assert ds.row(rows, "PUBLISH")
    assert ds.keys(rows) == [" ↑↓ row", "Esc back"]


@pytest.mark.parametrize("status", ["draft", "candidate", "preflight_failed", "cancelled"])
def test_con_135_no_approval_exists_before_approved(status: str) -> None:
    assert "∅ none · awaiting approval" in ds.row(_readiness(status), "APPROVAL")


def test_con_135_the_no_approval_render() -> None:
    assert "∅ none · awaiting approval" in ds.row(
        _readiness("approved", approved=False), "APPROVAL"
    )


def test_con_135_a_signal_nothing_observes_names_why() -> None:
    rows = _readiness("candidate")
    gate = next(r for r in rows if "policy gate" in r)
    assert "? unknown" in gate
    assert "nothing in this tree checks" in gate


def test_con_135_the_cursor_walks_the_signals() -> None:
    model = ds.release_view("candidate")
    session = ds.opened("release", "readiness", "REL-0001")
    ds.press(session, "ArrowDown", "ArrowDown", "a", projection=model)
    assert ds.row(ds.frame(session, projection=model), "SIGNAL") == " SIGNAL    3 of 4"
    assert session.overlay == "readiness"


# ---------- CON-136: the draft card renders one backlog row ----------


def _draft(key: str, size: int = 1, held: DecisionRecords = DRAFTS) -> list[str]:
    return ds.frame(ds.opened("backlog", "draft", key), decisions=held, size=size)


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_con_136_the_incomplete_draft(size: int) -> None:
    rows = _draft("EAWF-0091", size)
    assert rows[0].startswith(" Eä ▸ draft · EAWF-0091")
    assert ds.row(rows, "DUE") == " DUE       MLS-0002 · Sep 30"
    assert ds.row(rows, "NEEDS") == " NEEDS     criteria · owner"
    table = [r for r in rows if re.match(r"^ {10}[▸ ] (criteria|owner|batch) ", r)]
    assert [r.split()[1 if "▸" in r else 0] for r in table] == ["criteria", "owner", "batch"]
    assert "what proof will show it is done" in table[0] and "∅ not answered yet" in table[0]
    assert "BAT-0002 Bound replay" in table[2]
    assert "p is refused while criteria and owner are unanswered." in ds.text(rows)
    assert ds.keys(rows) == [
        " ↑↓ field",
        "Enter set",
        "p promote",
        "x defer",
        "Esc back",
    ]


def test_con_136_the_complete_and_the_refused_deferred_draft() -> None:
    complete = ds.text(_draft("EAWF-0092"))
    assert "refused" not in complete and "p promotes it into BAT-0002 Bound replay." in complete
    deferred = _draft("EAWF-0093")
    assert "deferred" in deferred[1]
    assert "p is refused while owner is unanswered." in ds.text(deferred)


def test_con_136_the_promote_refused_then_set_then_promote_journey() -> None:
    link = Link()
    session = ds.opened("backlog", "draft", "EAWF-0091")
    ds.press(session, "p", decisions=DRAFTS, link=link)
    assert session.toasts[-1].title == "NOT PROMOTED"
    assert session.toasts[-1].text == "p is refused while criteria and owner are unanswered."
    ds.press(session, "Enter", decisions=DRAFTS)
    assert "set criteria" in session.log[0].note
    answered = DRAFTS.drafts[0].model_copy(update={"criteria": "EVD-2249", "owner": "OP-0001"})
    held = DecisionRecords(drafts=(answered,))
    ds.press(session, "p", decisions=held, link=link)
    assert "nothing was written" in session.log[0].note
    assert link.sent == []
    session.toasts = []
    assert "refused while" not in ds.text(ds.frame(session, decisions=held))


def test_con_136_defer_opens_the_consequence_card_that_asks_a_reason() -> None:
    session = ds.opened("backlog", "draft", "EAWF-0091")
    ds.press(session, "x", decisions=DRAFTS)
    assert (session.overlay, session.ov_subject) == ("consequence", "EAWF-0091")
    card = session.mutation
    assert card is not None and card.action == "defer"
    assert "durable reason" in card.items[0].effects[0]
    assert card.items[0].request is None


def test_con_136_the_route_advertises_open_never_promote() -> None:
    labels = [entry.pair() for entry in ROUTE_KEYS["backlog"]]
    assert ("Enter", "open") in labels
    assert ("Enter", "promote") not in labels
    assert not [pair for pair in labels if pair[1] == "promote"]


def test_con_136_a_refusing_connection_keeps_only_the_read_keys() -> None:
    session = ds.opened("backlog", "draft", "EAWF-0091", conn="OFFLINE SNAPSHOT")
    rows = ds.frame(session, decisions=DRAFTS)
    assert ds.keys(rows) == [" ↑↓ field", "Esc back"]
    ds.press(session, "p", "x", decisions=DRAFTS)
    assert (session.overlay, session.mutation, session.toasts) == ("draft", None, [])


# ---------- CON-137: the marker card renders one roadmap marker's record ----------

MARKER_FACT_ROWS: dict[str, str] = {
    "MLS-0001": "● dated · the date is committed, not forecast",
    "MLS-0002": "○ forecast · a proposal, not a commitment",
    "MLS-0003": "– undated · no date is committed or forecast",  # noqa: RUF001
}


@pytest.mark.parametrize(("key", "fact"), MARKER_FACT_ROWS.items())
def test_con_137_a_dated_a_forecast_and_an_undated_marker(key: str, fact: str) -> None:
    rows = ds.frame(ds.opened("timeline", "marker", key), decisions=MARKERS)
    assert rows[0].startswith(f" Eä ▸ marker · {key}")
    assert "Replay-safe activity · Runtime" in rows[1] and "Roadmap" in rows[1]
    assert ds.row(rows, "MARKER") == f" MARKER    {fact}"
    for label in ("TRACK", "STATE", "PROMISED", "BUILT", "VERDICT"):
        assert ds.row(rows, label)
    assert "what the glyph on the lane stands for." in ds.text(rows)
    assert "timeline" not in ds.text(rows)
    assert rows[-1].strip() == "Esc back"


def test_con_137_the_sealed_marker_names_its_bundle_and_verdict() -> None:
    rows = ds.frame(ds.opened("timeline", "marker", "MLS-0001"), decisions=MARKERS)
    assert ds.row(rows, "BUNDLE") == " BUNDLE    sealed 13:20 · digest 2b9f…c41 · immutable"
    assert ds.row(rows, "VERDICT") == " VERDICT   2 of 3 criteria proven · 1 open"


def test_con_137_no_key_but_escape_acts_and_the_subject_never_moves() -> None:
    session = ds.opened("timeline", "marker", "MLS-0001", sel=1)
    before = ds.frame(session, decisions=MARKERS)
    ds.press(session, "ArrowDown", "Enter", "y", "p", decisions=MARKERS)
    assert (session.ov_subject, session.sel) == ("MLS-0001", 1)
    assert ds.frame(session, decisions=MARKERS) == before
    ds.press(session, "Escape", decisions=MARKERS)
    assert session.overlay is None


def test_con_137_an_absent_marker_states_the_absence_under_its_id() -> None:
    rows = ds.frame(ds.opened("timeline", "marker", "MLS-0009"), decisions=MARKERS)
    assert rows[0].startswith(" Eä ▸ marker · MLS-0009")
    assert "nothing is recorded for MLS-0009" in ds.text(rows)
    assert rows[-1].strip() == "Esc back"


# ---------- PRX-062: every decision overlay has a driven journey in the contract ----------


def _driven(journey_id: str) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Return a replayed port journey's frames, projections and the console's key responses."""
    result = replayed().results[journey_id]
    assert result.ok, f"{journey_id}: {result.detail}"
    journey = next(j.journey for j in load_port_journeys().journeys if j.journey.id == journey_id)
    responses = [entry.response for entry in replayed().sent[journey_id]]
    return [s.frame for s in journey.steps], [s.after for s in journey.steps], responses


def test_prx_062_question_detail_escape_keeps_it_open_decline_previews_a_digit_answers() -> None:
    shots, after, said = _driven("PJ12")
    assert (after[3]["overlay"], shots[3].split("\n")[0].split()[2]) == ("question", "question")
    assert after[4]["overlay"] is None
    assert said[3] == "Esc → back — ACT-0032 stays open"
    assert shots[4] == shots[2], "Esc moved the row the question was opened from"
    assert after[6]["overlay"] == "consequence"
    assert said[5] == "x → decline ACT-0032 → consequence preview"
    assert said[6] == "Esc → cancelled — ACT-0032 unchanged, nothing was written"
    assert said[8].startswith("1 → answer · ")
    assert "nothing was written" in said[8]


def test_prx_062_pause_detail_escape_leaves_the_pause_and_reconcile_previews() -> None:
    shots, after, said = _driven("PJ13")
    assert after[1]["overlay"] == "pause"
    assert said[1] == "Esc → back — the pause stays unknown"
    assert shots[2] == shots[0]
    assert (after[4]["overlay"], said[3]) == (
        "consequence",
        "n → reconcile RUN-a708a7d6 → consequence preview",
    )


def test_prx_062_the_readiness_matrix_walks_its_signals_and_closes_in_place() -> None:
    shots, after, _said = _driven("PJ14")
    assert [a["overlay"] for a in after] == [None, "readiness", "readiness", None]
    assert _foot(shots[1].split("\n"))[:2] == ("SIGNAL", 1)
    assert _foot(shots[2].split("\n"))[:2] == ("SIGNAL", 2)
    assert shots[3] == shots[0]


def test_prx_062_the_draft_card_refuses_promote_naming_what_is_unanswered() -> None:
    shots, after, said = _driven("PJ15")
    assert after[1]["overlay"] == "draft"
    assert said[1] == "p → refused · criteria and owner unanswered"
    assert said[2] == "ArrowDown → owner"
    assert said[3] == "Enter → set owner · the field the cursor is on"
    # Esc returns to the draft's row; the refusal stands in the rack until it ages out
    assert {**after[5], "toasts": 0} == after[0]
    assert "NOT PROMOTED" in shots[5] and "NOT PROMOTED" not in shots[0]


def test_prx_062_the_marker_card_is_immutable_and_closes_in_place() -> None:
    shots, after, _said = _driven("PJ16")
    assert after[1]["overlay"] == "marker"
    assert shots[2] == shots[1], "a key inside the marker card changed what it shows"
    assert shots[3] == shots[0]


def test_prx_062_the_resolution_card_states_a_purged_target_and_returns_to_its_row() -> None:
    shots, after, _said = _driven("PJ17")
    assert after[1]["overlay"] == "resolution"
    assert re.search(r"^ ENDING\s+purged", shots[1], re.MULTILINE)
    assert shots[2] == shots[0]
