"""The command palette overlay: routes first, then the entities the query matches."""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, build, header
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.palette import CRUMB, PAIRS, Hit, PaletteEntity, hits, palette_rows
from eawf.surfaces.tui.console.registry import REGISTRY, route_for_id

# Entities the registers do not carry, so every route stays reachable by name.
PALETTE_NAMED: tuple[PaletteEntity, ...] = (
    PaletteEntity(id="CAM-0001", route="campaign", what="Provider drift · REVIEW"),
    PaletteEntity(id="CLM-0004", route="evidence", what="normalizer preserves ordering"),
)


def entities(fixture: Fixture) -> list[PaletteEntity]:
    """Return every entity the palette can open: fleet Runs, stored records, named entities."""
    out: list[PaletteEntity] = []
    seen: set[str] = set()
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
    for named in PALETTE_NAMED:
        if named.id not in seen and named.route in REGISTRY.by_id:
            seen.add(named.id)
            out.append(named)
    return out


def palette_hits(query: str, fixture: Fixture) -> list[Hit]:
    """Return the palette rows ``query`` selects."""
    return hits(query, entities(fixture))


def render(view: View) -> list[str]:
    """Return the palette overlay."""
    s, w, h = view.session, view.w, view.h
    rows = [header(view, CRUMB), *palette_rows(s, palette_hits(s.pq, view.fixture), w=w, h=h)]
    return build(view, rows, keybar(PAIRS, w))
