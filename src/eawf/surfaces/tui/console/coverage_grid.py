"""The console coverage grid: every registry route, listed once under what serves it.

The grid makes "the console is bound" falsifiable. A route nobody wired and a route
somebody decided not to wire are both simply absent until a total list names each one
with its binding: ``bound`` (a document collection projected into rows),
``served_off_document`` (a read verb with no collection behind it), ``hole`` (a named
later wave will serve it) or ``unprojectable`` (no projection can ever carry it).

The grid is recorded in one file and regenerated from the console's own tables. The
release checkpoint reads :func:`checkpoint_refusals`: a recorded grid that is not its own
regeneration was patched by hand or has drifted from the console, and the checkpoint is
refused until the grid reconciles with the route registry.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.projection.compute import ROUTE_COLLECTIONS
from eawf.kernel.projection.read_models import ReadModelKind
from eawf.kernel.projection.settings import SETTINGS_ROUTES
from eawf.surfaces.tui.console.registry import REGISTRY

#: Where the grid is recorded, relative to the repository root.
COVERAGE_MANIFEST_PATH: Final = Path("tests/fixtures/console/coverage-manifest.json")

#: The wave id a declared hole names: zero-padded and at least two digits wide.
WAVE_ID: Final = re.compile(r"^P\d{2,}-I\d{2,}-W\d{2,}$")

#: The one route that is not a hole and never will be bound.
UNPROJECTABLE_ROUTE: Final = "entry"

#: The routes a read verb and a composing call site serve without a document collection.
#: The settings composer gates on this very tuple, so the grid names what it accepts.
OFF_DOCUMENT_ROUTES: Final[frozenset[str]] = frozenset(SETTINGS_ROUTES)

#: The routes the grid may list as holes, each owed by a named wave. Empty: every
#: projectable route is served. A hole the regenerated grid carries that is not named
#: here is an undeclared hole, and a name here the grid no longer carries is stale.
DECLARED_HOLES: frozenset[str] = frozenset()


class CoverageRow(BaseModel):
    """One route's row in the coverage grid.

    Attributes:
        route: The registry route id.
        read_model: The read model the route renders.
        binding: What serves the route -- a document collection, a read verb with no
            collection behind it, a named later wave, or nothing that ever can.
        bound_by: The wave that binds a hole; absent on every other binding.
        reason: Why an unprojectable route carries no projection; absent otherwise.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    route: Annotated[str, Field(min_length=1)]
    read_model: ReadModelKind
    binding: Literal["bound", "served_off_document", "hole", "unprojectable"]
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


def coverage_defects(manifest: CoverageManifest) -> tuple[str, ...]:
    """Return every way the grid disagrees with the console, sorted by route.

    Args:
        manifest: The validated grid.

    Returns:
        One message per disagreeing route: a registry route the grid does not list, a
        grid row for no registry route, a row naming the wrong read model, and a row
        whose binding is not what the projection tables say. Empty when they agree.
    """
    listed = {row.route: row for row in manifest.routes}
    routes = [row.route for row in manifest.routes]
    defects = [
        f"route {route!r} is listed twice"
        for route in sorted(set(routes))
        if routes.count(route) > 1
    ]
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
        off_document = spec.id in OFF_DOCUMENT_ROUTES
        if served and row.binding != "bound":
            defects.append(f"route {route!r} is served by a projection but listed {row.binding}")
        elif off_document and row.binding != "served_off_document":
            defects.append(f"route {route!r} is served off document but listed {row.binding}")
        elif not served and row.binding == "bound":
            defects.append(f"route {route!r} is listed bound but no projection serves it")
        elif not off_document and row.binding == "served_off_document":
            defects.append(f"route {route!r} is listed served off document but no verb serves it")
    return tuple(defects)


def regenerate_grid(recorded: CoverageManifest) -> CoverageManifest:
    """Return the grid the console's own tables produce, in route order.

    Every binding is derived: ``bound`` from the collection table, ``served_off_document``
    from the settings routes, ``unprojectable`` for the entry layer and ``hole`` for every
    other route. Only what a table cannot know is carried over from ``recorded``: the
    unprojectable route's reason and the wave a hole names.

    Args:
        recorded: The grid as it is recorded.

    Returns:
        The regenerated grid.

    Raises:
        pydantic.ValidationError: A hole the recorded grid does not declare, which
            regenerates as a hole naming no wave.
    """
    carried = {row.route: row for row in recorded.routes}
    rows: list[CoverageRow] = []
    for spec in sorted(REGISTRY.routes, key=lambda spec: spec.id):
        extra: dict[str, str | None]
        prior = carried.get(spec.id)
        if spec.key in ROUTE_COLLECTIONS:
            binding, extra = "bound", {}
        elif spec.id in OFF_DOCUMENT_ROUTES:
            binding, extra = "served_off_document", {}
        elif spec.id == UNPROJECTABLE_ROUTE:
            binding, extra = "unprojectable", {"reason": prior.reason if prior else None}
        else:
            binding, extra = "hole", {"bound_by": prior.bound_by if prior else None}
        rows.append(
            CoverageRow.model_validate(
                {
                    "route": spec.id,
                    "read_model": REGISTRY.read_models[spec.id],
                    "binding": binding,
                    **extra,
                }
            )
        )
    return CoverageManifest(schema_version=recorded.schema_version, routes=tuple(rows))


def checkpoint_refusals(document: Any) -> tuple[str, ...]:
    """Return why the checkpoint refuses the grid *document*, or nothing when it passes.

    Args:
        document: The recorded grid, as parsed from its file.

    Returns:
        One line per refusal: every :func:`coverage_defects` message, a recorded grid
        that is not its regeneration, and a hole set that is not :data:`DECLARED_HOLES`.

    Raises:
        pydantic.ValidationError: The document is not a grid, or holds a cell that is
            neither bound, served off document, unprojectable with a reason, nor a hole
            naming its wave -- an unclassified cell.
    """
    recorded = CoverageManifest.model_validate(document)
    refusals = list(coverage_defects(recorded))
    regenerated = regenerate_grid(recorded)
    if regenerated != recorded:
        refusals.append("the recorded grid is not its regeneration: it was patched by hand")
    holes = {row.route for row in regenerated.routes if row.binding == "hole"}
    if holes != DECLARED_HOLES:
        refusals.append(f"holes {sorted(holes)} do not match the declared {sorted(DECLARED_HOLES)}")
    return tuple(refusals)


__all__ = [
    "COVERAGE_MANIFEST_PATH",
    "DECLARED_HOLES",
    "OFF_DOCUMENT_ROUTES",
    "UNPROJECTABLE_ROUTE",
    "WAVE_ID",
    "CoverageManifest",
    "CoverageRow",
    "checkpoint_refusals",
    "coverage_defects",
    "regenerate_grid",
]
