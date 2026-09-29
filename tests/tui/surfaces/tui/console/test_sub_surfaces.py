"""Entity sub-surfaces: opened only from a parent, drawn at every size, walked back on Escape.

Requirement rows proved here, by id:

- ``UI-050``: an entity sub-surface is a route whose subject is one record of a parent
  entity. The set is closed by the route registry and counted from it; each takes its
  parent's group, is never a ``g`` or palette destination, renders at all three sizes,
  and climbs to its parent on Escape. ``notifications`` has no subject, so it is a global
  diagnostics route rather than a sub-surface.
- ``UI-051``: entering a rung on the Evidence route opens ``evidence.digest``, whose
  unknown rung says unknown rather than failed, whose unrun rung names what it awaits,
  whose keybar promises only copy and close, and whose copy is the claim URN with the
  rung fragment rather than the design's ``urn:eawf:`` spelling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx, go
from eawf.surfaces.tui.console.registry import REGISTRY, RouteGroup
from eawf.surfaces.tui.console.renderers import copy_target, render_route
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console.test_route_registry_closure import SUB_SURFACES

SIZES = ((80, 24), (120, 30), (160, 40))

#: The sub-surfaces the packet row names, each beside the parent it is about.
NAMED: dict[str, str] = {
    "evidence.digest": "evidence",
    "settings.stack": "settings",
    "merge.conflict": "git.pr",
    "export": "run.detail",
    "receipt": "task.detail",
    "campaign.step": "campaign",
    "campaign.artifact": "campaign",
}

#: A subject for each sub-surface that needs one to open.
SUBJECTS: dict[str, str] = {"receipt": "EVT-2218"}


class _Host:
    """The dispatcher's host: a held clock and a quit nobody asks for."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """End nothing; the tests never quit."""


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the golden fixture the console is built over."""
    return load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture")


def _ctx(session: Session, fixture: Fixture) -> Ctx:
    return Ctx(session=session, fixture=fixture, host=_Host(), w=80, h=24)


def _at(route: str) -> Session:
    session = Session()
    session.route = route
    session.subj_id = SUBJECTS.get(route)
    return session


# ---------- UI-050: the sub-surface set and its rules ----------


def test_ui050_the_sub_surface_set_is_the_registrys_and_names_each_parent() -> None:
    """The set is counted from the registry rows, never kept beside them."""
    assert dict(SUB_SURFACES) == NAMED
    assert len(SUB_SURFACES) == 7


@pytest.mark.parametrize(("route", "parent"), sorted(NAMED.items()))
def test_ui050_a_sub_surface_takes_its_parents_group_and_is_never_a_global_door(
    route: str, parent: str
) -> None:
    """No ``g`` letter and no palette row: a sub-surface needs its subject."""
    spec = REGISTRY.by_id[route]

    assert spec.group is REGISTRY.by_id[parent].group
    assert route not in REGISTRY.go_map.values()
    assert route not in REGISTRY.route_list
    assert REGISTRY.escapes[route].route == parent


def test_ui050_notifications_is_a_global_diagnostics_route_not_a_sub_surface() -> None:
    """It has no subject, so it opens from the go prefix and the palette."""
    assert "notifications" not in SUB_SURFACES
    assert REGISTRY.by_id["notifications"].group is RouteGroup.DIAGNOSTICS
    assert "notifications" in REGISTRY.go_map.values()
    assert "notifications" in REGISTRY.route_list


@pytest.mark.parametrize("route", sorted(NAMED))
@pytest.mark.parametrize(("w", "h"), SIZES)
def test_ui050_every_sub_surface_renders_at_all_three_sizes(
    fixture: Fixture, route: str, w: int, h: int
) -> None:
    """Each frame is exactly the size asked for, keybar last."""
    rows = render_route(View(session=_at(route), fixture=fixture, w=w, h=h))

    assert len(rows) == h
    assert rows[-1].strip()


@pytest.mark.parametrize(("route", "parent"), sorted(NAMED.items()))
def test_ui050_escape_on_an_empty_back_stack_climbs_to_the_parent(
    fixture: Fixture, route: str, parent: str
) -> None:
    """A sub-surface opened by a typed id still walks back to the entity it is about."""
    session = _at(route)

    dispatch(_ctx(session, fixture), "Escape", False)

    assert session.route == parent


def test_ui050_opening_pushes_the_back_stack_and_escape_returns_to_the_opener(
    fixture: Fixture,
) -> None:
    """Opened from its parent, Escape returns to the record it was opened from."""
    session = _at("evidence")
    ctx = _ctx(session, fixture)

    assert go(ctx, "evidence.digest", "Enter")
    dispatch(ctx, "Escape", False)

    assert session.route == "evidence"


# ---------- UI-051: the rung card ----------


def _rung(fixture: Fixture, index: int, *, w: int = 120, h: int = 30) -> str:
    session = _at("evidence.digest")
    session.rung = index
    return "\n".join(render_route(View(session=session, fixture=fixture, w=w, h=h)))


def test_ui051_an_unknown_rung_says_unknown_not_failed_in_the_same_fields(
    fixture: Fixture,
) -> None:
    """The card is never empty for a rung with no outcome."""
    body = _rung(fixture, 2)

    assert "no outcome yet — unknown, not failed" in body
    for label in ("CHECK", "OVER", "FOUND", "AS OF", "RECORD", "MEANS"):
        assert label in body


def test_ui051_a_rung_not_yet_run_names_the_rung_it_awaits(fixture: Fixture) -> None:
    body = _rung(fixture, 3)

    assert "awaiting rung 3" in body


def test_ui051_a_passing_rung_holds_and_does_not_certify(fixture: Fixture) -> None:
    body = _rung(fixture, 0)

    assert "This rung holds — it does not certify the claim on its own." in body


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_ui051_the_keybar_promises_only_copy_and_close(fixture: Fixture, w: int, h: int) -> None:
    session = _at("evidence.digest")
    rows = render_route(View(session=session, fixture=fixture, w=w, h=h))

    assert rows[-1].strip() == "y copy   Esc close"


@pytest.mark.parametrize(("index", "n"), [(0, 1), (3, 4)])
def test_ui051_copy_is_the_claim_urn_with_the_rung_fragment(
    fixture: Fixture, index: int, n: int
) -> None:
    """The first and last rung: the fragment is the rung number, never a design spelling."""
    session = _at("evidence.digest")
    session.rung = index

    copied = copy_target(session, fixture)

    assert copied == f"eawf://{fixture.scope}/{fixture.scope}/_/claim/CLM-0004#rung-{n}"
    assert not copied.startswith("urn:eawf:")


def test_ui051_a_rung_past_the_end_opens_the_last_rung(fixture: Fixture) -> None:
    """The off-by-one boundary: an overshot cursor clamps rather than failing."""
    session = _at("evidence.digest")
    session.rung = 99

    assert copy_target(session, fixture).endswith("#rung-4")
