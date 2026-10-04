"""The console acts as its operator, offers only what acts, and says one thing everywhere.

Every frame here is drawn from rows a test hands the console, the packaged chrome alone
behind them: who the header says the console acts as, which keys a card offers when a
write would refuse, the one word a Run that stopped answering is called on every route,
the questions dismissed in one decision through the question answer verb, the Task's
proof read off the newest receipt of each criterion, and the plain words the frames use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.compute import (
    CRITERION_FACT,
    STALL_KIND,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.projection.spine import SpineRow, build_spine_view
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.cards import GateKind, gate
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.frame import View, header
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.mutation import lifecycle_verbs
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import (
    DISMISS_REPLY,
    DISMISS_VERB,
    NO_PRINCIPAL_REASON,
    RUN_CONTROLS,
    UNBOUND_REASON,
    QuestionAnswer,
    binding_refusal,
    linked_refusal,
)
from eawf.surfaces.tui.console.overlays.decision import (
    CARRIED_OVER,
    pause_keys,
    question_keys,
)
from eawf.surfaces.tui.console.overlays.situations import LOST, stopped_answering
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.attention import (
    NO_DEADLINE,
    NO_RECEIPT_LINE,
    age_words,
    due_cell,
)
from eawf.surfaces.tui.console.renderers.history import _event_words
from eawf.surfaces.tui.console.renderers.scope_home import attention_lines
from eawf.surfaces.tui.console.renderers.spine import RESUME_VERB, offered_verbs
from eawf.surfaces.tui.console.renderers.task_detail import _due_text, _proof_text
from eawf.surfaces.tui.console.session import SIZES, Session

from . import decision_support as ds
from .overlay_support import Host, Link, chrome, prototype

AT = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
ROOT = "eawf://EAWF/EAWF/EAWF"
OPERATOR = "OP-0001"


def _session(route: str, **fields: Any) -> Session:
    session = Session()
    session.route = route
    for name, value in fields.items():
        setattr(session, name, value)
    return session


def _view(session: Session, **fields: Any) -> View:
    w, h = SIZES[1]
    return View(session=session, fixture=chrome(), w=w, h=h, **fields)


# ---------- who the console acts as ----------


def test_the_header_names_the_principal_a_linked_console_acts_as() -> None:
    view = _view(_session("scope.home"), linked=True, principal=OPERATOR)
    assert f"as {OPERATOR}" in header(view, " Eä ▸ home")


@pytest.mark.parametrize(
    ("linked", "principal"), [(False, OPERATOR), (True, None)], ids=["no-link", "nobody"]
)
def test_the_header_names_no_one_it_does_not_act_as(linked: bool, principal: str | None) -> None:
    view = _view(_session("scope.home"), linked=linked, principal=principal)
    assert " as " not in header(view, " Eä ▸ home")


def test_the_header_keeps_its_width_with_the_principal_named() -> None:
    view = _view(_session("scope.home"), linked=True, principal="O" + "P" * 31)
    assert len(header(view, " Eä ▸ home")) == view.w


# ---------- the question and pause cards offer only what is sent ----------

QUESTIONS = ds.records("overlays/question.json")
PAUSES = ds.records("overlays/pause.json")


def _question(key: str) -> Any:
    found = QUESTIONS.question(key)
    assert found is not None
    return found


def test_an_open_question_offers_its_answers_and_never_an_unbound_decline() -> None:
    pairs = question_keys(_question("QST-0001"), Session(), "RUNNING", "")
    tokens = [token for token, _label in pairs]
    assert tokens[0] == "1..3"
    assert "x" not in tokens
    assert tokens[-1] == "Esc"


def test_a_console_acting_as_nobody_offers_no_answer() -> None:
    pairs = question_keys(_question("QST-0001"), Session(), "RUNNING", NO_PRINCIPAL_REASON)
    assert [token for token, _label in pairs] == ["Esc"]


def test_a_question_no_answer_verb_records_offers_no_answer() -> None:
    record = _question("QST-0001").model_copy(update={"id": "ACT-0001"})
    assert [t for t, _label in question_keys(record, Session(), "RUNNING", "")] == ["Esc"]


def test_a_pause_offers_reconcile_and_never_an_unbound_let_go() -> None:
    pause = next(p for p in PAUSES.pauses if p.id == "PSE-0001")
    assert [t for t, _l in pause_keys(pause, Session(), LOST, "")] == ["n", "Esc"]
    assert [t for t, _l in pause_keys(pause, Session(), LOST, NO_PRINCIPAL_REASON)] == ["Esc"]


def test_a_migrated_reason_reads_in_words_not_addresses() -> None:
    record = _question("QST-0001").model_copy(
        update={"rationale": "asked under urn:eawf:v1:state:EAWF before the move to epoch 2"}
    )
    held = QUESTIONS.model_copy(update={"questions": (record,)})
    rows = ds.frame(ds.opened("attention", "question", "QST-0001"), decisions=held)
    assert CARRIED_OVER in ds.text(rows)
    assert "urn:eawf" not in ds.text(rows)


# ---------- the Attention register: ages, receipts and the stalled word ----------


def _attention_projection(*, questions: int = 1, stall: bool = False) -> RouteProjection:
    document: dict[str, Any] = {
        "pending_action": {
            "ACT-0001": {
                "key": "ACT-0001",
                "urn": f"{ROOT}/pending-action/ACT-0001",
                "revision": 3,
                "status": "WAITING",
                "kind": "protected_approval",
                "question": "accept the milestone?",
                "created_at": "2026-10-04T11:00:00Z",
            }
        },
        "open_question": {
            f"QST-{n:04d}": {
                "key": f"QST-{n:04d}",
                "urn": f"{ROOT}/question/QST-{n:04d}",
                "revision": 1,
                "status": "open",
                "question": f"over-budget advisory {n}",
                "created_at": "2026-09-04T12:00:00Z",
            }
            for n in range(1, questions + 1)
        },
    }
    ledger: dict[Epoch2Collection, tuple[dict[str, Any], ...]] = {}
    if stall:
        ledger[Epoch2Collection.RUN] = (
            {
                "payload_kind": STALL_KIND,
                "key": "RUN-00000154-STALL-1",
                "urn": f"{ROOT}/run/RUN-00000154",
                "revision": 1,
                "status": "stalled",
            },
        )
    return build_route_projection(
        route="attention",
        document=document,
        cursor=2408,
        scope_id="EAWF",
        generated_at=AT,
        ledger_rows=ledger,
    )


def _attention_frame(*, sealable: bool, principal: str | None = OPERATOR) -> list[str]:
    register = build_register_view(_attention_projection())
    session = _session("attention")
    view = _view(
        session,
        register=register,
        attention=register,
        linked=True,
        principal=principal,
        sealable=sealable,
        now=AT,
    )
    return render_route(view)


def test_an_undated_item_states_its_age_instead_of_a_bare_dash() -> None:
    register = build_register_view(_attention_projection())
    question = next(row for row in register.rows if row.key == "QST-0001")
    assert due_cell(question, AT) == "30d old"
    assert due_cell(question, None) == NO_DEADLINE


@pytest.mark.parametrize(
    ("seconds", "words"),
    [(0, "0s"), (59, "59s"), (86_399, "23h 59m"), (86_400, "1d"), (86_400 * 400, "400d")],
)
def test_an_age_reads_in_days_from_a_day_on(seconds: int, words: str) -> None:
    assert age_words(seconds) == words


def test_an_age_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        age_words(-1)


def test_an_answer_needing_a_receipt_is_not_offered_without_one() -> None:
    rows = _attention_frame(sealable=False)
    assert "a answer" not in rows[-1]
    assert "x deny" not in rows[-1]
    assert NO_RECEIPT_LINE in "\n".join(rows)


def test_an_answer_with_its_receipt_is_offered() -> None:
    rows = _attention_frame(sealable=True)
    assert "a answer" in rows[-1]
    assert NO_RECEIPT_LINE not in "\n".join(rows)


def test_the_counts_line_carries_no_jargon() -> None:
    assert "opened itself" not in "\n".join(_attention_frame(sealable=True))


def test_home_names_every_bucket_so_a_stall_behind_questions_reaches_it() -> None:
    register = build_register_view(_attention_projection(questions=5, stall=True))
    view = _view(_session("scope.home"), attention=register, principal=OPERATOR, now=AT)
    lines = "\n".join(str(line) for line in attention_lines(view, register))
    assert " NEEDS OPERATOR  6" in lines
    assert "1 stalled" in lines
    assert "30d old" in lines


def test_home_with_nothing_mine_says_so_plainly() -> None:
    register = build_register_view(_attention_projection(questions=0))
    view = _view(_session("scope.home"), attention=register, principal=None)
    lines = "\n".join(str(line) for line in attention_lines(view, register))
    assert "nothing needs you" in lines
    assert "opened itself" not in lines


# ---------- one status for a Run that stopped answering ----------


def _run_spine(status: str = "RUNNING") -> Any:
    document = {
        "run": {
            "RUN-00000154": {
                "key": "RUN-00000154",
                "urn": f"{ROOT}/run/RUN-00000154",
                "revision": 1,
                "status": status,
            }
        }
    }
    return build_spine_view(
        build_route_projection(
            route="run.detail", document=document, cursor=5, scope_id="EAWF", generated_at=AT
        )
    )


@pytest.mark.parametrize(
    ("key", "status", "states", "stopped"),
    [
        ("RUN-1", "RUNNING", {"RUN-1": LOST}, True),
        ("RUN-1", "RUNNING", {}, False),
        ("RUN-1", "COMPLETED", {"RUN-1": LOST}, False),
        (None, "RUNNING", {"RUN-1": LOST}, False),
        ("RUN-1", "RUNNING", {"RUN-2": LOST}, False),
    ],
)
def test_stopped_answering_is_a_running_run_the_stall_read_names(
    key: str | None, status: str, states: dict[str, str], stopped: bool
) -> None:
    assert stopped_answering(key, status, states) is stopped


def test_a_stopped_run_is_offered_its_resume_and_nothing_that_treats_it_as_running() -> None:
    session = _session("run.detail", subj_id="RUN-00000154", sel_id="RUN-00000154")
    spine = _run_spine()
    verbs = offered_verbs(session, chrome(), spine, {"RUN-00000154": LOST})
    names = [verb.verb for verb in verbs]
    assert names[-1] == RESUME_VERB.verb
    assert "interrupt" not in names
    assert "cancel" not in names
    assert RUN_CONTROLS[RESUME_VERB.verb] is ControlKind.RESUME
    assert binding_refusal("run.detail", RESUME_VERB.verb) == ""


def test_a_stopped_run_is_offered_no_lifecycle_verb() -> None:
    session = _session("run.detail", subj_id="RUN-00000154", sel_id="RUN-00000154")
    rows = tuple(_run_projection_rows())
    decided = gate(session, chrome(), verb="lifecycle move", linked=True)
    assert decided.kind is GateKind.OPEN
    assert lifecycle_verbs(session, rows, decided, None)
    assert lifecycle_verbs(session, rows, decided, {"RUN-00000154": LOST}) == ()


def _run_projection_rows() -> Any:
    projection = build_route_projection(
        route="run.detail",
        document={
            "run": {
                "RUN-00000154": {
                    "key": "RUN-00000154",
                    "urn": f"{ROOT}/run/RUN-00000154",
                    "revision": 1,
                    "status": "RUNNING",
                }
            }
        },
        cursor=5,
        scope_id="EAWF",
        generated_at=AT,
    )
    return projection.rows


def test_a_running_run_keeps_its_controls() -> None:
    session = _session("run.detail", subj_id="RUN-00000154")
    names = [verb.verb for verb in offered_verbs(session, chrome(), _run_spine(), {})]
    assert "interrupt" in names
    assert RESUME_VERB.verb not in names


# ---------- a menu offers only verbs a daemon verb carries ----------


@pytest.mark.parametrize("route", ["batch.detail", "milestone", "track", "task.detail"])
def test_a_linked_menu_lists_no_unbound_writing_verb(route: str) -> None:
    verbs = offered_verbs(_session(route), chrome(), None)
    assert not [
        v.verb for v in verbs if v.mutates and linked_refusal(route, v.verb) == UNBOUND_REASON
    ]


def test_the_prototype_replay_keeps_its_whole_menu() -> None:
    session = _session("batch.detail")
    assert offered_verbs(session, prototype(), None) == prototype().menus.verbs("batch.detail")


# ---------- dismissing many questions in one decision ----------


def _press(session: Session, projection: RouteProjection, link: Link, *keys: str) -> None:
    register = build_register_view(projection)
    w, h = SIZES[1]
    for key in keys:
        compose_frame(
            View(
                session=session,
                fixture=chrome(),
                w=w,
                h=h,
                register=register,
                attention=register,
                linked=True,
                principal=OPERATOR,
                rows=tuple(projection.rows),
            )
        )
        ctx = Ctx(
            session=session,
            fixture=chrome(),
            host=Host(),
            w=w,
            h=h,
            send=link,
            attention=projection,
            rows=tuple(projection.rows),
            principal=OPERATOR,
        )
        dispatch(ctx, key, False)


def test_every_listed_question_is_dismissed_through_the_answer_verb() -> None:
    projection = _attention_projection(questions=3)
    session, link = _session("attention"), Link()

    _press(session, projection, link, ".", "*")
    assert session.marked == ["QST-0001", "QST-0002", "QST-0003"]
    _press(session, projection, link, ".", "c", "Enter")

    assert link.sent == [
        QuestionAnswer(target=f"QST-{n:04d}", reply=DISMISS_REPLY) for n in (1, 2, 3)
    ]
    assert session.marked == []


def test_space_marks_one_question_and_comma_clears_the_marks() -> None:
    projection = _attention_projection(questions=2)
    session, link = _session("attention", sel_id="QST-0002"), Link()

    _press(session, projection, link, " ")
    assert session.marked == ["QST-0002"]
    _press(session, projection, link, ",")
    assert session.marked == []


def test_a_pending_action_is_never_marked_for_dismissal() -> None:
    projection = _attention_projection(questions=1)
    session, link = _session("attention", sel_id="ACT-0001"), Link()

    _press(session, projection, link, " ")
    assert session.marked == []


def test_dismissing_with_nothing_marked_or_selected_sends_nothing() -> None:
    projection = _attention_projection(questions=1)
    session, link = _session("attention", sel_id="ACT-0001"), Link()

    _press(session, projection, link, ".", "c", "Enter")
    assert link.sent == []


def test_a_bucket_that_hides_the_questions_marks_and_dismisses_none_of_them() -> None:
    """The error path: a question the chosen bucket hides is never marked or answered."""
    projection = _attention_projection(questions=2, stall=True)
    session, link = _session("attention"), Link()

    _press(session, projection, link, ".", "*")
    assert session.marked == ["QST-0001", "QST-0002"]
    session.bucket = "stalled"
    _press(session, projection, link, ".", "*")
    assert session.marked == ["QST-0001", "QST-0002"], "a hidden question was selected"
    _press(session, projection, link, ".", "c", "Enter")

    assert link.sent == []


def test_select_all_under_a_bucket_marks_only_what_it_shows() -> None:
    projection = _attention_projection(questions=2, stall=True)
    session, link = _session("attention", bucket="stalled"), Link()

    _press(session, projection, link, ".", "*")

    assert session.marked == []


@pytest.mark.parametrize(
    ("questions", "marked", "words"), [(1, 1, "1 question "), (3, 3, "3 questions ")]
)
def test_the_dismiss_card_names_how_many_questions_it_answers(
    questions: int, marked: int, words: str
) -> None:
    """Boundary: one question reads singular, several plural, so Enter is never a surprise."""
    projection = _attention_projection(questions=questions)
    session, link = _session("attention"), Link()

    _press(session, projection, link, ".", "*")
    assert len(session.marked) == marked
    _press(session, projection, link, ".", "c")

    assert session.c_target is not None
    assert session.c_target["effects"].startswith(words)


def test_the_dismiss_card_counts_none_when_nothing_listed_is_marked() -> None:
    projection = _attention_projection(questions=1)
    session, link = _session("attention", sel_id="ACT-0001"), Link()

    _press(session, projection, link, ".", "c")

    assert session.c_target is not None
    assert session.c_target["effects"].startswith("0 questions ")


def test_the_dismiss_verb_is_carried_by_a_daemon_verb() -> None:
    assert binding_refusal("attention", DISMISS_VERB) == ""


# ---------- the keybar keeps help ----------


def test_a_read_model_bar_pins_help_last_however_narrow() -> None:
    pairs = [("↑↓", "tree"), ("Enter", "drill"), ("Tab", "list"), ("g", "go"), (".", "actions")]
    bar = keybar([*pairs, ("?", "help")], 60, keep_actions=True)
    assert bar.rstrip().endswith("? help")
    assert len(bar) == 60


def test_the_prototype_bar_still_drops_help_first() -> None:
    pairs = [("↑↓", "tree"), ("Enter", "drill"), ("Tab", "list"), ("g", "go"), (".", "actions")]
    assert "? help" not in keybar([*pairs, ("?", "help")], 60)


# ---------- overlays let the global keys through ----------


@pytest.mark.parametrize(("key", "lands"), [("/", "palette"), ("?", "help")])
def test_a_global_key_on_a_reading_card_closes_it_and_acts(key: str, lands: str) -> None:
    session = ds.opened("attention", "question", "QST-0001")
    ds.press(session, key, decisions=QUESTIONS)
    assert session.overlay == lands


def test_g_on_a_reading_card_closes_it_and_arms_the_prefix() -> None:
    session = ds.opened("attention", "pause", "PSE-0001")
    ds.press(session, "g", decisions=PAUSES)
    assert session.overlay is None
    assert session.prefix == "g"


def test_a_reply_being_typed_keeps_every_key() -> None:
    session = ds.opened("attention", "question", "QST-0001")
    session.reply = {"text": ""}
    ds.press(session, "?", decisions=QUESTIONS)
    assert session.overlay == "question"
    assert session.reply == {"text": "?"}


# ---------- the Task's proof and due ----------


def _proof(key: str, criterion: str, result: str, ended: str) -> SpineRow:
    return SpineRow(
        key=key,
        urn=f"{ROOT}/receipt/{key}",
        collection=Epoch2Collection.RECEIPT,
        revision=1,
        fields={},
        facts={"criteria": criterion, "result": result, "ended_at": ended},
    )


_FACTS = {
    f"{CRITERION_FACT}1": "CR-1 · behavioral · G1 · first",
    f"{CRITERION_FACT}2": "CR-2 · behavioral · G2 · second",
}


def test_a_criterion_rerun_until_it_passes_counts_as_passing() -> None:
    proofs = [
        _proof("RCP-1", "CR-1", "fail", "2026-10-01T00:00:00Z"),
        _proof("RCP-2", "CR-1", "pass", "2026-10-02T00:00:00Z"),
        _proof("RCP-3", "CR-2", "fail", "2026-10-01T00:00:00Z"),
        _proof("RCP-4", "CR-2", "pass", "2026-10-03T00:00:00Z"),
    ]
    assert (
        _proof_text(_FACTS, proofs)
        == "2 of 2 criteria pass on their newest receipt · 4 receipts filed"
    )


def test_a_criterion_whose_newest_receipt_fails_does_not_pass() -> None:
    proofs = [
        _proof("RCP-1", "CR-1", "pass", "2026-10-01T00:00:00Z"),
        _proof("RCP-2", "CR-1", "fail", "2026-10-02T00:00:00Z"),
    ]
    assert _proof_text(_FACTS, proofs).startswith("0 of 2 criteria pass")


def test_no_receipt_and_no_criterion_each_say_so() -> None:
    assert _proof_text(_FACTS, []).startswith("∅ no proof receipt")
    single = [_proof("RCP-1", "CR-1", "pass", "2026-10-01T00:00:00Z")]
    assert _proof_text({}, single) == "1 receipt filed · the Task states no criterion they prove"


def test_due_names_the_milestone_date_and_title_not_its_id() -> None:
    projection = build_route_projection(
        route="scope.home",
        document={
            "milestone": {
                "MLS-0100": {
                    "key": "MLS-0100",
                    "urn": f"{ROOT}/milestone/MLS-0100",
                    "revision": 1,
                    "status": "ACTIVE",
                    "title": "Ship rc1",
                    "target_date": "2026-10-10",
                }
            }
        },
        cursor=5,
        scope_id="EAWF",
        generated_at=AT,
    )
    view = _view(_session("task.detail"), rows=tuple(projection.rows))
    assert _due_text(view, "MLS-0100") == "2026-10-10 · with MLS-0100 Ship rc1"
    assert _due_text(_view(_session("task.detail")), "MLS-0200") == "with MLS-0200"


# ---------- plain words ----------


@pytest.mark.parametrize(
    ("event", "words"),
    [
        ("domain.run.completed", "run completed"),
        ("question.answered", "answered"),
        ("lifecycle", "lifecycle"),
        ("domain.task.claim_released", "task claim released"),
    ],
)
def test_an_event_name_reads_as_words(event: str, words: str) -> None:
    assert _event_words(event) == words
