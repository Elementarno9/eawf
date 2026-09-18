"""Every console route is listed once, as bound or as a hole naming the wave that binds it.

The coverage grid is the thing that makes "the console is bound" falsifiable. Without it,
a route that nobody ever wired is indistinguishable from a route somebody decided not to
wire: both are simply absent. The manifest lists all thirty-four registry routes, each
with the read model it renders and one of three bindings -- ``bound`` when a projection
serves it today, ``hole`` when a named later wave will, and ``unprojectable`` for the one
route no projection can carry, because no projection exists before a session does.

Two failures are the point. A route the registry holds and the manifest does not is
unlisted, and the grid can no longer claim to be total. A hole that names no wave is an
undeclared hole, which is the shape a permanent gap takes while looking temporary. Both
are reported by :func:`coverage_defects`, and both are exercised here on deliberately
broken manifests so the check is known to have teeth.

The last rule is the one binding must not cost. The epoch-1 trust mode -- the mode the
tracked golden contract replays -- keeps every key its footer advertises resolving. A
footer that advertises a key nothing handles is a frame promising an action it does not
have, and native rendering is not a reason to start promising one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from eawf.kernel.projection.compute import ROUTE_COLLECTIONS
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE
from eawf.kernel.projection.operations import OPERATIONS_ROUTES
from eawf.kernel.projection.read_models import READ_MODEL_BY_KIND, ReadModelKind
from eawf.kernel.projection.verification import VERIFICATION_ROUTES
from eawf.runtime.daemon.methods.projection import ROUTE_READ_METHODS
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import KEY_NAMES
from eawf.surfaces.tui.console.keymap import route_keys
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session

#: Where the grid is recorded. One file, read by this suite alone, so a wave that binds a
#: route moves its row here in the same commit.
MANIFEST = Path(__file__).resolve().parents[4] / "fixtures/console/coverage-manifest.json"

#: The prototype registers the epoch-1 mode renders from.
FIXTURE_ROOT = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"

#: The wave id a declared hole names. Zero-padded and at least two digits wide, which is
#: the symbol the commit lint and the roadmap both spell.
WAVE_ID = re.compile(r"^P\d{2,}-I\d{2,}-W\d{2,}$")

#: The one route that is not a hole and never will be bound.
UNPROJECTABLE_ROUTE = "entry"

#: A route the grid still lists as a hole, which the teeth cases are exercised on. A
#: bound route would pass the very check those cases exist to red.
HOLE_ROUTE = "settings.stack"

#: The route whose epoch-1 footer this suite presses every key of.
TRUST_ROUTE = "trust"


class CoverageRow(BaseModel):
    """One route's row in the coverage grid.

    Attributes:
        route: The registry route id.
        read_model: The read model the route renders.
        binding: Whether a projection serves the route today, a later wave will, or none
            ever can.
        bound_by: The wave that binds a hole; absent on every other binding.
        reason: Why an unprojectable route carries no projection; absent otherwise.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    route: Annotated[str, Field(min_length=1)]
    read_model: ReadModelKind
    binding: Literal["bound", "hole", "unprojectable"]
    bound_by: str | None = None
    reason: Annotated[str, Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def _binding_carries_its_declaration(self) -> Self:
        """Refuse a row whose binding and declaration disagree.

        Raises:
            ValueError: A hole names no wave or names something that is not a wave id, a
                bound row names one anyway, or an unprojectable row states no reason.
        """
        problems: list[str] = []
        if self.binding == "hole" and (self.bound_by is None or not WAVE_ID.match(self.bound_by)):
            problems.append(f"route {self.route!r} is an undeclared hole: it names no wave")
        if self.binding != "hole" and self.bound_by is not None:
            problems.append(f"route {self.route!r} is {self.binding} but names a binding wave")
        if (self.binding == "unprojectable") != (self.reason is not None):
            problems.append(f"route {self.route!r} states a reason only when unprojectable")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class CoverageManifest(BaseModel):
    """The whole grid, as it is recorded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["coverage-grid/1.0"]
    routes: Annotated[tuple[CoverageRow, ...], Field(min_length=1)]


def load_manifest(document: Any = None) -> CoverageManifest:
    """Return the validated grid, from ``document`` or from the recorded file.

    Raises:
        pydantic.ValidationError: The grid is not a coverage manifest.
    """
    if document is None:
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return CoverageManifest.model_validate(document)


def coverage_defects(manifest: CoverageManifest) -> tuple[str, ...]:
    """Return every way the grid disagrees with the console, sorted by route.

    Args:
        manifest: The validated grid.

    Returns:
        One message per disagreeing route: a registry route the grid does not list, a
        grid row for no registry route, a row naming the wrong read model, and a row
        whose binding is not what the projection table says. Empty when they agree.
    """
    listed = {row.route: row for row in manifest.routes}
    defects: list[str] = []
    repeated = sorted(
        {
            row.route
            for row in manifest.routes
            if [r.route for r in manifest.routes].count(row.route) > 1
        }
    )
    defects += [f"route {route!r} is listed twice" for route in repeated]
    for route in sorted(set(listed) | set(REGISTRY.ids)):
        row = listed.get(route)
        if row is None:
            defects.append(f"route {route!r} is unlisted: the grid is not total")
            continue
        spec = REGISTRY.by_id.get(route)
        if spec is None:
            defects.append(f"route {route!r} is listed but the registry does not hold it")
            continue
        declared = REGISTRY.read_models[route]
        if row.read_model is not declared:
            defects.append(f"route {route!r} lists {row.read_model} but renders {declared}")
        served = spec.key in ROUTE_COLLECTIONS
        if served and row.binding != "bound":
            defects.append(f"route {route!r} is served by a projection but listed {row.binding}")
        if not served and row.binding == "bound":
            defects.append(f"route {route!r} is listed bound but no projection serves it")
    return tuple(defects)


def _fixture() -> Fixture:
    """Return the tracked prototype registers the epoch-1 mode renders from."""
    return load_fixture(FIXTURE_ROOT)


class _Host:
    """The dispatcher's host: a held clock and a quit that records it was asked."""

    def __init__(self) -> None:
        self.quits = 0
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """Record that the console was asked to end."""
        self.quits += 1


#: The dispatcher name of every key the bar prints under another name.
_DISPATCHER_NAME: dict[str, str] = {name: key for key, name in KEY_NAMES.items()}


def _advertised(route: str, fixture: Fixture) -> list[str]:
    """Return the dispatcher key name of every key the route's footer advertises."""
    session = Session()
    session.route = route
    names: list[str] = []
    for entry in route_keys(session, fixture, route):
        for token in entry.keys:
            glyphs = list(token) if all(ch in _DISPATCHER_NAME for ch in token) else [token]
            names.extend(_DISPATCHER_NAME.get(glyph, glyph) for glyph in glyphs)
    return names


def _row(route: str, **changes: Any) -> dict[str, Any]:
    """Return the recorded row of ``route`` with ``changes`` applied."""
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    row = next(r for r in document["routes"] if r["route"] == route)
    return {**row, **changes}


def _manifest_without(route: str) -> dict[str, Any]:
    """Return the recorded grid with ``route``'s row dropped."""
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["routes"] = [r for r in document["routes"] if r["route"] != route]
    return document


# ---------- the grid is total, and agrees with the console ----------


def test_the_recorded_grid_validates() -> None:
    """The shipped file is a coverage manifest, not merely well-formed JSON."""
    manifest = load_manifest()
    assert manifest.schema_version == "coverage-grid/1.0"
    assert len(manifest.routes) == len(REGISTRY.ids)


def test_the_recorded_grid_lists_every_registry_route_exactly_once() -> None:
    """A route the grid does not list is a route nobody has decided about."""
    listed = [row.route for row in load_manifest().routes]
    assert sorted(listed) == sorted(REGISTRY.ids)
    assert len(listed) == len(set(listed))


def test_the_recorded_grid_has_no_defects() -> None:
    """The grid and the console agree in both directions today."""
    assert coverage_defects(load_manifest()) == ()


@pytest.mark.parametrize("route", sorted(REGISTRY.ids))
def test_every_row_names_the_read_model_its_route_renders(route: str) -> None:
    """The grid repeats the registry binding, and a drifted row fails here."""
    row = next(r for r in load_manifest().routes if r.route == route)
    assert row.read_model is REGISTRY.read_models[route]
    assert REGISTRY.by_id[route].key in READ_MODEL_BY_KIND[row.read_model].routes


def test_the_bound_rows_are_exactly_the_routes_a_projection_serves() -> None:
    """A row calling itself bound is checked against the table that binds routes."""
    bound = {row.route for row in load_manifest().routes if row.binding == "bound"}
    served = {spec.id for spec in REGISTRY.routes if spec.key in ROUTE_COLLECTIONS}
    assert bound == served
    assert len(bound) == 31


@pytest.mark.parametrize("route", sorted(VERIFICATION_ROUTES) + sorted(OPERATIONS_ROUTES))
def test_this_waves_routes_are_listed_bound_and_served(route: str) -> None:
    """The seven routes this wave binds moved out of the hole list in the same commit."""
    row = next(r for r in load_manifest().routes if r.route == route)
    assert row.binding == "bound"
    assert READ_METHOD_TEMPLATE.format(route=route) in ROUTE_READ_METHODS


def test_every_hole_names_the_wave_that_binds_it() -> None:
    """A hole naming no wave is a permanent gap wearing a temporary label."""
    holes = [row for row in load_manifest().routes if row.binding == "hole"]
    assert holes
    for row in holes:
        assert row.bound_by is not None
        assert WAVE_ID.match(row.bound_by), row.bound_by


def test_the_unprojectable_row_is_the_entry_layer_alone() -> None:
    """One read model no projection carries, and one route that renders it."""
    rows = [row for row in load_manifest().routes if row.binding == "unprojectable"]
    assert [row.route for row in rows] == [UNPROJECTABLE_ROUTE]
    assert rows[0].read_model is ReadModelKind.PROCESS_FRAME
    assert not READ_MODEL_BY_KIND[rows[0].read_model].projection_backed
    assert rows[0].reason
    assert UNPROJECTABLE_ROUTE not in ROUTE_COLLECTIONS


def test_the_grid_accounts_for_every_route_exactly_once_across_the_three_bindings() -> None:
    """Bound plus holes plus the one unprojectable route is the whole registry."""
    rows = load_manifest().routes
    counted = sum(1 for row in rows if row.binding in ("bound", "hole", "unprojectable"))
    assert counted == len(REGISTRY.ids)


# ---------- the check has teeth ----------


def test_an_unlisted_route_is_reported() -> None:
    """Dropping a row makes the grid non-total, and the check says which route."""
    defects = coverage_defects(load_manifest(_manifest_without("export")))
    assert defects == ("route 'export' is unlisted: the grid is not total",)


def test_a_row_for_no_registry_route_is_reported() -> None:
    """A grid row the registry does not hold is a route that was renamed or removed."""
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["routes"].append(
        {
            "route": "spike.hole",
            "read_model": "search_page",
            "binding": "hole",
            "bound_by": "P33-I01-W99",
        }
    )
    defects = coverage_defects(load_manifest(document))
    assert defects == ("route 'spike.hole' is listed but the registry does not hold it",)


def test_a_row_naming_the_wrong_read_model_is_reported() -> None:
    """The grid repeats the registry binding; a drifted row is a grid nobody can trust."""
    document = _manifest_without("health")
    document["routes"].append(_row("health", read_model="search_page"))
    defects = coverage_defects(load_manifest(document))
    assert defects == ("route 'health' lists search_page but renders health_view",)


def test_a_served_route_listed_as_a_hole_is_reported() -> None:
    """A route a projection already serves cannot still be waiting on a wave."""
    document = _manifest_without("trust")
    document["routes"].append(_row("trust", binding="hole", bound_by="P33-I01-W99"))
    defects = coverage_defects(load_manifest(document))
    assert defects == ("route 'trust' is served by a projection but listed hole",)


def test_a_hole_listed_as_bound_is_reported() -> None:
    """Calling an unbound route bound is the claim the grid exists to refuse."""
    document = _manifest_without(HOLE_ROUTE)
    document["routes"].append(_row(HOLE_ROUTE, binding="bound", bound_by=None))
    defects = coverage_defects(load_manifest(document))
    assert defects == (f"route {HOLE_ROUTE!r} is listed bound but no projection serves it",)


def test_a_route_listed_twice_is_reported() -> None:
    """One route with two rows is a grid that can claim two different things at once."""
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["routes"].append(_row("search"))
    assert "is listed twice" in coverage_defects(load_manifest(document))[0]


def test_an_undeclared_hole_is_refused_at_validation() -> None:
    """A hole with no wave never reaches the comparison; the row itself is invalid."""
    document = _manifest_without(HOLE_ROUTE)
    row = _row(HOLE_ROUTE, bound_by=None)
    document["routes"].append({**row, "binding": "hole"})
    with pytest.raises(ValidationError, match="undeclared hole"):
        load_manifest(document)


def test_a_hole_naming_something_that_is_not_a_wave_is_refused() -> None:
    """The wave id is the symbol, not a free-text promise."""
    document = _manifest_without(HOLE_ROUTE)
    document["routes"].append(_row(HOLE_ROUTE, bound_by="later"))
    with pytest.raises(ValidationError, match="undeclared hole"):
        load_manifest(document)


def test_a_bound_row_naming_a_wave_is_refused() -> None:
    """A bound route has no wave left to wait for."""
    document = _manifest_without("trust")
    document["routes"].append(_row("trust", bound_by="P33-I01-W99"))
    with pytest.raises(ValidationError, match="names a binding wave"):
        load_manifest(document)


def test_an_unprojectable_row_without_a_reason_is_refused() -> None:
    """The one route no projection carries has to say why, or it reads as an oversight."""
    document = _manifest_without(UNPROJECTABLE_ROUTE)
    document["routes"].append(_row(UNPROJECTABLE_ROUTE, reason=None))
    with pytest.raises(ValidationError, match="states a reason only when unprojectable"):
        load_manifest(document)


def test_an_unknown_key_in_a_row_is_refused() -> None:
    """The grid forbids extras, so a typo is a failure rather than a silently dropped key."""
    document = _manifest_without("search")
    document["routes"].append(_row("search", owner="somebody"))
    with pytest.raises(ValidationError):
        load_manifest(document)


def test_an_empty_grid_is_refused() -> None:
    """The empty boundary: a grid with no rows covers nothing and claims everything."""
    with pytest.raises(ValidationError):
        load_manifest({"schema_version": "coverage-grid/1.0", "routes": []})


def test_a_grid_of_another_schema_version_is_refused() -> None:
    """A version this suite does not read is refused rather than read as if it were."""
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["schema_version"] = "coverage-grid/2.0"
    with pytest.raises(ValidationError):
        load_manifest(document)


def test_a_grid_that_is_not_a_mapping_is_refused() -> None:
    """The wrong-type boundary, refused at the loader rather than downstream."""
    with pytest.raises(ValidationError):
        load_manifest([])


# ---------- the epoch-1 trust mode still resolves every advertised key ----------


def test_the_epoch_one_trust_mode_resolves_every_key_its_footer_advertises() -> None:
    """A footer that advertises a key nothing handles promises an action it does not have."""
    fixture = _fixture()
    unclaimed: list[str] = []
    for key in _advertised(TRUST_ROUTE, fixture):
        session = Session()
        session.route = TRUST_ROUTE
        session.subj_id = REGISTRY.by_id[TRUST_ROUTE].fixed_subject
        render_route(View(session=session, fixture=fixture, w=120, h=30))
        ctx = Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30)
        dispatch(ctx, key, False)
        if session.trace is None or session.trace.endswith("unclaimed"):
            unclaimed.append(f"{TRUST_ROUTE}:{key}")
    assert not unclaimed, f"advertised but unhandled: {', '.join(unclaimed)}"


def test_the_trust_footer_advertises_at_least_one_key() -> None:
    """A frame with no footer key is one an operator cannot leave."""
    assert _advertised(TRUST_ROUTE, _fixture())


def test_an_unadvertised_key_on_trust_is_recorded_as_unclaimed() -> None:
    """The claim check has teeth: a key the route does not bind reads as unclaimed."""
    fixture = _fixture()
    session = Session()
    session.route = TRUST_ROUTE
    render_route(View(session=session, fixture=fixture, w=120, h=30))
    dispatch(Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30), "Q", False)
    assert session.trace is not None
    assert session.trace.endswith("unclaimed")
