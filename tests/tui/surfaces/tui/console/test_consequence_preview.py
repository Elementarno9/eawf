"""CON-051 to CON-059, CON-061: the linked consequence card previews, then sends, then answers.

A linked console reaches every canonical mutation through one card built from the rows
its daemon link holds. The card states six panes in one order, names what the move will
not do and what happens if the revision moves first, and sends nothing until it is
confirmed. A bulk selection gets one card naming every target and the ones that may not
confirm, and its results stay one row per target. The write gate removes the verbs when
no request can leave and keeps them with a reason when the link refuses.

The link is a recording stand-in, and the rows come from the projection builder the
daemon serves, so the card is judged on the same row shape it reads live.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.attention import attention_mine
from eawf.kernel.projection.compute import ProjectionRow, build_route_projection
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.runtime.control import ControlDisposition
from eawf.kernel.state.epoch2.consequence import (
    CANONICAL_MUTATIONS,
    MUTATIONS_BY_METHOD,
    Refusal,
)
from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.cards import (
    CARD,
    Card,
    GateKind,
    Item,
    answer_card,
    gate,
    lifecycle_card,
    setting_card,
    settle,
)
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.eligibility import FIRST_ANSWER_WINS, eligible_pane
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.mutation import ENTITY_ROUTES, NATIVE_KEYS, verbs_for
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import (
    SAME_VERB,
    AnswerRequest,
    LifecycleRequest,
    OperationResult,
    OperationStatus,
    SettingRequest,
    VerbRequest,
)
from eawf.surfaces.tui.console.paint import Part, paint
from eawf.surfaces.tui.console.renderers.attention import eligibility_line
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.surfaces.tui.console.tokens import TRUTH

from . import test_native_route_bodies as bodies
from .overlay_support import Host, chrome, prototype

WALL = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

AT = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
ROOT = "eawf://EAWF/EAWF/EAWF"
TASK, OTHER, DRAFT = "EAWF-0001", "EAWF-0002", "EAWF-0003"
QUEUED, RUNNING = "RUN-00000001", "RUN-00000002"

#: The six panes every preview draws, in the only order it draws them.
PANES: tuple[str, ...] = ("ACTION", "TARGET", "EFFECTS", "NOT", "IF STALE", "AUTHORITY")


def _row(
    kind: str, key: str, status: str | None, revision: int = 1, **extra: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {"urn": f"{ROOT}/{kind}/{key}", "revision": revision, **extra}
    if status is not None:
        row["status"] = status
    return row


def _document(*, task_revision: int = 3) -> dict[str, Any]:
    return {
        "task": {
            TASK: _row("task", TASK, "PLANNED", task_revision, title="Claim me"),
            OTHER: _row("task", OTHER, "PLANNED", 5),
            DRAFT: _row("task", DRAFT, "DRAFT", 2),
        },
        "run": {
            QUEUED: _row("run", QUEUED, "QUEUED"),
            RUNNING: _row("run", RUNNING, "RUNNING", 4),
        },
        "track": {"TRK-CORE": _row("track", "TRK-CORE", "ACTIVE", 2)},
    }


def _rows(route: str, document: dict[str, Any] | None = None) -> tuple[ProjectionRow, ...]:
    return build_route_projection(
        route=route,
        document=_document() if document is None else document,
        cursor=9,
        scope_id="EAWF",
        generated_at=AT,
    ).rows


class Link:
    """A daemon link that records every request it is handed."""

    def __init__(self) -> None:
        self.sent: list[VerbRequest] = []

    def __call__(self, request: VerbRequest) -> bool:
        self.sent.append(request)
        return True


def _session(route: str, **fields: Any) -> Session:
    session = Session()
    session.route = route
    for name, value in fields.items():
        setattr(session, name, value)
    return session


def _press(
    session: Session,
    *keys: str,
    rows: tuple[ProjectionRow, ...],
    link: Link | None = None,
    fixture: Fixture | None = None,
    host: Host | None = None,
) -> None:
    held = host or Host()
    for key in keys:
        ctx = Ctx(
            session=session,
            fixture=fixture or chrome(),
            host=held,
            w=120,
            h=30,
            send=link,
            rows=rows,
        )
        dispatch(ctx, key, False)


def _frame(session: Session, rows: tuple[ProjectionRow, ...], size: int = 1) -> list[str]:
    w, h = SIZES[size]
    view = View(session=session, fixture=chrome(), w=w, h=h, linked=True, rows=rows)
    return compose_frame(view)


def _labels(frame: list[str]) -> list[str]:
    known = {*PANES, "SELECTED", "UNKNOWN"}
    found = []
    for row in frame[3:-1]:
        head = row[1:11].strip()
        if head in known:
            found.append(head)
    return found


def _card(session: Session) -> Card:
    card = session.mutation
    assert isinstance(card, Card)
    return card


# ---------- CON-051: every canonical mutation previews first ----------


def test_con_051_every_lifecycle_verb_has_a_menu_letter_on_its_routes() -> None:
    assert set(NATIVE_KEYS) == set(MUTATIONS_BY_METHOD)
    assert {m.entity for m in CANONICAL_MUTATIONS} <= set(ENTITY_ROUTES)


def test_con_051_no_lifecycle_letter_collides_with_another_chrome_verb() -> None:
    menus = chrome().menus
    for entity, routes in ENTITY_ROUTES.items():
        letters = [NATIVE_KEYS[m.method] for m in verbs_for(entity)]
        assert len(letters) == len(set(letters)), entity
        for route in routes:
            for verb in menus.verbs(route):
                if verb.key in letters:
                    assert SAME_VERB.get(verb.verb) in {m.method for m in verbs_for(entity)}, (
                        route,
                        verb.key,
                    )


@pytest.mark.parametrize("mutation", CANONICAL_MUTATIONS, ids=lambda m: m.method)
def test_con_051_every_lifecycle_verb_previews_before_anything_is_sent(mutation: Any) -> None:
    status = str(mutation.from_statuses[0])
    kind = mutation.entity.value
    route = sorted(ENTITY_ROUTES[mutation.entity])[0]
    document = {kind: {"KEY-0001": _row(kind, "KEY-0001", status, 7)}}
    rows = _rows(route, document)
    session, link = _session(route, subj_id="KEY-0001"), Link()

    _press(session, ".", NATIVE_KEYS[mutation.method], rows=rows, link=link)

    assert session.overlay == CARD
    assert link.sent == []
    assert _card(session).origin == mutation.method
    assert _labels(_frame(session, rows))[:6] == list(PANES)


def test_con_051_a_settings_edit_previews_in_the_same_six_panes() -> None:
    request = SettingRequest(target="prose.level", layer="repo", value="strict")
    session, link = _session("settings"), Link()
    session.mutation = setting_card(request, effect="strict becomes the value", token="t", now=0)
    session.overlay = CARD
    assert _labels(_frame(session, ()))[:6] == list(PANES)
    assert link.sent == []


def test_con_051_the_prototype_console_keeps_its_golden_card() -> None:
    session, link = _session("task.detail", subj_id=TASK), Link()
    _press(session, ".", "l", rows=_rows("task.detail"), link=link, fixture=prototype())
    assert session.mutation is None


# ---------- CON-052: six panes in one order ----------


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_con_052_the_preview_draws_six_panes_in_the_fixed_order(size: int) -> None:
    rows = _rows("task.detail")
    session = _session("task.detail", subj_id=TASK)
    _press(session, ".", "l", rows=rows, link=Link())
    frame = _frame(session, rows, size)
    assert _labels(frame) == list(PANES)
    text = "\n".join(frame)
    assert "revision 3 · PLANNED · exact" in text
    assert "task EAWF-0001 moves PLANNED → CLAIMED" in text


def test_con_052_a_refused_preview_keeps_its_six_panes_and_offers_only_back() -> None:
    rows = _rows("task.detail")
    session, link = _session("task.detail", subj_id=DRAFT), Link()
    _press(session, ".", "m", rows=rows, link=link)
    frame = _frame(session, rows)
    assert _labels(frame) == list(PANES)
    assert "missing_transition_fields" in frame[1]
    assert "Enter" not in frame[-1]
    _press(session, "Enter", rows=rows, link=link)
    assert link.sent == []


# ---------- CON-053: the NOT pane ----------


@pytest.mark.parametrize("mutation", CANONICAL_MUTATIONS, ids=lambda m: m.method)
def test_con_053_every_verb_names_its_non_effects(mutation: Any) -> None:
    assert mutation.not_effects
    assert all(line.strip() for line in mutation.not_effects)


def test_con_053_the_not_pane_prints_each_non_effect() -> None:
    rows = _rows("task.detail")
    session = _session("task.detail", subj_id=TASK)
    _press(session, ".", "l", rows=rows, link=Link())
    text = "\n".join(_frame(session, rows))
    for line in MUTATIONS_BY_METHOD["domain.task.claim"].not_effects:
        assert line in text


# ---------- CON-054: IF STALE is stated first, then honoured ----------


def test_con_054_the_card_states_the_reload_rule_before_confirmation() -> None:
    rows = _rows("task.detail")
    session = _session("task.detail", subj_id=TASK)
    _press(session, ".", "l", rows=rows, link=Link())
    text = " ".join(" ".join(_frame(session, rows, 2)).split())
    assert "if revision 3 moves before you confirm, this card reloads" in text
    assert "authorization is withdrawn, never re-targeted" in text


def test_con_054_a_moved_revision_reloads_the_card_and_sends_nothing() -> None:
    rows, moved = _rows("task.detail"), _rows("task.detail", _document(task_revision=4))
    session, link = _session("task.detail", subj_id=TASK), Link()
    _press(session, ".", "l", rows=rows, link=link)
    first = _card(session).items[0].request

    _press(session, "Enter", rows=moved, link=link)

    card = _card(session)
    assert link.sent == []
    assert card.results == ()
    assert card.items[0].revision == 4
    assert "withdrawn" in card.note
    reloaded = card.items[0].request
    assert isinstance(first, LifecycleRequest) and isinstance(reloaded, LifecycleRequest)
    assert reloaded.operation_id != first.operation_id


# ---------- CON-055: repeat suppression by operation id and revision ----------


def test_con_055_a_repeat_at_an_unchanged_revision_is_sent_again_unprompted() -> None:
    rows = _rows("task.detail")
    session, link = _session("task.detail", subj_id=TASK), Link()
    _press(session, ".", "l", "Enter", "Escape", ".", "l", rows=rows, link=link)

    first, second = link.sent
    assert isinstance(first, LifecycleRequest) and isinstance(second, LifecycleRequest)
    assert second.operation_id == first.operation_id
    assert _card(session).results


def test_con_055_a_changed_revision_always_prompts_under_a_new_id() -> None:
    rows, moved = _rows("task.detail"), _rows("task.detail", _document(task_revision=4))
    session, link = _session("task.detail", subj_id=TASK), Link()
    _press(session, ".", "l", "Enter", "Escape", rows=rows, link=link)
    _press(session, ".", "l", rows=moved, link=link)

    assert len(link.sent) == 1
    card = _card(session)
    assert card.results == ()
    request = card.items[0].request
    first = link.sent[0]
    assert isinstance(request, LifecycleRequest) and isinstance(first, LifecycleRequest)
    assert request.operation_id != first.operation_id


# ---------- CON-056 and CON-057: one bulk preview, naming every target ----------


def test_con_056_marking_rows_gives_one_preview_naming_every_target() -> None:
    rows = _rows("backlog")
    session, link = _session("backlog"), Link()
    for key in (TASK, OTHER, DRAFT):
        session.sel_id = key
        _press(session, " ", rows=rows, link=link)
    _press(session, ".", "l", rows=rows, link=link)

    frame = _frame(session, rows)
    text = "\n".join(frame)
    assert session.marked == [TASK, OTHER, DRAFT]
    assert "claim · 3 tasks" in text
    assert all(key in text for key in (TASK, OTHER, DRAFT))
    assert "Enter confirm all 2" in frame[-1]
    assert link.sent == []


def test_con_056_register_root_selects_every_row_and_comma_clears() -> None:
    rows = _rows("activity")
    session = _session("activity", sel_id=QUEUED)
    _press(session, ".", "*", rows=rows)
    assert session.overlay is None
    assert session.marked == [QUEUED, RUNNING]
    _press(session, ",", rows=rows)
    assert session.marked == []


def test_con_056_a_selection_too_long_for_the_frame_is_still_named_in_full() -> None:
    tasks = {f"EAWF-{n:04d}": _row("task", f"EAWF-{n:04d}", "PLANNED", n) for n in range(1, 31)}
    rows = _rows("backlog", {"task": tasks})
    session = _session("backlog", marked=list(tasks))
    session.mutation = lifecycle_card(
        MUTATIONS_BY_METHOD["domain.task.claim"], rows, principal="you", now=0.0
    )
    session.overlay = CARD
    text = "\n".join(_frame(session, rows, 0))
    assert all(key in text for key in tasks)


def test_con_057_the_bulk_preview_names_the_targets_that_may_not_confirm() -> None:
    rows = _rows("activity")
    card = lifecycle_card(MUTATIONS_BY_METHOD["domain.run.fail"], rows, principal="", now=0.0)
    session = _session("activity", mutation=card, overlay=CARD)
    text = " ".join(" ".join(_frame(session, rows, 2)).split())
    assert "UNKNOWN" in text
    assert f"{RUNNING} is RUNNING, whose outcome is not yet observed" in text


def test_con_057_a_bulk_preview_with_no_doubt_says_so() -> None:
    rows = _rows("backlog")
    card = lifecycle_card(MUTATIONS_BY_METHOD["domain.task.claim"], rows[:2], principal="", now=0.0)
    session = _session("backlog", mutation=card, overlay=CARD)
    assert "none · every target states its status" in "\n".join(_frame(session, rows))


# ---------- CON-058: one row in, one row out ----------


def _confirmed_bulk(link: Link) -> tuple[Session, tuple[ProjectionRow, ...]]:
    rows = _rows("backlog")
    session = _session("backlog", marked=[TASK, OTHER, DRAFT])
    _press(session, ".", "l", "Enter", rows=rows, link=link)
    return session, rows


def test_con_058_every_target_keeps_its_own_result_row() -> None:
    link = Link()
    session, rows = _confirmed_bulk(link)
    card = _card(session)
    assert [row.key for row in card.results] == [TASK, OTHER, DRAFT]
    assert [row.disposition.value for row in card.results] == ["requesting", "requesting", "idle"]
    assert len(link.sent) == 2

    sent = {request.target: request for request in link.sent}
    first, second = sent[TASK], sent[OTHER]
    assert isinstance(first, LifecycleRequest) and isinstance(second, LifecycleRequest)
    settle(
        session,
        OperationResult(
            operation_id=first.operation_id,
            target=TASK,
            status=OperationStatus.APPLIED,
            disposition=ControlDisposition.CONFIRMED,
            detail="committed",
            revision=4,
        ),
        now=1000.5,
    )
    settle(
        session,
        OperationResult(
            operation_id=second.operation_id,
            target=OTHER,
            status=OperationStatus.OUTSTANDING,
            disposition=ControlDisposition.UNKNOWN,
            detail="no answer yet",
        ),
        now=1000.6,
    )
    card = _card(session)
    assert [row.disposition.value for row in card.results] == ["confirmed", "unknown", "idle"]
    unknown = card.results[1]
    assert (unknown.accepted, unknown.confirmed) == ("—", "—")
    frame = _frame(session, rows)
    assert "claim · 3 requested · 1 confirmed · 1 unknown · 1 idle" in frame[1]
    assert "n reconcile the unknown" in frame[-1]


def test_con_058_reconcile_asks_again_for_the_unknown_row_under_its_own_id() -> None:
    link = Link()
    session, rows = _confirmed_bulk(link)
    second = next(r for r in link.sent if r.target == OTHER)
    assert isinstance(second, LifecycleRequest)
    settle(
        session,
        OperationResult(
            operation_id=second.operation_id,
            target=OTHER,
            status=OperationStatus.OUTSTANDING,
            disposition=ControlDisposition.UNKNOWN,
            detail="no answer yet",
        ),
        now=1000.2,
    )
    _press(session, "ArrowDown", "n", rows=rows, link=link)
    again = link.sent[-1]
    assert isinstance(again, LifecycleRequest)
    assert again.operation_id == second.operation_id
    assert _card(session).results[1].disposition is ControlDisposition.UNKNOWN


def test_con_058_reconcile_on_a_settled_row_sends_nothing() -> None:
    link = Link()
    session, rows = _confirmed_bulk(link)
    _press(session, "ArrowDown", "ArrowDown", "n", rows=rows, link=link)
    assert len(link.sent) == 2


# ---------- CON-059: transport loss removes, refusal explains ----------


def _drawer(conn: str) -> list[str]:
    rows = _rows("task.detail")
    session = _session("task.detail", subj_id=TASK, conn=conn, overlay="actions")
    return _frame(session, rows)


def test_con_059_transport_loss_removes_the_lifecycle_verbs() -> None:
    text = "\n".join(_drawer("DISCONNECTED"))
    assert "claim" not in text
    assert "not offered" in text


def test_con_059_a_gate_refusal_keeps_the_verbs_with_their_reason() -> None:
    text = "\n".join(_drawer("GAP DETECTED"))
    assert "claim" in text
    assert "not offered" not in text


def test_con_059_the_two_refusals_come_from_the_one_gate_and_differ() -> None:
    fixture, lost, gap = chrome(), _session("x", conn="DISCONNECTED"), _session("x", conn="GAP")
    gap.conn = "GAP DETECTED"
    assert gate(lost, fixture, verb="claim", linked=True).kind is GateKind.TRANSPORT
    assert gate(gap, fixture, verb="claim", linked=True).kind is GateKind.REFUSED
    assert gate(gap, fixture, verb="claim", linked=False).kind is GateKind.TRANSPORT
    assert _drawer("DISCONNECTED") != _drawer("GAP DETECTED")


def test_con_059_a_removed_verb_opens_no_card() -> None:
    rows = _rows("task.detail")
    session, link = _session("task.detail", subj_id=TASK, conn="DISCONNECTED"), Link()
    _press(session, ".", "l", rows=rows, link=link)
    assert session.overlay != CARD
    assert "not offered" in session.log[0].note


# ---------- CON-061: a losing answer renders superseded ----------


def _superseded_session() -> tuple[Session, tuple[ProjectionRow, ...]]:
    rows = _rows(
        "attention",
        {"pending_action": {"ACT-0001": _row("pending-action", "ACT-0001", "WAITING", 3)}},
    )
    card = answer_card(rows[0], "a", principal="you", now=1000.0, wall=WALL, rows=rows)
    session = _session("attention", overlay=CARD)
    session.mutation = card
    link = Link()
    _press(session, "Enter", rows=rows, link=link)
    settle(
        session,
        OperationResult(
            operation_id="console-1",
            target="ACT-0001",
            status=OperationStatus.SUPERSEDED,
            disposition=ControlDisposition.SUPERSEDED,
            detail="ACT-0001 was already answered · this answer is superseded",
            revision=4,
            answered_by="OP-0002",
        ),
        now=1000.4,
    )
    return session, rows


def test_con_061_a_superseded_answer_names_both_revisions_and_the_winner() -> None:
    session, rows = _superseded_session()
    text = " ".join(" ".join(_frame(session, rows, 2)).split())
    assert _card(session).results[0].disposition is ControlDisposition.SUPERSEDED
    assert "preview built at revision 3 · exact" in text
    assert "answered at revision 4 · exact · by OP-0002" in text
    assert "neither applied nor refused" in text
    assert "TRACE previewed +0.0s · requested +0.0s · answered +0.4s" in text


def test_con_061_the_word_superseded_is_left_unpainted() -> None:
    session, rows = _superseded_session()
    frame = _frame(session, rows, 2)
    row = next(r for r in frame if r.rstrip().endswith("superseded"))
    at, start = row.rindex("superseded"), 0
    for stroke in paint(row, Part.BODY):
        if start <= at < start + len(stroke.text):
            assert stroke.surface is None, stroke
            break
        start += len(stroke.text)
    else:
        pytest.fail("the word was not painted at all")


# ---------- the key tables declare the card's and the selection's keys ----------


def test_con_056_the_selection_keys_are_declared_where_the_menu_offers_bulk() -> None:
    from eawf.surfaces.tui.console.keymap import GLOBAL_KEYS, SELECTION_ROUTES, acts_here

    # the Attention route marks its questions for one dismissal, beside the lifecycle routes
    assert frozenset().union(*ENTITY_ROUTES.values()) | {"attention"} == SELECTION_ROUTES
    selection = [key for key in GLOBAL_KEYS if set(key.keys) & {" ", "*", ","}]
    assert {k for key in selection for k in key.keys} == {" ", ","}
    assert all(acts_here(key, "backlog", linked=True) for key in selection)
    assert not any(acts_here(key, "backlog") for key in selection)


def test_con_058_the_card_keys_are_the_allowlist_while_a_card_is_held() -> None:
    from eawf.surfaces.tui.console.keymap import CARD_KEYS, allowlist

    link = Link()
    session, rows = _confirmed_bulk(link)
    assert allowlist(session, chrome()) >= CARD_KEYS
    _press(session, "x", rows=rows, link=link)
    assert len(link.sent) == 2


# ---------- a target is sent exactly when it is not refused ----------


def _item(**fields: Any) -> Item:
    return Item(
        key="EAWF-0001",
        title=None,
        revision=1,
        status="DRAFT",
        effects=(),
        not_effects=(),
        unknown="",
        stale_token="1",
        **fields,
    )


def test_item_neither_sent_nor_refused_raises_value_error() -> None:
    with pytest.raises(ValueError, match="sent exactly when it is not refused"):
        _item(refusal=None, request=None)


def test_item_both_sent_and_refused_raises_value_error() -> None:
    refusal = Refusal(code="x", reason="no", remediation="none")
    request = AnswerRequest(target="EAWF-0001", option_id="approve")
    with pytest.raises(ValueError, match="sent exactly when it is not refused"):
        _item(refusal=refusal, request=request)


def test_item_why_names_the_refusal_and_is_empty_when_sent() -> None:
    refusal = Refusal(code="x", reason="no status", remediation="wait")
    assert _item(refusal=refusal, request=None).why == "no status"
    request = AnswerRequest(target="EAWF-0001", option_id="approve")
    assert _item(refusal=None, request=request).why == ""


# ---------- CON-062: eligibility is stated beside the action, never counted ----------


def _attention_rows() -> dict[str, ProjectionRow]:
    rows = bodies._projection("attention").rows
    return {row.key: row for row in rows}


def test_con_062_an_action_several_principals_may_answer_says_so_on_its_row() -> None:
    row = _attention_rows()["ACT-0001"]
    assert eligibility_line(row, bodies.ME, 2) == "you may answer"


def test_con_062_a_sole_eligible_principal_is_told_it_is_the_only_answer() -> None:
    row = _attention_rows()["ACT-0001"]
    assert eligibility_line(row, bodies.ME, 1) == "you are the only eligible answer"
    assert eligibility_line(row, bodies.ME, 0) == "you are the only eligible answer"


def test_con_062_an_action_addressed_to_another_principal_names_them_not_a_count() -> None:
    row = _attention_rows()["ACT-0002"]
    line = eligibility_line(row, bodies.ME, 2)
    assert line.startswith(f"{bodies.OTHER} only")


def test_con_062_with_no_principal_the_row_says_how_to_become_one() -> None:
    row = _attention_rows()["ACT-0001"]
    assert eligibility_line(row, None, 2).startswith("no principal is named")


def test_con_062_eligibility_moves_no_attention_count() -> None:
    alone = build_register_view(bodies._projection("attention"))
    shared = build_register_view(bodies._projection("attention", _second_eligible()))
    mine = attention_mine(alone, principal=bodies.ME)
    assert attention_mine(shared, principal=bodies.ME).value == mine.value


def _second_eligible() -> dict[str, Any]:
    document = {**bodies.DOCUMENT, "pending_action": dict(bodies.DOCUMENT["pending_action"])}
    document["pending_action"]["ACT-0004"] = bodies._action(
        "ACT-0004", "WAITING", "run/RUN-00000003", assignee_ref=bodies.OTHER
    )
    return document


def test_con_062_the_eligible_pane_names_each_principal_and_that_the_first_answer_wins() -> None:
    rows = tuple(_attention_rows().values())
    card = answer_card(
        _attention_rows()["ACT-0001"], "a", principal=bodies.ME, now=1000.0, wall=WALL, rows=rows
    )
    assert FIRST_ANSWER_WINS in card.eligible
    heads, *principals = card.eligible[:-2]
    assert heads.split() == ["PRINCIPAL", "CLASS", "LAST", "ACTED", "STATE"]
    assert [line.split()[0] for line in principals] == sorted({bodies.ME, bodies.OTHER})
    assert all(" operator " in line for line in principals)


def test_con_062_one_eligible_principal_draws_no_pane() -> None:
    only = _attention_rows()["ACT-0001"]
    card = answer_card(only, "a", principal=bodies.ME, now=1000.0, wall=WALL, rows=(only,))
    assert card.eligible == ()


def test_con_070_a_principal_who_never_acted_has_an_empty_cell_on_the_live_card() -> None:
    row = _attention_rows()["ACT-0001"].model_copy(
        update={
            "facts": {
                **_attention_rows()["ACT-0001"].facts,
                f"answered.{bodies.OTHER}": "superseded decline",
                f"acted.{bodies.OTHER}": "2026-09-30T11:05:00+00:00",
            }
        }
    )
    pane = eligible_pane(row, frozenset({bodies.ME, bodies.OTHER}), bodies.ME)
    mine = next(line for line in pane if line.startswith(f"{bodies.ME} (you)"))
    other = next(line for line in pane if line.startswith(bodies.OTHER))
    # never acted: the LAST ACTED and STATE cells are empty, no token borrowed
    assert mine.split() == [bodies.ME, "(you)", "operator"]
    assert TRUTH["unavailable"].unicode not in mine
    assert other.split()[1:] == ["operator", "11:05", "UTC", "superseded", "·", "decline"]
    assert pane[-1] == "every cell is per principal · an empty cell means never acted"
