"""A console reading a live tree: its menus, finished records, event keys, scope and marks.

A menu verb no daemon verb carries is listed refused with that reason and never opens a
consequence card; the chrome's prototype reasons stay off a live tree; a finished
Milestone offers no lifecycle verb and states its final band in the frame's own gutter;
a Run's event keys answer once, in words that hold; the tree names its project, root id
and URN in one scheme; an empty table draws no head; ``*`` says how many it marked; and
a cut keybar keeps the action menu ahead of paging.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.identity.urn import parse_qualified_urn
from eawf.kernel.projection.compute import build_route_projection
from eawf.surfaces.tui.console.action_menu import VerbWeight
from eawf.surfaces.tui.console.app import ConsoleApp, compose_frame
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock, notify
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.drill import NO_EVENTS
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.harness import settle
from eawf.surfaces.tui.console.keybar import KEY, keybar
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.mutation import CARD, Card
from eawf.surfaces.tui.console.operations import (
    SAME_VERB,
    UNBOUND_REASON,
    binding_refusal,
    linked_refusal,
)
from eawf.surfaces.tui.console.renderers import copy_for, render_route
from eawf.surfaces.tui.console.renderers.sandbox_log import NO_POLICY_SECTION
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.surfaces.tui.console.tokens import Severity

from . import test_native_route_frames as nrf
from .test_console_live_keys import DETAIL_ROUTES
from .test_console_live_smoke import (
    REPO_ROOT,
    authority_digests,
    live_console,
    render_setup,
    require_epoch2_repository,
)
from .test_enter_opened_cards import AT, FIXTURE_ROOT

#: The live chrome menus, as a console holding no prototype row lists them.
LINKED = Fixture.from_chrome(load_chrome())
#: The project URN the probe tree's records are spelled under.
PROJECT_URN = "eawf://EAWF/EAWF/_/project/EAWF"
#: The probe tree's root id, as the seam is addressed.
ROOT_ID = "root-0123456789abcdef"


class _Clipped(ConsoleApp):
    """A console that is never run, whose clipboard takes a copy as a terminal's would."""

    def copy_text(self, text: str) -> bool:
        """Take ``text``; a console that is not running has no terminal of its own."""
        return bool(text)


def _linked(route: str, *, subject: str | None = None, sel: str | None = None) -> ConsoleApp:
    """Return a console linked to a seam holding ``route``'s projection over the probe tree."""
    seam = ProjectionSeam(
        route=route, scope_id=ROOT_ID, state_path=None, clock=lambda: AT, scope_name="eawf"
    )
    seam._projection = build_route_projection(
        route=route, document=nrf.DOCUMENT, cursor=nrf.CURSOR, scope_id=ROOT_ID, generated_at=AT
    )
    app = _Clipped(chrome=load_chrome(), seam=seam, clock=FakeClock())
    app.session.route = route
    app.session.subj_id = subject
    app.session.sel_id = sel
    return app


def _press(app: ConsoleApp, *keys: str) -> None:
    for key in keys:
        dispatch(app._ctx(), key, False)


def _text(app: ConsoleApp) -> str:
    return "\n".join(compose_frame(app.view()))


# ---------- a verb no daemon verb carries never opens a card ----------


def test_every_offered_heavy_verb_on_a_live_tree_is_one_a_daemon_verb_carries() -> None:
    """Audit every chrome menu verb: a heavy verb stays offered only with a daemon binding."""
    audited = 0
    for route, _rows in LINKED.proto.actions.items():
        for verb in LINKED.menus.verbs(route):
            audited += 1
            if verb.weight is VerbWeight.LIGHT:
                continue
            if verb.available:
                assert binding_refusal(route, verb.verb) == "", (route, verb.verb)
            else:
                assert verb.reason == linked_refusal(route, verb.verb), (route, verb.verb)
    assert audited > 50


@pytest.mark.parametrize(("route", "letter"), [("scope.home", "p"), ("run.detail", "s")])
def test_pin_outcome_and_steer_read_the_unbound_reason(route: str, letter: str) -> None:
    verb = LINKED.menus.verb(route, letter)
    assert verb is not None
    assert (verb.available, verb.reason) == (False, UNBOUND_REASON)


@pytest.mark.parametrize(
    ("route", "subject", "sel", "letter"),
    [
        ("scope.home", None, "MLS-0100", "p"),
        ("run.detail", "RUN-00000001", None, "s"),
    ],
)
def test_an_unbound_verb_is_refused_without_a_card(
    route: str, subject: str | None, sel: str | None, letter: str
) -> None:
    app = _linked(route, subject=subject, sel=sel)
    _text(app)
    _press(app, ".")
    assert UNBOUND_REASON in _text(app)
    _press(app, letter)
    assert app.session.overlay != CARD
    assert app.session.trace is not None and UNBOUND_REASON in app.session.trace


def test_the_prototype_replay_keeps_its_own_menus() -> None:
    proto = load_fixture(FIXTURE_ROOT)
    pin = proto.menus.verb("scope.home", "p")
    assert pin is not None and pin.available and pin.reason == ""


# ---------- no prototype reason on a live tree ----------


def test_retire_track_names_what_it_acts_on_not_the_fixtures_batches() -> None:
    verb = LINKED.menus.verb("scope.home", "r")
    assert verb is not None and not verb.available
    assert "batches in flight" not in verb.reason
    assert verb.reason == linked_refusal("scope.home", "retire track")
    assert "track" in verb.reason
    assert set(SAME_VERB) == {"retire track", "accept", "authorize merge", "promote"}


def test_no_live_menu_reason_is_a_chrome_literal() -> None:
    chrome_reasons = {
        row[3] for rows in load_chrome().actions.values() for row in rows if len(row) > 3 and row[3]
    }
    for route in LINKED.proto.actions:
        for verb in LINKED.menus.verbs(route):
            assert verb.reason not in chrome_reasons, (route, verb.verb, verb.reason)


# ---------- the sandbox policy key names the held policy ----------


def test_p_names_the_held_policy_and_opens_no_unrelated_section() -> None:
    app = _linked("sandbox.log", sel="SBX-0001")
    _text(app)
    _press(app, "p")
    assert app.session.route == "sandbox.log"
    toast = app.session.toasts[-1]
    assert toast.text == f"SBX-0001 · sandbox policy · rev 3 · {NO_POLICY_SECTION}"
    assert toast.sev is Severity.WARN
    assert "pol-2026" not in toast.text


# ---------- a finished Milestone keeps only its light verbs ----------


def test_home_offers_no_lifecycle_verb_on_a_completed_leaf() -> None:
    app = _linked("scope.home", sel="MLS-0101")
    _text(app)
    _press(app, ".")
    menu = _text(app)
    for verb in ("activate", "open review", "cancel"):
        assert f"  {verb}  " not in menu, verb
    _press(app, "t")
    assert app.session.overlay != CARD


def test_home_still_offers_lifecycle_verbs_on_an_open_leaf() -> None:
    app = _linked("scope.home", sel="MLS-0100")
    _text(app)
    _press(app, ".")
    assert "open review" in _text(app)


def test_a_completed_milestone_frame_draws_its_final_band_and_light_menu() -> None:
    app = _linked("milestone", subject="MLS-0101")
    frame = _text(app).split("\n")
    final = next(row for row in frame if "FINAL" in row)
    track = next(row for row in frame if "TRACK" in row)
    assert final.index("FINAL") == track.index("TRACK")
    assert final.index("MLS-0101") == track.index("TRK-CORE")
    assert "COMPLETED · final" in frame[1]
    _press(app, ".")
    menu = _text(app)
    assert "request repair" not in menu
    assert "open trust record" in menu


# ---------- one label width per frame ----------


def _value_at(row: str, label: str) -> int:
    """Return the cell a labelled row's value starts at."""
    after = row.index(label) + len(label)
    return after + len(row[after:]) - len(row[after:].lstrip())


def test_the_batch_final_band_sits_in_the_batch_frames_gutter() -> None:
    app = _linked("batch.detail", subject="BAT-0100")
    frame = _text(app).split("\n")
    final = next(row for row in frame if "FINAL" in row)
    state = next(row for row in frame if row.lstrip().startswith("STATE"))
    assert final.index("FINAL") == state.index("STATE")
    assert _value_at(final, "FINAL") == _value_at(state, "STATE")


def test_health_tuples_sits_in_the_checks_gutter() -> None:
    frame = nrf._frame("health", w=160)
    checks = next(row for row in frame if row.startswith(" CHECKS"))
    tuples = next(row for row in frame if row.startswith(" TUPLES"))
    assert _value_at(tuples, "TUPLES") == _value_at(checks, "CHECKS")


# ---------- the Run's event keys answer once, in words that hold ----------


def test_the_event_toast_points_at_enter_on_this_frame() -> None:
    assert NO_EVENTS == "this frame walks no event · the transcript draws them · Enter opens it"
    assert "Activity" not in NO_EVENTS


def test_a_toast_identical_to_the_newest_replaces_it() -> None:
    session, clock = Session(), FakeClock()
    notify(session, clock, text="same", title="↓", sev=Severity.INFO)
    clock.advance(1.0)
    notify(session, clock, text="same", title="↑", sev=Severity.INFO)
    assert [(t.text, t.title) for t in session.toasts] == [("same", "↑")]
    assert session.toasts[0].at == pytest.approx(clock.now())
    notify(session, clock, text="other", title="x", sev=Severity.INFO)
    notify(session, clock, text="same", title="y", sev=Severity.INFO)
    assert [t.text for t in session.toasts] == ["same", "other", "same"]


def test_arrows_on_a_run_stack_one_toast() -> None:
    app = _linked("run.detail", subject="RUN-00000001")
    _text(app)
    _press(app, "ArrowDown", "ArrowUp", "PageDown", "End")
    assert [t.text for t in app.session.toasts] == [NO_EVENTS]


# ---------- home's scope drawer and one URN scheme ----------


def test_i_on_home_opens_the_scope_drawer() -> None:
    app = _linked("scope.home", sel="MLS-0100")
    assert "i inspect" in _text(app).split("\n")[-1]
    _press(app, "i")
    assert app.session.overlay == "inspect"
    text = _text(app)
    assert "project     eawf" in text
    assert f"root        {ROOT_ID}" in text
    assert f"urn         {PROJECT_URN}" in text
    assert copy_for(app._ctx()) == PROJECT_URN


def test_a_subjectless_frame_copies_the_tree_urn_in_the_one_scheme() -> None:
    app = _linked("scope.home", sel=None)
    app.session.route = "git.pr"
    _press(app, "Y")
    copied = app.session.toasts[-1].text
    assert copied == PROJECT_URN
    assert not copied.startswith("urn:eawf:")
    assert str(parse_qualified_urn(copied)) == copied


def test_the_prototype_home_bar_keeps_the_packs_keys() -> None:
    view = View(session=Session(), fixture=load_fixture(FIXTURE_ROOT), w=80, h=24)
    assert "i inspect" not in render_route(view)[-1]
    assert KEY["inspect"] in native_keys("scope.home", windowed=False)


# ---------- an empty table draws no head ----------


@pytest.mark.parametrize(
    ("route", "heads"),
    [
        ("cost.ceiling", ("RUN AT REASON",)),
        ("campaign", ("STEP STATE DEPENDS ON", "RECEIPT WHAT IT SHOWS CLAIM", "ARTIFACT WRITTEN")),
    ],
)
def test_an_empty_table_draws_no_head(route: str, heads: tuple[str, ...]) -> None:
    rows = {" ".join(row.split()) for row in nrf._frame(route, w=80)}
    for head in heads:
        assert head not in rows, head


# ---------- select all says how many it marked ----------


def test_select_all_raises_the_count_it_marked() -> None:
    app = _linked("scope.home", sel="MLS-0100")
    _text(app)
    _press(app, ".", "*")
    assert app.session.marked
    toast = app.session.toasts[-1]
    assert toast.text.endswith(f"{len(app.session.marked)} selected")


# ---------- a cut native bar keeps the action menu ----------


def test_a_cut_native_activity_bar_keeps_the_menu_ahead_of_paging() -> None:
    pairs = [e.pair() for e in native_keys("activity", windowed=True)]
    bar = keybar(pairs, 80, keep_actions=True)
    assert ". actions" in bar
    assert "Enter drill" in bar
    assert ". actions" not in keybar(pairs, 80)


# ---------- live: every action menu on every route, on this repository's tree ----------


def _menu_letters(app: ConsoleApp) -> list[str]:
    """Return every verb letter the open action drawer lists."""
    rows = compose_frame(app.view())
    head = next((i for i, row in enumerate(rows) if "ACTIONS   KEY  VERB" in row), None)
    if head is None:
        return []
    letters = []
    for row in rows[head + 1 : -1]:
        cell = row[14:19].strip()
        if len(cell) == 1:
            letters.append(cell)
    return letters


def test_live_no_menu_verb_without_a_daemon_verb_opens_a_card(tmp_path: Path) -> None:
    """Every letter of every route's action menu, pressed live, opens only a bound card."""
    from eawf.surfaces.tui.console.registry import REGISTRY

    require_epoch2_repository()
    before = authority_digests(REPO_ROOT)

    async def body() -> tuple[list[str], int]:
        opened: list[str] = []
        pressed = 0
        async with (
            live_console(REPO_ROOT, tmp_path / "runtime", launched=True) as (app, seam),
            app.run_test(size=SIZES[1]) as pilot,
        ):
            setups: list[SessionSetup] = []
            for route in REGISTRY.ids:
                if route == "entry":
                    continue
                setups.append(SessionSetup(route=route, size=1))
                await render_setup(app, pilot, setups[-1])
                model: Any = seam.projection_for(REGISTRY.by_id[route].key)
                keys = [row.key for row in getattr(model, "rows", ())]
                if keys and route in DETAIL_ROUTES:
                    setups.append(SessionSetup(route=route, size=1, subjId=keys[0]))
            for setup in setups:
                await render_setup(app, pilot, setup)
                app.press_key(".")
                await settle(pilot)
                for letter in _menu_letters(app):
                    await render_setup(app, pilot, setup)
                    app.press_key(".")
                    app.press_key(letter)
                    await settle(pilot)
                    pressed += 1
                    s = app.session
                    if s.overlay == CARD and not isinstance(s.mutation, Card):
                        opened.append(f"{setup.route} {setup.subj_id or ''}: . {letter}")
        return opened, pressed

    opened, pressed = asyncio.run(body())
    assert not opened, "a card opened for a verb no daemon verb carries:\n" + "\n".join(opened)
    assert pressed > 60
    assert authority_digests(REPO_ROOT) == before, "the live serve wrote to the authority tree"
