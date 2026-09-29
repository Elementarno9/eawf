"""The help card and the overlay crumbs: what help teaches, and where an overlay says it is.

Help teaches the keys the frame under it offers and every global key, ``!`` included, and
still carries the quit rule at 80x24. A linked console's overlay names the project it is
open in and the route by its word, never the chrome's placeholder scope or a route id.
"""

from __future__ import annotations

import dataclasses

import pytest

from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import KEY
from eawf.surfaces.tui.console.session import BackEntry, Session
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
from tests.tui.surfaces.tui.console.test_guarded_quit import FIXTURE
from tests.tui.surfaces.tui.console.test_native_navigation import _milestone


def _named(view: View) -> View:
    """Return ``view`` as a launched console draws it: attached to the project ``eawf``."""
    return dataclasses.replace(view, scope_name="eawf")


def _over(view: View, overlay: str) -> list[str]:
    """Draw the route once, as the running console has, then the overlay over it."""
    compose_frame(view)
    view.session.overlay = overlay
    return compose_frame(view)


def _this_route(card: list[str]) -> list[str]:
    start = card.index(next(row for row in card if row.startswith(" THIS ROUTE"))) + 1
    end = card.index(next(row for row in card if row.startswith(" EVERYWHERE"))) - 1
    return card[start:end]


# ---------- K-03: an overlay names the project and the route by its word ----------


@pytest.mark.parametrize(
    ("overlay", "crumb"),
    [("help", " Eä ▸ eawf ▸ help · home"), ("palette", " Eä ▸ eawf ▸ palette")],
)
def test_k_03_an_overlay_crumb_names_the_project(overlay: str, crumb: str) -> None:
    card = _over(_named(bodies._view("scope.home")), overlay)
    assert card[0].startswith(crumb)


def test_k_03_an_overlay_over_a_path_names_the_project_never_the_placeholder() -> None:
    view = _named(bodies._view("track", subject="TRK-CORE"))
    view.session.back.record(BackEntry(route="scope.home", sel=0, subj=None))
    head = _over(view, "help")[0]
    assert head.startswith(" Eä ▸ eawf ▸ ")
    assert " ? " not in head
    assert "help · track" in head


def test_k_03_the_prototype_replay_keeps_its_recorded_crumb() -> None:
    session = Session()
    session.route = "activity"
    card = _over(View(session=session, fixture=FIXTURE, w=80, h=24), "help")
    assert card[0].startswith(" Eä ▸ help · activity ")


# ---------- K-12: help teaches only what the frame under it offers ----------


def test_k_12_help_leaves_out_a_key_the_frame_gave_up() -> None:
    """A Milestone with no acceptance bundle offers no digest copy, and help says none."""
    view = _milestone("MLS-0100")
    card = _over(view, "help")
    assert view.session.route_bar_keys is not None
    assert not view.session.route_bar_keys.intersection(KEY["digest"].keys)
    assert not any(KEY["digest"].label in row for row in _this_route(card))


@pytest.mark.parametrize("route", ["scope.home", "activity", "track"])
def test_k_12_every_key_help_teaches_for_the_route_is_one_its_bar_offered(route: str) -> None:
    view = bodies._view(route, subject="TRK-CORE" if route == "track" else None)
    card = _over(view, "help")
    offered = view.session.route_bar_keys
    assert offered is not None
    rows = _this_route(card)
    assert rows
    for entry in KEY.values():
        if any(row.lstrip().startswith(f"{entry.token} ") for row in rows):
            assert offered.intersection(entry.keys), entry.token


# ---------- C-10: the attention jump is taught, and the quit rule still fits ----------


def test_c_10_help_lists_the_attention_jump_and_keeps_the_quit_rule_at_80x24() -> None:
    session = Session()
    session.route = "activity"
    card = _over(View(session=session, fixture=FIXTURE, w=80, h=24), "help")
    assert any(row.startswith("   !          jump to your top attention item") for row in card)
    assert any("80ms to 1.5s apart" in row for row in card)
    assert len(card) == 24
