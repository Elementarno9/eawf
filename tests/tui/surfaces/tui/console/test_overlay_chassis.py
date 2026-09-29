"""The overlay and drawer chassis: the closed sets, their frames, their gates and their exits.

The rows proven here are CON-091 (the eleven overlays and four drawers, one subject per
renderer), CON-092 (an overlay or drawer sits above a route and returns to it), CON-095
(the same facts at 80, 120 and 160 columns), CON-096 (nothing arriving opens an overlay),
CON-099 (the resolution card's five endings), CON-101 (an overlay inherits the connection
gate) and CON-102 (dismissing is never a resolution). The live-path cases open each
overlay and drawer over a route a daemon link holds and assert the overlay draws itself
rather than the unknown frame.
"""

from __future__ import annotations

import ast
import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.projection.compute import RouteProjection, build_route_projection
from eawf.kernel.runtime.control import ControlDisposition
from eawf.surfaces.tui.console import attention as att
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.app import ConsoleApp, compose_frame
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import unheld
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.keymap import DRAWER_KEYS, DRAWER_PAIRS, OVERLAY_KEYS
from eawf.surfaces.tui.console.navigation import Ctx, close_overlay, open_overlay
from eawf.surfaces.tui.console.operations import OperationResult, OperationStatus
from eawf.surfaces.tui.console.overlays import OVERLAY_RENDERERS, render_overlay
from eawf.surfaces.tui.console.overlays.resolution import ENDINGS, Ending
from eawf.surfaces.tui.console.palette import HitKind, hits
from eawf.surfaces.tui.console.reads import MUTABLE, mut_reason
from eawf.surfaces.tui.console.registry import DRAWERS, OVERLAYS, REGISTRY, SURFACES
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from eawf.surfaces.tui.console.tokens import CONNECTION, RULE_HEAVY, RULE_THIN, TRUTH

from .overlay_support import (
    Host,
    Link,
    chrome,
    press,
    prototype,
    session_on,
    surface_of,
    view_of,
)

CONSOLE = Path(dv.__file__).parent
AT = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
UNKNOWN = TRUTH["unknown"].unicode
RUN = "RUN-538453eb"
#: The connection values that refuse a write, each with a reason the chrome names.
REFUSING: tuple[str, ...] = tuple(conn for conn in CONNECTION if conn not in MUTABLE)
#: Every drawer the session opens as its overlay; the go drawer is the armed prefix.
ROW_DRAWERS: tuple[str, ...] = tuple(d for d in DRAWERS if d != "go")


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the prototype registers the golden contract replays."""
    return prototype()


def _register(fixture: Fixture) -> list[tuple[str, str]]:
    return [(a.id, a.state) for a in fixture.proto.attention]


def _held_seam(route: str) -> ProjectionSeam:
    projection: RouteProjection = build_route_projection(
        route=route,
        document={
            "track": {
                "TRK-7001": {
                    "urn": f"urn:eawf:{SCOPE}:track:TRK-7001",
                    "revision": 1,
                    "status": "ACTIVE",
                }
            }
        },
        cursor=7,
        scope_id=SCOPE,
        generated_at=AT,
    )
    seam = ProjectionSeam(route=route, scope_id=SCOPE, state_path=None, clock=lambda: AT)
    seam._projection = projection
    return seam


# ---------- CON-091: the overlay set is closed at eleven and the drawer set at four ----------


def test_con_091_the_overlay_set_is_closed_at_the_eleven_it_names() -> None:
    assert OVERLAYS == (
        "help",
        "palette",
        "consequence",
        "question",
        "pause",
        "evidence",
        "acceptance",
        "readiness",
        "resolution",
        "draft",
        "marker",
    )
    assert set(OVERLAY_RENDERERS) == set(OVERLAYS) == set(OVERLAY_KEYS)


def test_con_091_the_drawer_set_is_closed_at_the_four_it_names() -> None:
    assert DRAWERS == ("go", "actions", "inspect", "raw")
    assert set(DRAWER_KEYS) == set(DRAWER_PAIRS) == set(DRAWERS)
    assert set(SURFACES) == {*OVERLAYS, *DRAWERS}


def test_con_091_an_entity_sub_surface_is_a_route_and_never_an_overlay() -> None:
    subs: tuple[str, ...] = (
        "evidence.digest",
        "settings.stack",
        "merge.conflict",
        "export",
        "receipt",
        "campaign.step",
        "campaign.artifact",
    )
    for route in subs:
        assert route in REGISTRY.by_id
        assert route not in SURFACES


@pytest.mark.parametrize("name", ["evidence", "acceptance"])
def test_con_091_one_renderer_draws_one_subject_whatever_route_opened_it(
    name: str, fixture: Fixture
) -> None:
    frames = {
        tuple(render_overlay(name, view_of(session_on(route, overlay=name), fixture)))
        for route in ("campaign", "milestone", "activity")
    }
    assert len(frames) == 1


def test_con_091_the_evidence_viewer_and_the_acceptance_evidence_are_two_subjects(
    fixture: Fixture,
) -> None:
    claim = render_overlay("evidence", view_of(session_on("campaign", overlay="evidence"), fixture))
    bundle = render_overlay("acceptance", view_of(session_on("milestone"), fixture))
    assert "Eä ▸ evidence · EVD-0011" in claim[0]
    assert "Eä ▸ acceptance evidence · MLS-0004" in bundle[0]
    assert "Reading evidence does not accept the milestone." in "\n".join(bundle)
    assert "it seals no digest" in "\n".join(claim)


def test_con_091_milestone_enter_opens_the_acceptance_evidence_not_the_evidence_viewer(
    fixture: Fixture,
) -> None:
    session = session_on("milestone", subj_id="MLS-0004")
    # a milestone frame with no batch row to drill leaves Enter to its evidence
    ctx = Ctx(session=session, fixture=fixture, host=Host(), w=120, h=30)
    dispatch(ctx, "Enter", False)
    assert (session.overlay, session.ov_subject) == ("acceptance", "MLS-0004")


def test_con_091_an_overlay_the_registry_does_not_name_is_refused(fixture: Fixture) -> None:
    with pytest.raises(KeyError):
        render_overlay("provenance", view_of(session_on("activity"), fixture))
    with pytest.raises(KeyError, match="no overlay or drawer is named"):
        open_overlay(session_on("activity"), "provenance")


# ---------- CON-092: an overlay or drawer sits above a route and returns to it ----------


@pytest.mark.parametrize("name", [*OVERLAYS, *ROW_DRAWERS])
def test_con_092_escape_returns_to_the_route_subject_row_and_back_stack(
    name: str, fixture: Fixture
) -> None:
    session = session_on("run.detail", subj_id=RUN, sel=2)
    session.back.push(route="activity", sel=4, subj=None)
    open_overlay(session, name, subject="ACT-0031")
    assert (session.route, session.subj_id, len(session.back)) == ("run.detail", RUN, 1)
    session.sel = 0
    ctx = Ctx(session=session, fixture=fixture, host=Host(), w=120, h=30)
    dispatch(ctx, "Escape", False)
    assert session.overlay is None
    assert (session.route, session.subj_id, session.sel, len(session.back)) == (
        "run.detail",
        RUN,
        2,
        1,
    )


def test_con_092_replacing_the_top_overlay_keeps_the_row_it_began_from() -> None:
    session = session_on("attention", sel=3)
    open_overlay(session, "question", subject="ACT-0032")
    session.sel = 0
    open_overlay(session, "consequence", subject="ACT-0032")
    assert close_overlay(session) == "consequence"
    assert (session.overlay, session.sel) == (None, 3)


def test_con_092_closing_with_nothing_open_changes_nothing() -> None:
    session = session_on("activity", sel=5)
    assert close_overlay(session) is None
    assert session.sel == 5


def test_con_092_no_overlay_or_drawer_is_a_route_node_or_a_palette_door() -> None:
    only_surfaces = set(SURFACES) - set(REGISTRY.ids)
    assert {"acceptance", "consequence", "question", "actions", "inspect"} <= only_surfaces
    assert not only_surfaces & set(REGISTRY.route_list)
    assert not only_surfaces & set(REGISTRY.go_map.values())
    assert not only_surfaces & set(REGISTRY.escapes)
    route_rows = {hit.route for hit in hits("", []) if hit.kind is HitKind.ROUTE}
    assert not only_surfaces & route_rows


@pytest.mark.parametrize("name", OVERLAYS)
def test_con_092_the_address_is_the_routes_while_an_overlay_is_open(
    name: str, fixture: Fixture
) -> None:
    session = session_on("run.detail", subj_id=RUN)
    before = dv.urn(session, fixture)
    open_overlay(session, name, subject="CLM-0004")
    assert dv.urn(session, fixture) == before


# ---------- CON-095: the same facts at 80, 120 and 160 ----------


def _facts(rows: list[str]) -> set[str]:
    """Return every word the frame body states, rules, padding and window edge counts aside.

    An edge count says how many rows a window hides, which a wider frame changes by
    showing more of them; it is a count and not a row.
    """
    words: set[str] = set()
    for row in rows[1:-1]:
        if row.strip(f" {RULE_HEAVY}{RULE_THIN}┄") and not row.startswith("   … "):
            words.update(row.split())
    return words


@pytest.mark.parametrize("name", OVERLAYS)
def test_con_095_every_overlay_states_the_same_facts_at_80_120_and_160(
    name: str, fixture: Fixture
) -> None:
    facts = [
        _facts(compose_frame(view_of(session_on("activity", overlay=name), fixture, size)))
        for size in range(len(SIZES))
    ]
    if name == "palette":
        # a wider palette lists more rows of the list already present, and nothing else
        assert facts[0] <= facts[1] <= facts[2]
    else:
        assert facts[0] == facts[1] == facts[2]


@pytest.mark.parametrize("name", OVERLAYS)
def test_con_095_every_overlay_holding_nothing_has_its_80_column_form(name: str) -> None:
    frames = [
        compose_frame(view_of(session_on("activity", overlay=name), chrome(), size))
        for size in range(len(SIZES))
    ]
    assert [len(rows) for rows in frames] == [h for _w, h in SIZES]
    assert _facts(frames[0]) == _facts(frames[1]) == _facts(frames[2])


# ---------- CON-096: no overlay is opened by an arriving event ----------


def test_con_096_a_daemon_answer_a_toast_and_a_tick_open_no_overlay(fixture: Fixture) -> None:
    async def drive() -> tuple[str | None, int]:
        app = ConsoleApp(fixture, FakeClock())
        async with app.run_test(size=SIZES[1]):
            app.reset(SessionSetup(route="attention", size=1))
            app.announce(
                OperationResult(
                    operation_id=None,
                    target="ACT-0031",
                    status=OperationStatus.OUTSTANDING,
                    detail="unknown",
                    disposition=ControlDisposition.UNKNOWN,
                )
            )
            app.raise_toast("a notice arrived", title="notice")
            app.tick()
            app.render_frame()
            return app.session.overlay, app.session.auto_opens

    assert asyncio.run(drive()) == (None, 0)


_ASSIGNS_OVERLAY = re.compile(r"\.overlay\s*=(?!=)")
#: The modules on the key path allowed to open an overlay: navigation's open, the action
#: menu's toggle and one route key hook.
KEY_PATH_OPENERS: frozenset[str] = frozenset(
    {"navigation.py", "action_menu.py", "renderers/unattended.py"}
)


def _overlay_openers() -> set[str]:
    """Return every console module that sets ``.overlay`` to something other than ``None``."""
    found: set[str] = set()
    for path in CONSOLE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                sets_overlay = isinstance(target, ast.Attribute) and target.attr == "overlay"
                clears = isinstance(node.value, ast.Constant) and node.value.value is None
                if sets_overlay and not clears:
                    found.add(path.relative_to(CONSOLE).as_posix())
    return found


def test_con_096_only_the_key_path_opens_an_overlay() -> None:
    openers = _overlay_openers()
    assert openers, "the scan is vacuous: nothing opens an overlay"
    assert openers <= KEY_PATH_OPENERS
    for arrival in ("app.py", "seam.py", "clock.py", "toast_emitter.py", "attention.py"):
        assert arrival not in openers


# ---------- CON-099: the resolution card's five endings ----------


def test_con_099_the_endings_are_exactly_the_five() -> None:
    assert [e.value for e in Ending] == ["missing", "moved", "retired", "denied", "purged"]
    assert set(ENDINGS) == set(Ending)


@pytest.mark.parametrize("ending", list(Ending))
@pytest.mark.parametrize("size", range(len(SIZES)))
def test_con_099_each_ending_names_what_it_means_and_what_remains(
    ending: Ending, size: int
) -> None:
    session = session_on(
        "history", overlay="resolution", ov_subject="EAWF-0099", resolution_ending=ending.value
    )
    rows = compose_frame(view_of(session, chrome(), size))
    text = "\n".join(rows)
    assert "Eä ▸ resolution · EAWF-0099" in rows[0]
    assert f" ENDING    {ENDINGS[ending].ending}" in text
    assert ENDINGS[ending].means in text
    assert ENDINGS[ending].works in text
    assert ENDINGS[ending].not_works in text
    assert "NOT HELD" not in text
    not_row = next(row for row in rows if row.startswith(" NOT "))
    for other in Ending:
        assert (other.value in not_row) is (other is not ending)


def test_con_099_an_ending_outside_the_five_is_refused(fixture: Fixture) -> None:
    session = session_on(
        "history", overlay="resolution", ov_subject="EAWF-0099", resolution_ending="lost"
    )
    with pytest.raises(ValueError, match="lost"):
        render_overlay("resolution", view_of(session, fixture))


def test_con_099_enter_on_the_purged_fact_opens_its_card_with_the_purged_ending(
    fixture: Fixture,
) -> None:
    session = session_on("history", sel=3)
    press(session, fixture, "Enter")
    assert (session.overlay, session.ov_subject) == ("resolution", pt.PURGED_RUN)
    assert session.resolution_ending == Ending.PURGED.value


def test_con_099_enter_on_an_unheld_history_opens_no_card() -> None:
    session = session_on("history", sel=3)
    ctx = Ctx(session=session, fixture=chrome(), host=Host(), w=120, h=30, unheld=True)
    dispatch(ctx, "Enter", False)
    assert session.overlay is None


# ---------- CON-101: an overlay inherits the connection gate ----------

OVERLAY_VERBS: tuple[tuple[str, str, str], ...] = (
    ("draft", "p", "promote"),
    ("draft", "x", "defer"),
    ("draft", "Enter", "set criteria"),
    ("question", "1", "answering"),
    ("question", "x", "decline"),
    ("pause", "n", "reconcile"),
    ("pause", "c", "cancel"),
)


@pytest.mark.parametrize("conn", REFUSING)
@pytest.mark.parametrize(("overlay", "key", "verb"), OVERLAY_VERBS)
def test_con_101_an_overlay_verb_is_refused_by_the_routes_gate_with_its_reason(
    conn: str, overlay: str, key: str, verb: str, fixture: Fixture
) -> None:
    route = "backlog" if overlay == "draft" else "attention"
    session, link = session_on(route, overlay=overlay, conn=conn), Link()
    press(session, fixture, key, link=link)
    reason = mut_reason(session, fixture)
    assert link.sent == []
    assert session.overlay == overlay
    assert session.trace == f"{key} → {verb} is unavailable — {reason}"
    on_route = session_on("attention", conn=conn)
    press(on_route, fixture, "a")
    assert on_route.trace == f"a → answer is unavailable — {reason}"


@pytest.mark.parametrize("conn", REFUSING)
def test_con_101_the_consequence_card_is_refused_with_the_same_reason(
    conn: str, fixture: Fixture
) -> None:
    target = {"verb": "interrupt", "state": None, "id": RUN, "kind": "run.detail"}
    session = session_on("run.detail", subj_id=RUN, overlay="consequence", conn=conn)
    session.c_target = {**target, "effects": "", "not": ""}
    link = Link()
    press(session, fixture, "Enter", link=link)
    assert link.sent == []
    assert session.trace is not None
    assert session.trace.endswith(mut_reason(session, fixture))


@pytest.mark.parametrize("conn", REFUSING)
@pytest.mark.parametrize("overlay", ["draft", "question", "pause"])
def test_con_101_a_refused_overlay_advertises_none_of_its_verbs(
    conn: str, overlay: str, fixture: Fixture
) -> None:
    rows = compose_frame(view_of(session_on("attention", overlay=overlay, conn=conn), fixture))
    bar = rows[-1]
    for token in ("promote", "defer", "pick an answer", "decline", "reconcile", "cancel"):
        assert token not in bar
    assert "Esc back" in bar


def test_con_101_a_live_draft_still_promotes(fixture: Fixture) -> None:
    session = session_on("backlog", overlay="draft")
    press(session, fixture, "p")
    assert session.trace is not None
    assert "is unavailable" not in session.trace


# ---------- CON-102: dismissing an overlay is never a resolution ----------


@pytest.mark.parametrize("name", OVERLAYS)
def test_con_102_dismissing_writes_nothing_and_returns_focus_to_the_invoking_row(
    name: str, fixture: Fixture
) -> None:
    session, link = session_on("attention", sel=2), Link()
    open_overlay(session, name, subject="ACT-0031")
    session.sel = 0
    before = _register(fixture)
    press(session, fixture, "Escape", link=link)
    assert session.overlay is None
    assert session.sel == 2
    assert link.sent == []
    assert _register(fixture) == before
    assert session.toasts == []


def test_con_102_enter_on_an_attention_row_opens_the_detail_its_record_kind_owns(
    fixture: Fixture,
) -> None:
    rows = att.attn_list(session_on("attention"), fixture)
    kinds = set()
    for i, row in enumerate(rows):
        session = session_on("attention", sel=i)
        press(session, fixture, "Enter")
        if att.is_notice(row):
            kinds.add("notice")
            assert (session.route, session.overlay) == ("notifications", None)
        elif "question" in row.kind:
            kinds.add("question")
            assert (session.overlay, session.ov_subject) == ("question", row.id)
        elif row.bucket == "lost":
            kinds.add("pause")
            assert (session.overlay, session.ov_subject) == ("pause", row.id)
        else:
            kinds.add("action")
            assert (session.overlay, session.ov_subject) == ("consequence", row.id)
    assert kinds == {"notice", "question", "pause", "action"}


def test_con_102_a_notice_never_opens_a_consequence_card(fixture: Fixture) -> None:
    rows = att.attn_list(session_on("attention"), fixture)
    at = next(i for i, row in enumerate(rows) if att.is_notice(row))
    session = session_on("attention", sel=at)
    press(session, fixture, "Enter")
    assert session.overlay != "consequence"


# ---------- the live path: overlays hold the frame over a route a link holds ----------


def _live_app(route: str, overlay: str | None = None) -> ConsoleApp:
    """Return a console holding only the chrome and a link that holds ``route``.

    The console is not started, so its seam reads nothing further and no daemon is
    reached; the frame is composed from what the link already holds.
    """
    app = ConsoleApp(clock=FakeClock(), seam=_held_seam(route))
    app.reset(SessionSetup(route=route, overlay=overlay, size=1))
    return app


def _live_frame(route: str, overlay: str | None = None) -> list[str]:
    return compose_frame(_live_app(route, overlay).view())


def _live_press(app: ConsoleApp, key: str) -> list[str]:
    """Dispatch ``key`` the way the running app does, and return the next frame."""
    view = app.view()
    compose_frame(view)
    ctx = Ctx(
        session=app.session,
        fixture=app.fixture,
        host=Host(),
        w=view.w,
        h=view.h,
        projection=view.projection,
        unheld=unheld(view),
    )
    dispatch(ctx, key, False)
    return compose_frame(app.view())


@pytest.mark.parametrize("name", OVERLAYS)
def test_live_overlay_over_a_held_route_draws_the_overlay_not_not_held(name: str) -> None:
    rows = _live_frame("scope.home", name)
    text = "\n".join(rows)
    assert "NOT HELD" not in text
    if name != "palette":
        # the palette names every held record by id, the route's own among them
        assert "TRK-7001" not in text, "the route beneath an overlay is not visible"
    assert f"Eä ▸ {SURFACES[name].title}" in rows[0]


@pytest.mark.parametrize("name", ROW_DRAWERS)
def test_live_drawer_over_a_held_route_keeps_the_route_and_swaps_the_keybar(name: str) -> None:
    route = _live_frame("scope.home")
    rows = _live_frame("scope.home", name)
    assert "NOT HELD" not in "\n".join(rows)
    assert rows[:2] == route[:2]
    assert rows[-1] == keybar(DRAWER_PAIRS[name], len(rows[-1]))


def test_live_overlay_holding_nothing_takes_only_escape() -> None:
    app = _live_app("scope.home", "question")
    rows = _live_press(app, "1")
    assert app.session.overlay == "question"
    assert "Eä ▸ question" in rows[0]
    rows = _live_press(app, "Escape")
    assert (surface_of(app.session), app.session.route) == (None, "scope.home")
    assert "TRK-7001" in "\n".join(rows)


def test_live_help_opens_and_closes_over_a_held_route() -> None:
    app = _live_app("scope.home")
    opened = _live_press(app, "?")
    # a linked console names the route by its word, never by its route id
    assert "Eä ▸ help · home" in opened[0]
    assert "NOT HELD" not in "\n".join(opened)
    _live_press(app, "Escape")
    assert app.session.overlay is None


def test_live_consequence_holding_nothing_keeps_its_confirm() -> None:
    rows = _live_frame("attention", "consequence")
    assert "Enter confirm" in rows[-1]
    assert f"{UNKNOWN} unknown" in "\n".join(rows)
