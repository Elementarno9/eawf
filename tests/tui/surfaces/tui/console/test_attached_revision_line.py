"""The native ATTACHED line: the seam's own revision, never the golden fixture's.

A native frame that cannot call its counts complete says so the way the prototype
frame does -- an ``ATTACHED`` line naming the revision the rows were read at -- but the
revision it names has to be the one its own held projection states, never the golden
harness's own ``41,208``. :func:`~eawf.surfaces.tui.console.reads.attached` is the one
function both frames go through, so the suite pins its contract directly and then pins
one native route drawing from it, over a projection built at a revision the harness
does not share.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.spine import build_spine_view
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.reads import Age, Reads, attached
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session

#: When the probe projection is stamped; only its cursor matters to this suite.
AT = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

#: The scope the probe projection is built for.
SCOPE = "EAWF"

#: The golden fixture's own revision, quoted so a passing test proves this value is
#: absent rather than merely not asserted.
FIXTURE_REVISION = "41,208"

#: A cursor distinct from the fixture's, so a test that passed by coincidence -- a
#: renderer that always draws the fixture's own revision -- would still be caught.
LIVE_CURSOR = 90209

_FIXTURE_PATH = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"


def test_attached_line_states_the_revision_it_was_given() -> None:
    """The ATTACHED line names the caller's own revision, never a value of its own."""
    rd = Reads(complete=False, label="known", age=Age.REVISION)

    line = attached(rd, revision=group(LIVE_CURSOR))

    assert line == "revision 90,209"
    assert FIXTURE_REVISION not in line


def test_attached_line_omits_an_age_it_was_not_given() -> None:
    """An aged state prints the revision alone rather than fabricating a duration."""
    rd = Reads(complete=False, label="a snapshot", age=Age.AGED)

    line = attached(rd, revision=group(LIVE_CURSOR))

    assert line == "revision 90,209"
    assert "old" not in line


def test_attached_line_states_the_age_it_was_given() -> None:
    """An aged state with a known age still names it, exactly as the prototype does."""
    rd = Reads(complete=False, label="a snapshot", age=Age.AGED)

    line = attached(rd, revision=group(LIVE_CURSOR), age="6m 12s")

    assert line == "revision 90,209 · 6m 12s old"


def test_attached_line_is_empty_when_the_counts_are_complete() -> None:
    """A complete link claims nothing extra: the honesty line has nothing to say."""
    rd = Reads(complete=True, label="", age=Age.NONE)

    assert attached(rd, revision=group(LIVE_CURSOR)) == ""


def test_a_native_frame_draws_the_held_projections_own_revision() -> None:
    """The scope-home native frame's ATTACHED line names its own projection's cursor."""
    projection = build_route_projection(
        route="scope.home", document={}, cursor=LIVE_CURSOR, scope_id=SCOPE, generated_at=AT
    )
    spine = build_spine_view(projection)
    session = Session()
    session.route = spine.route
    session.conn = "GAP DETECTED"
    view = View(session=session, fixture=load_fixture(_FIXTURE_PATH), w=120, h=30, projection=spine)

    rows = render_route(view)
    body = "\n".join(rows)

    assert " ATTACHED  revision 90,209" in body
    assert FIXTURE_REVISION not in body


def test_a_complete_native_frame_draws_no_attached_line() -> None:
    """A native frame whose link is live and complete has nothing to attach."""
    projection = build_route_projection(
        route="scope.home", document={}, cursor=LIVE_CURSOR, scope_id=SCOPE, generated_at=AT
    )
    spine = build_spine_view(projection)
    session = Session()
    session.route = spine.route
    session.conn = "LIVE"
    view = View(session=session, fixture=load_fixture(_FIXTURE_PATH), w=120, h=30, projection=spine)

    rows = render_route(view)

    assert not any(row.strip().startswith("ATTACHED") for row in rows)
