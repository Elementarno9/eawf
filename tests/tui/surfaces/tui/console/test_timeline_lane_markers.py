"""The native Timeline lanes carry a marker cursor, and each closed Milestone says how it closed.

A Track's lane draws its dated Milestones in the week of their target date. The focused
lane brackets one of them, the arrows step the bracket along the lane, Enter opens the
bracketed Milestone and the menu's ``propose date`` re-dates it. A cancelled Milestone is
drawn apart from a completed one, and the legend names both glyphs.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

from eawf.kernel.projection.compute import build_route_projection
from eawf.surfaces.tui.console.app import ConsoleApp, compose_frame
from eawf.surfaces.tui.console.cards import CARD, Card
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.operations import Operator, TargetDate
from eawf.surfaces.tui.console.seam import ProjectionSeam

from . import test_native_route_frames as nrf
from .test_enter_opened_cards import AT

ROOT_ID = "root-0123456789abcdef"


def _document() -> dict[str, Any]:
    """Return the probe tree with three of its Track's Milestones dated around now."""
    document = copy.deepcopy(nrf.DOCUMENT)
    milestones = document["milestone"]
    milestones["MLS-0100"]["target_date"] = "2026-09-22"
    milestones["MLS-0101"]["target_date"] = "2026-09-10"
    milestones["MLS-0102"] = nrf._row(
        "milestone",
        "MLS-0102",
        "CANCELLED",
        title="Dropped spike",
        primary_track_ref=f"{nrf.ROOT}/track/TRK-CORE",
        target_date="2026-09-29",
    )
    return document


def _timeline() -> ConsoleApp:
    """Return a live Timeline console on its lanes, drawn once."""
    seam = ProjectionSeam(
        route="roadmap",
        scope_id=ROOT_ID,
        state_path=None,
        clock=lambda: AT,
        scope_name="eawf",
        operator=Operator(principal="OP-0001"),
    )
    seam._projection = build_route_projection(
        route="roadmap", document=_document(), cursor=nrf.CURSOR, scope_id=ROOT_ID, generated_at=AT
    )
    app = ConsoleApp(chrome=load_chrome(), seam=seam, clock=FakeClock())
    app.session.route = "timeline"
    _text(app)
    return app


def _press(app: ConsoleApp, *keys: str) -> None:
    for key in keys:
        dispatch(app._ctx(), key, False)


def _text(app: ConsoleApp) -> str:
    return "\n".join(compose_frame(app.view()))


def _lane(frame: str) -> str:
    return next(row for row in frame.splitlines() if "TRK-CORE" in row and "─" in row)


def test_the_focused_lane_brackets_its_first_dated_milestone() -> None:
    app = _timeline()
    frame = _text(app)
    assert "[✓]" in _lane(frame)
    assert "MARKER" in frame and "MLS-0101 · TRK-CORE · 1 of 3" in frame
    assert app.session.timeline_marker == "MLS-0101"


def test_the_arrows_step_the_marker_and_enter_opens_the_marked_milestone() -> None:
    app = _timeline()
    _press(app, "ArrowRight")
    frame = _text(app)
    assert "[●]" in _lane(frame) and "MLS-0100 · TRK-CORE · 2 of 3" in frame
    _press(app, "Enter")
    assert (app.session.route, app.session.subj_id) == ("milestone", "MLS-0100")


def test_the_lane_keybar_offers_the_marker_and_enter() -> None:
    foot = _text(_timeline()).splitlines()[-1]
    assert "marker" in foot and "Enter" in foot


def test_propose_date_from_a_lane_marker_re_dates_the_marked_milestone() -> None:
    app = _timeline()
    _press(app, "ArrowRight")
    _text(app)  # the console redraws after every key, which places the marker
    _press(app, ".", "m", *"2026-10-20", "Enter")
    card = app.session.mutation
    assert app.session.overlay == CARD and isinstance(card, Card)
    assert card.items[0].request == TargetDate(target="MLS-0100", target_date=date(2026, 10, 20))


def test_a_cancelled_milestone_draws_apart_from_a_completed_one() -> None:
    frame = _text(_timeline())
    lane = _lane(frame)
    assert "✓" in lane and "✗" in lane
    assert "✗ cancelled" in frame and "✓ done" in frame
