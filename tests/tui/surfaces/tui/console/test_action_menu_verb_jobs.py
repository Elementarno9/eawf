"""Every live action-menu verb does its job: it changes the display, or reaches a daemon verb.

A console reading a live tree lists no verb refused for want of a daemon verb. A verb that
only changes what the console shows is light and acts at once in the session's own state;
a verb that writes opens the consequence card of the daemon verb that carries it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from eawf.kernel.projection.compute import ProjectionRow, build_route_projection
from eawf.kernel.projection.settings import build_settings_view
from eawf.kernel.runtime.control import ControlDisposition, ControlKind
from eawf.kernel.state.epoch2.campaign import CampaignStatus
from eawf.runtime.daemon.methods.campaign import CloseParams
from eawf.runtime.daemon.methods.milestone_target import MilestoneTargetRequest
from eawf.surfaces.tui.console.action_menu import (
    Availability,
    MenuVerb,
    Outcome,
    VerbWeight,
    outcome,
)
from eawf.surfaces.tui.console.app import ConsoleApp, compose_frame
from eawf.surfaces.tui.console.cards import CARD, Card
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.in_place import IN_PLACE
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import (
    CAMPAIGN_CLOSE_METHOD,
    DROP_REASON,
    MILESTONE_TARGET_METHOD,
    UNBOUND_REASON,
    CampaignDrop,
    ConsoleOperation,
    ControlRequest,
    DispatchRequest,
    OperationStatus,
    Operator,
    SettingRequest,
    TargetDate,
    VerbRequest,
    address,
    settled,
)
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.activity import changed_since, ordered
from eawf.surfaces.tui.console.renderers.scope_home import groups_of
from eawf.surfaces.tui.console.renderers.spine import held
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session

from . import test_native_route_frames as nrf
from .test_enter_opened_cards import AT

#: The live chrome menus, as a console holding no prototype row lists them.
LINKED = Fixture.from_chrome(load_chrome())
#: The probe tree's root id, as the seam is addressed.
ROOT_ID = "root-0123456789abcdef"
#: Who the probe console acts as.
OPERATOR = Operator(principal="OP-0001")
#: The probe tree's Runs, in the register's order.
LINKED_ACTIVITY_ROWS = build_route_projection(
    route="activity", document=nrf.DOCUMENT, cursor=nrf.CURSOR, scope_id=ROOT_ID, generated_at=AT
).rows


def _linked(route: str, *, subject: str | None = None, sel: str | None = None) -> ConsoleApp:
    """Return a console acting as an operator, linked to ``route``'s probe projection."""
    seam = ProjectionSeam(
        route=route,
        scope_id=ROOT_ID,
        state_path=None,
        clock=lambda: AT,
        scope_name="eawf",
        operator=OPERATOR,
    )
    seam._projection = build_route_projection(
        route=route, document=nrf.DOCUMENT, cursor=nrf.CURSOR, scope_id=ROOT_ID, generated_at=AT
    )
    app = ConsoleApp(chrome=load_chrome(), seam=seam, clock=FakeClock())
    app.session.route = route
    app.session.subj_id = subject
    app.session.sel_id = sel
    return app


def _press(app: ConsoleApp, *keys: str) -> None:
    for key in keys:
        dispatch(app._ctx(), key, False)


def _text(app: ConsoleApp) -> str:
    return "\n".join(compose_frame(app.view()))


def _listed(app: ConsoleApp) -> list[str]:
    """Return the Run keys the Activity window draws, top to bottom."""
    cells = (line[3:].split() for line in _text(app).splitlines())
    return [words[0] for words in cells if words and words[0].startswith("RUN-")]


# ---------- the census ----------


def test_no_live_menu_verb_refuses_for_want_of_a_daemon_verb() -> None:
    refused = [
        f"{route} {verb.key} {verb.verb}"
        for route in LINKED.proto.actions
        for verb in LINKED.menus.verbs(route)
        if UNBOUND_REASON in verb.reason
    ]
    assert not refused, "live verbs no daemon verb carries:\n" + "\n".join(refused)


def test_every_live_heavy_verb_is_offered_or_refused_for_a_reason_of_its_own() -> None:
    for route in LINKED.proto.actions:
        for verb in LINKED.menus.verbs(route):
            if verb.weight is VerbWeight.HEAVY and not verb.available:
                assert verb.reason, (route, verb.verb)


def test_every_light_verb_acting_in_place_has_a_handler_and_every_handler_a_verb() -> None:
    in_place = {
        (route, verb.verb)
        for route in LINKED.proto.actions
        for verb in LINKED.menus.verbs(route)
        if outcome(verb, Availability(True)) is Outcome.IN_PLACE
    }
    assert in_place == set(IN_PLACE)


@pytest.mark.parametrize(
    ("verb", "expected"),
    [
        (MenuVerb(key="s", verb="sort", available=True, weight=VerbWeight.LIGHT), "in_place"),
        (MenuVerb(key="y", verb="copy URN", available=True, weight=VerbWeight.LIGHT), "copy"),
        (
            MenuVerb(key="b", verb="open git", available=True, weight=VerbWeight.LIGHT, target="x"),
            "open",
        ),
    ],
)
def test_a_light_verb_with_no_target_copies_only_when_it_names_a_copy(
    verb: MenuVerb, expected: str
) -> None:
    assert outcome(verb, Availability(True)).value == expected


# ---------- Timeline: propose date writes the Milestone's target date ----------


def _timeline(region: str) -> ConsoleApp:
    """Return a live Timeline console with ``region`` focused, drawn once."""
    app = _linked("roadmap")
    app.session.route = "timeline"
    app.session.tl_reg = region
    _text(app)
    return app


def test_propose_date_needs_an_undated_row_because_a_lane_has_no_cursor() -> None:
    app = _timeline("LANES")
    _press(app, ".", "m")
    assert app.session.edit is None
    assert app.session.trace is not None and "Tab to UNDATED" in app.session.trace


def test_propose_date_types_a_day_and_previews_the_target_write() -> None:
    app = _timeline("UNDATED")
    milestone = (app.session.tl_regs or {})["UNDATED"][app.session.tl_sel][0]
    _press(app, ".", "m", *"2026-10-20")
    frame = _text(app)
    assert f"{milestone} target 2026-10-20" in frame
    assert frame.splitlines()[-1].startswith(" type YYYY-MM-DD")
    _press(app, "Enter")
    card = app.session.mutation
    assert app.session.overlay == CARD and isinstance(card, Card) and card.kind == "target"
    assert card.items[0].request == TargetDate(target=milestone, target_date=date(2026, 10, 20))


@pytest.mark.parametrize("typed", ["", "2026-13-01", "2026-1"])
def test_a_day_that_is_not_a_calendar_day_keeps_the_field_open(typed: str) -> None:
    app = _timeline("UNDATED")
    _press(app, ".", "m", *typed, "Enter")
    assert app.session.edit is not None and app.session.overlay != CARD
    assert app.session.trace is not None and "is not a YYYY-MM-DD day" in app.session.trace


def test_the_date_field_takes_digits_and_dashes_only_and_escape_cancels() -> None:
    app = _timeline("UNDATED")
    _press(app, ".", "m", "2", "x", "0", "Backspace", "-")
    assert app.session.edit is not None and app.session.edit["text"] == "2-"
    _press(app, "Escape")
    assert app.session.edit is None and app.session.overlay is None


def test_a_target_date_is_a_request_the_daemon_verb_accepts() -> None:
    (row, *_rest) = [
        r
        for r in build_route_projection(
            route="roadmap",
            document=nrf.DOCUMENT,
            cursor=nrf.CURSOR,
            scope_id=ROOT_ID,
            generated_at=AT,
        ).rows
        if r.key.startswith("MLS-")
    ]
    day = date(2026, 10, 20)
    sent = address(
        TargetDate(target=row.key, target_date=day), urn=row.urn, revision=4, operator=OPERATOR
    )
    assert isinstance(sent, ConsoleOperation) and sent.method == MILESTONE_TARGET_METHOD
    args = MilestoneTargetRequest.model_validate(dict(sent.params))
    assert args.target_date == day
    assert sent.params["idempotency_key"] == sent.operation_id


def test_a_target_date_answer_reads_as_committed_or_refused() -> None:
    sent = address(
        TargetDate(target="MLS-1", target_date=date(2026, 10, 20)),
        urn="u",
        revision=4,
        operator=OPERATOR,
    )
    assert isinstance(sent, ConsoleOperation)
    done = settled(sent, {"status": "ok", "revision_before": 4, "revision_after": 5})
    assert done.status is OperationStatus.APPLIED
    assert done.detail == "MLS-1 target date 2026-10-20 · revision 4 → 5 · committed"
    no = settled(
        sent,
        {"status": "error", "errors": [{"code": "illegal_transition", "remediation": "reopen"}]},
    )
    assert no.status is OperationStatus.REFUSED
    assert "illegal_transition" in no.detail


# ---------- Activity: sort, follow, pause and mark all, in the session alone ----------


def test_sort_steps_the_window_through_its_columns_and_back() -> None:
    app = _linked("activity", sel="RUN-00000001")
    _text(app)
    seen = []
    for _ in range(5):
        _press(app, ".", "s")
        seen.append(app.session.activity_order)
    assert seen == ["run", "task", "state", "as of", None]
    assert app.session.toasts[-1].text == "register order"


def test_sort_by_state_reorders_the_window_and_says_so() -> None:
    app = _linked("activity", sel="RUN-00000001")
    _text(app)
    _press(app, ".", "s", ".", "s", ".", "s")
    frame = _text(app)
    assert "ORDER" in frame and "state · . s re-orders" in frame
    assert _listed(app) == ["RUN-00000003", "RUN-00000002", "RUN-00000001"]


def _moved(row: ProjectionRow, at: str) -> ProjectionRow:
    return row.model_copy(update={"facts": {**row.facts, "updated_at": at}})


def test_follow_lists_the_newest_change_first_and_an_unstated_one_last() -> None:
    rows = list(LINKED_ACTIVITY_ROWS)
    rows[1] = _moved(rows[1], "2026-09-17T12:05:00+00:00")
    rows[2] = _moved(rows[2], "2026-09-17T12:01:00+00:00")
    assert [row.key for row in ordered(rows, "follow")] == [
        "RUN-00000002",
        "RUN-00000003",
        "RUN-00000001",
    ]


def test_follow_holds_the_cursor_on_the_top_row_until_pressed_again() -> None:
    app = _linked("activity", sel="RUN-00000003")
    _text(app)
    _press(app, ".", "f")
    assert "newest change first, following" in _text(app)
    assert app.session.sel_id == _listed(app)[0]
    _press(app, ".", "f")
    assert app.session.activity_order is None
    assert app.session.toasts[-1].text == "following stopped"


def test_pause_display_holds_the_rows_and_counts_what_changed_since() -> None:
    app = _linked("activity", sel="RUN-00000001")
    _text(app)
    _press(app, ".", "p")
    _text(app)
    seam = app.seam
    assert seam is not None
    (route,) = seam.held_routes
    live = seam.projection_for(route)
    assert live is not None
    seam._hold(route, live.model_copy(update={"rows": live.rows[:-1]}))
    frame = _text(app)
    assert "RUN-00000003" in _listed(app), "a paused window keeps the rows it held"
    assert "PAUSED" in frame and "1 run changed since" in frame
    _press(app, ".", "p")
    assert "RUN-00000003" not in _listed(app)
    assert app.session.activity_held is None


@pytest.mark.parametrize(
    ("held", "live", "moved"),
    [
        ((), (), 0),
        (("a", 1), ("a", 1), 0),
        (("a", 1), ("a", 2), 1),
        (("a", 1), (), 1),
        ((), ("a", 1), 1),
    ],
)
def test_changed_since_counts_moved_added_and_gone_runs(
    held: tuple[str, int] | tuple[()], live: tuple[str, int] | tuple[()], moved: int
) -> None:
    base = LINKED_ACTIVITY_ROWS[0]

    def rows(spec: tuple[str, int] | tuple[()]) -> list[ProjectionRow]:
        return [base.model_copy(update={"key": spec[0], "revision": spec[1]})] if spec else []

    assert changed_since(rows(held), rows(live)) == moved


def test_select_all_shown_marks_the_register_where_no_lifecycle_verb_claims_it() -> None:
    app = _linked("activity", sel="RUN-00000003")
    _text(app)
    _press(app, ".", "*")
    assert app.session.marked == ["RUN-00000001", "RUN-00000002", "RUN-00000003"]


# ---------- Activity: the selected Run's controls reach the control-request verb ----------


@pytest.mark.parametrize(
    ("letter", "control"), [("c", ControlKind.CANCEL), ("n", ControlKind.RECONCILE)]
)
def test_a_run_control_on_the_register_previews_the_run_under_the_cursor(
    letter: str, control: ControlKind
) -> None:
    app = _linked("activity", sel="RUN-00000002")
    _text(app)
    _press(app, ".", letter)
    card = app.session.mutation
    assert app.session.overlay == CARD and isinstance(card, Card)
    assert card.items[0].request == ControlRequest(target="RUN-00000002", control=control)


# ---------- scope home: pin outcome ----------


def test_pin_outcome_leads_the_tree_with_the_cursors_track_and_unpins() -> None:
    app = _linked("scope.home", sel="MLS-0100")
    _text(app)
    _press(app, ".", "p")
    assert app.session.pinned_track == "TRK-CORE"
    assert "TRK-CORE leads your tree" in _text(app)
    _press(app, ".", "p")
    assert app.session.pinned_track is None
    assert "PINNED" not in _text(app)


def test_pin_outcome_refuses_a_milestone_filed_under_no_held_track() -> None:
    app = _linked("scope.home", sel="MLS-0900")
    _text(app)
    _press(app, ".", "p")
    assert app.session.pinned_track is None
    assert app.session.trace is not None and "no Track is filed" in app.session.trace


def test_a_pinned_track_sorts_first_and_none_keeps_the_register_order() -> None:
    app = _linked("scope.home", sel="MLS-0100")
    spine = held(app.view())
    assert spine is not None
    core = next(row for row in spine.rows if row.key == "TRK-CORE")
    two = replace(spine, rows=(replace(core, key="TRK-AAA"), *spine.rows))
    assert [g[0].key for g in groups_of(two, None) if g[0]] == ["TRK-AAA", "TRK-CORE"]
    assert [g[0].key for g in groups_of(two, "TRK-CORE") if g[0]] == ["TRK-CORE", "TRK-AAA"]


# ---------- the Track's pause and the Campaign's drop ----------


def test_pause_dispatch_on_a_track_previews_the_queue_pause() -> None:
    app = _linked("track", subject="TRK-CORE")
    _text(app)
    _press(app, ".", "p")
    card = app.session.mutation
    assert isinstance(card, Card) and card.kind == "dispatch"
    assert card.items[0].request == DispatchRequest(verb="pause")


def test_drop_campaign_previews_the_close_of_the_campaign_on_screen() -> None:
    app = _linked("campaign", subject="CAM-0001")
    _text(app)
    _press(app, ".", "x")
    card = app.session.mutation
    assert isinstance(card, Card) and card.kind == "campaign"
    assert card.items[0].request == CampaignDrop(target="CAM-0001")


def test_a_campaign_drop_is_addressed_to_the_close_verb_as_a_cancel() -> None:
    sent = address(CampaignDrop(target="CAM-0001"), urn="urn:c", revision=3, operator=OPERATOR)
    assert isinstance(sent, ConsoleOperation)
    assert sent.method == CAMPAIGN_CLOSE_METHOD
    assert dict(sent.params) == {
        "urn": "urn:c",
        "expected_revision": 3,
        "actor": OPERATOR.principal,
        "to_status": "cancelled",
        "reason": DROP_REASON,
    }
    result = settled(sent, {"record": {"status": "cancelled"}, "committed": True})
    assert result.status is OperationStatus.APPLIED
    assert result.disposition is ControlDisposition.CONFIRMED
    assert "the campaign is cancelled" in result.detail


def test_a_campaign_drop_is_a_close_the_daemon_verb_accepts() -> None:
    (row,) = build_route_projection(
        route="campaign",
        document=nrf.DOCUMENT,
        cursor=nrf.CURSOR,
        scope_id=ROOT_ID,
        generated_at=AT,
    ).rows
    sent = address(CampaignDrop(target=row.key), urn=row.urn, revision=2, operator=OPERATOR)
    assert isinstance(sent, ConsoleOperation)
    args = CloseParams.model_validate(dict(sent.params))
    assert args.to_status is CampaignStatus.CANCELLED
    assert args.reason == DROP_REASON


# ---------- Settings: edit and unset run the lens's own keys ----------


class _Host:
    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        return self._clock

    def quit(self) -> None:
        """End nothing; the tests never quit."""


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "eawf").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    repo = tmp_path / "repo"
    (repo / ".ea" / "local").mkdir(parents=True)
    (repo / ".ea" / "config.yaml").write_text("estimation:\n  eu_basis: tokens\n")
    return repo


def _settings_press(tree: Path, session: Session, keys: list[str]) -> list[VerbRequest]:
    view = build_settings_view(
        workspace=tree,
        repo=tree,
        scope_id="EAWF",
        cursor=1,
        generated_at=datetime(2026, 9, 17, tzinfo=UTC),
        env={},
        branch="probe",
    )
    leaf = view.leaf("estimation.eu_basis")
    assert leaf.section is not None
    session.set_sec = view.sections().index(leaf.section)
    session.set_key = [one.key for one in view.keys_of(leaf.section)].index(leaf.key)
    sent: list[VerbRequest] = []

    def send(request: VerbRequest) -> bool:
        sent.append(request)
        return True

    for key in keys:
        render_route(View(session=session, fixture=LINKED, w=80, h=24, settings=view))
        ctx = Ctx(
            session=session, fixture=LINKED, host=_Host(), w=80, h=24, settings=view, send=send
        )
        dispatch(ctx, key, False)
    return sent


def test_unset_from_the_menu_sends_the_lens_layers_unset(tree: Path) -> None:
    session = Session(route="settings", lens="repo")
    sent = _settings_press(tree, session, [".", "x", "Enter"])
    assert sent == [SettingRequest(target="estimation.eu_basis", layer="repo", unset=True)]


def test_edit_from_the_menu_opens_the_lens_editor(tree: Path) -> None:
    session = Session(route="settings", lens="repo")
    assert _settings_press(tree, session, [".", "e"]) == []
    assert session.edit is not None and session.edit["key"] == "estimation.eu_basis"
