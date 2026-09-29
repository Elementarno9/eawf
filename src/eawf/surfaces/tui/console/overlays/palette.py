"""The command palette overlay: routes first, then the entities the query matches."""

from __future__ import annotations

from collections.abc import Sequence

from eawf.kernel.projection.compute import ProjectionRow
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, build, header
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.palette import CRUMB, PAIRS, Hit, PaletteEntity, hits, palette_rows
from eawf.surfaces.tui.console.registry import COLLECTION_ROUTES, REGISTRY, route_for_id

# Entities the prototype registers do not carry, so every route stays reachable by name.
PALETTE_NAMED: tuple[PaletteEntity, ...] = (
    PaletteEntity(id="CAM-0001", route="campaign", what="Provider drift · REVIEW"),
    PaletteEntity(id="CLM-0004", route="evidence", what="normalizer preserves ordering"),
)


def entities(fixture: Fixture, rows: Sequence[ProjectionRow] = ()) -> list[PaletteEntity]:
    """Return every entity the palette can open: fleet Runs, stored records, named entities.

    The named entities are the prototype's own, so a fixture holding no prototype rows
    names none of them. A linked console names the spine records its held projections
    carry first -- Tracks, Milestones, Batches, Tasks, Runs, Campaigns -- each opening the
    route its own collection lives on.

    Args:
        fixture: The registers.
        rows: Every row the link's held projections carry; empty with no link.
    """
    out: list[PaletteEntity] = []
    seen: set[str] = set()
    for held in rows:
        route = COLLECTION_ROUTES.get(held.collection)
        if route is not None and held.key not in seen:
            seen.add(held.key)
            out.append(PaletteEntity(id=held.key, route=route, what=held.title or _untitled(held)))
    for row in fixture.proto.fleet:
        if row.run and row.run not in seen:
            seen.add(row.run)
            out.append(PaletteEntity(id=row.run, route="run.detail", what=row.task or "run"))
    for entity_id in fixture.detail:
        if entity_id in seen:
            continue
        seen.add(entity_id)
        route = route_for_id(entity_id)
        if route:
            what = dv.field_of(fixture, entity_id, "NAME") or REGISTRY.route_word(route)
            out.append(PaletteEntity(id=entity_id, route=route, what=what))
    for named in PALETTE_NAMED if fixture.prototype else ():
        if named.id not in seen and named.route in REGISTRY.by_id:
            seen.add(named.id)
            out.append(named)
    return out


def _untitled(held: ProjectionRow) -> str:
    """Return what an untitled record is shown as: its kind and its state.

    A route id is the console's own address and names nothing an operator filed, so a
    record with no title is described by the facts its row does state. Its parent's id is
    left out: the text is matched against a query, and a Batch is not a hit for its
    Milestone's id.
    """
    kind = held.collection.value.replace("_", " ").capitalize()
    return f"{kind} · {value_cell(held.status).slot}"


def palette_hits(query: str, fixture: Fixture, rows: Sequence[ProjectionRow] = ()) -> list[Hit]:
    """Return the palette rows ``query`` selects, over the held ``rows`` as well."""
    return hits(query, entities(fixture, rows))


def render(view: View) -> list[str]:
    """Return the palette overlay."""
    s, w, h = view.session, view.w, view.h
    found = palette_hits(s.pq, view.fixture, view.rows)
    rows = [header(view, CRUMB), *palette_rows(s, found, w=w, h=h)]
    return build(view, rows, keybar(PAIRS, w))
