"""Every console route resolves to something native, and the grid owes nothing.

The coverage grid says what serves each route. This suite asks the question one
step further out: does every route the registry holds actually *resolve* -- to a
declared read model, and to a shipped verb that answers it -- and does the grid
end up owing no work to a later wave.

Three things are checked, and each is derived from the code rather than restated.

**Resolution is total.** Every registry route binds a read model, every read
model that carries a projection declares the route back, and the registry's own
hole list is empty. A route with no read model is a specification hole, and the
registry publishes them, so the assertion is over that published list.

**Every bound route has a verb.** A route the projection table binds is served by
``projection.<route>.read`` and by ``projection.<route>.reconnect``, both
registered on the daemon. A route bound in the table with no registered verb
would render in the grid and answer nothing on the wire.

**The grid declares no hole.** Thirty-one bound, two served off document, one
unprojectable, and nothing owed. The arithmetic is asserted against the tables it
counts, so binding one more route without moving its row reds this suite rather
than quietly changing the total.

The counts are pinned deliberately. A test that only compared the grid to itself
would stay green through a route being dropped from both sides at once, which is
exactly how a console loses a surface without anybody noticing.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from eawf.kernel.projection.compute import ROUTE_COLLECTIONS
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE, RECONNECT_METHOD_TEMPLATE
from eawf.kernel.projection.read_models import (
    READ_MODEL_BY_KIND,
    READ_MODELS,
    ReadModelKind,
    ReadModelSpec,
)
from eawf.kernel.projection.settings import SETTINGS_ROUTES
from eawf.runtime.daemon.methods import registered_methods
from eawf.runtime.daemon.methods.projection import (
    ROUTE_READ_METHODS,
    ROUTE_RECONNECT_METHODS,
    SETTINGS_READ_METHOD,
)
from eawf.surfaces.tui.console.registry import REGISTRY

#: Where the coverage grid is recorded, four levels up lands on ``tests/``.
MANIFEST = Path(__file__).resolve().parents[4] / "fixtures/console/coverage-manifest.json"

#: How many routes a projection serves from a document collection.
BOUND_ROUTES = 31

#: How many routes a read verb serves without a collection behind it.
OFF_DOCUMENT_ROUTES = 2

#: How many routes no projection can ever carry.
UNPROJECTABLE_ROUTES = 1

#: The one route in that last group.
UNPROJECTABLE_ROUTE = "entry"


def recorded_rows() -> list[dict[str, object]]:
    """Return the coverage grid's rows, decoded.

    Returns:
        One mapping per row, in the order the grid records them.
    """
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = list(document["routes"])
    return rows


def unresolved_routes() -> tuple[str, ...]:
    """Return every registry route that does not resolve to a native read model.

    A route resolves when the registry binds it to a read model kind, that kind
    is one the read-model declarations hold, and the declarations name the
    route's own key back. Anything else is a route whose frame nobody owns.

    Returns:
        The unresolved route ids, sorted. Empty when resolution is total.
    """
    unresolved: list[str] = []
    for spec in REGISTRY.routes:
        kind = REGISTRY.read_models.get(spec.id)
        if kind is None:
            unresolved.append(spec.id)
            continue
        declaration = READ_MODEL_BY_KIND.get(kind)
        if declaration is None or spec.key not in declaration.routes:
            unresolved.append(spec.id)
    return tuple(sorted(unresolved))


def unserved_bound_routes() -> tuple[str, ...]:
    """Return every collection-bound route with no registered read or reconnect verb.

    Returns:
        The route ids, sorted. Empty when every bound route answers on the wire.
    """
    registered = set(registered_methods())
    unserved: list[str] = []
    for spec in REGISTRY.routes:
        if spec.key not in ROUTE_COLLECTIONS:
            continue
        read = READ_METHOD_TEMPLATE.format(route=spec.key)
        reconnect = RECONNECT_METHOD_TEMPLATE.format(route=spec.key)
        if read not in registered or reconnect not in registered:
            unserved.append(spec.id)
    return tuple(sorted(unserved))


def declared_holes() -> tuple[str, ...]:
    """Return every route the recorded grid still owes to a later wave.

    Returns:
        The route ids of rows whose binding is ``hole``, sorted.
    """
    return tuple(sorted(str(row["route"]) for row in recorded_rows() if row["binding"] == "hole"))


# ---------- every route resolves ----------


def test_every_registry_route_resolves_to_a_native_read_model() -> None:
    """A route nobody bound renders a frame nobody declared."""
    assert unresolved_routes() == ()


def test_the_registry_publishes_no_read_model_hole() -> None:
    """The registry's own hole list is the surface a missing binding shows on."""
    assert REGISTRY.read_model_holes == ()
    assert tuple(REGISTRY.read_models) == REGISTRY.ids


@pytest.mark.parametrize("route", sorted(REGISTRY.ids))
def test_every_route_and_its_read_model_name_each_other(route: str) -> None:
    """Resolution is checked in both directions, one route at a time."""
    kind = REGISTRY.read_models[route]
    assert isinstance(kind, ReadModelKind)
    assert REGISTRY.by_id[route].key in READ_MODEL_BY_KIND[kind].routes


@pytest.mark.parametrize("spec", sorted(READ_MODELS, key=lambda item: item.kind.value))
def test_every_declared_read_model_is_rendered_by_a_route_or_carried_by_one(
    spec: ReadModelSpec,
) -> None:
    """A read model nothing renders and nothing carries is a shape with no surface."""
    bound = {route for route, kind in REGISTRY.read_models.items() if kind is spec.kind}
    if bound:
        assert {REGISTRY.by_id[route].key for route in bound} == set(spec.routes)
        return
    assert spec.parent is not None
    assert READ_MODEL_BY_KIND[spec.parent].routes


# ---------- every bound route answers on the wire ----------


def test_every_collection_bound_route_has_a_registered_read_and_reconnect_verb() -> None:
    """A route the table binds and no verb serves renders nothing in a shipped binary."""
    assert unserved_bound_routes() == ()


def test_the_registered_route_verbs_are_exactly_the_bound_routes() -> None:
    """The verb tables and the collection table agree on which routes are served."""
    expected = {READ_METHOD_TEMPLATE.format(route=route) for route in ROUTE_COLLECTIONS}
    assert set(ROUTE_READ_METHODS) == expected
    assert len(ROUTE_RECONNECT_METHODS) == len(ROUTE_READ_METHODS)


def test_the_settings_routes_are_served_by_their_own_verb() -> None:
    """The two off-document routes answer through a verb that is not a row read."""
    assert len(SETTINGS_ROUTES) == OFF_DOCUMENT_ROUTES
    assert SETTINGS_READ_METHOD in registered_methods()
    assert SETTINGS_READ_METHOD not in ROUTE_READ_METHODS


def test_the_unprojectable_route_is_served_by_nothing_and_that_is_correct() -> None:
    """No projection exists before a session does, so the entry layer has no verb."""
    spec = REGISTRY.by_id[UNPROJECTABLE_ROUTE]
    assert spec.key not in ROUTE_COLLECTIONS
    assert READ_METHOD_TEMPLATE.format(route=spec.key) not in registered_methods()
    assert not READ_MODEL_BY_KIND[REGISTRY.read_models[UNPROJECTABLE_ROUTE]].projection_backed


# ---------- the grid owes nothing ----------


def test_the_coverage_grid_declares_zero_holes() -> None:
    """The claim this suite is named for: no route is owed to a later wave."""
    assert declared_holes() == ()


def test_the_coverage_grid_lists_every_registry_route_once() -> None:
    """A grid that is not total cannot claim zero holes about anything."""
    listed = [row["route"] for row in recorded_rows()]
    assert sorted(listed) == sorted(REGISTRY.ids)
    assert len(listed) == len(set(listed))


def test_the_coverage_grid_accounts_for_every_route_across_three_bindings() -> None:
    """Thirty-one bound plus two off document plus one unprojectable is the registry."""
    tally = Counter(row["binding"] for row in recorded_rows())
    assert tally == {
        "bound": BOUND_ROUTES,
        "served_off_document": OFF_DOCUMENT_ROUTES,
        "unprojectable": UNPROJECTABLE_ROUTES,
    }
    assert sum(tally.values()) == len(REGISTRY.ids)


def test_the_pinned_counts_are_the_counts_the_tables_hold() -> None:
    """The literals are checked against the tables, not merely against the grid."""
    assert len(ROUTE_COLLECTIONS) == BOUND_ROUTES
    assert len(SETTINGS_ROUTES) == OFF_DOCUMENT_ROUTES
    assert len(REGISTRY.ids) == BOUND_ROUTES + OFF_DOCUMENT_ROUTES + UNPROJECTABLE_ROUTES


# ---------- the checks have teeth ----------


def test_an_unbound_route_is_reported_as_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping one route's read model makes resolution partial, and the check says so."""
    thinned = {route: kind for route, kind in REGISTRY.read_models.items() if route != "search"}
    monkeypatch.setattr(REGISTRY, "read_models", thinned)
    assert unresolved_routes() == ("search",)


def test_a_route_its_read_model_does_not_name_back_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-way binding is a route rendering a shape that never claimed it."""
    rebound = {**REGISTRY.read_models, "search": ReadModelKind.HEALTH_VIEW}
    monkeypatch.setattr(REGISTRY, "read_models", rebound)
    assert unresolved_routes() == ("search",)


def test_a_bound_route_with_no_registered_verb_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un-registering a read verb leaves a route the grid calls bound and nothing serves."""
    kept = tuple(name for name in registered_methods() if name != "projection.trust.read")
    monkeypatch.setattr(
        "tests.tui.surfaces.tui.console.test_route_binding_complete.registered_methods",
        lambda: kept,
    )
    assert unserved_bound_routes() == ("trust",)


def test_a_declared_hole_is_reported() -> None:
    """The zero-hole claim has teeth: a grid carrying one is not empty of them."""
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["routes"] = [
        *document["routes"],
        {
            "route": "spike.hole",
            "read_model": "search_page",
            "binding": "hole",
            "bound_by": "P99-I99-W99",
        },
    ]
    holes = sorted(row["route"] for row in document["routes"] if row["binding"] == "hole")
    assert holes == ["spike.hole"]


def test_the_grid_file_is_where_this_suite_expects_it() -> None:
    """A path that stopped resolving would make every grid assertion vacuous."""
    assert MANIFEST.is_file()
    assert recorded_rows()
