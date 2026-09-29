"""search: entity hits for one query, with exact counts; event text is never searched.

The native frame answers the palette's query over the entity registers the read model
holds: a hit is a record whose key or title carries the query, sorted by kind then id, and
each hit says what matched and where. The counts by kind are counted off the hits, so
they are exact while the projection is complete and say ``known`` when it is not. An empty
query matches every record, which is the register itself, and the sub line says so once.
"""

from __future__ import annotations

from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import NO_VALUE
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin, window_rows
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.renderers.read_model import (
    cursor_note,
    finish,
    label,
    native_head,
    noun,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.spine import held

_KEYS = route_pairs("search")
_HITS: tuple[tuple[str, str, str], ...] = pt.SEARCH_HITS


def matched(row: SpineRow, query: str) -> str | None:
    """Return what of ``row`` carries ``query``, or ``None`` when nothing does.

    Args:
        row: The record searched.
        query: The palette query, compared without case; empty matches every record.

    Returns:
        ``"id"`` or ``"name"``, the field the query was found in, or the no-value mark for
        an empty query, which matched nothing in particular; the frame's sub line says
        once that every record is listed.
    """
    needle = query.strip().lower()
    if not needle:
        return NO_VALUE
    if needle in row.key.lower():
        return "id"
    if row.title is not None and needle in row.title.lower():
        return "name"
    return None


def native_frame(view: View, spine: SpineView) -> list[str]:
    """Return the Search frame drawn from the corpus the daemon served.

    Args:
        view: The render being built; its session carries the palette query.
        spine: The diagnostics corpus at the committed cursor, sorted by kind then id.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    query = s.pq
    hits = [(row, what) for row in spine.rows if (what := matched(row, query)) is not None]
    cursor = dv.sel_in(s, len(hits))
    s.sel_id = hits[cursor][0].key if hits else None
    kinds: dict[str, int] = {}
    for row, _what in hits:
        kinds[row.collection.value] = kinds.get(row.collection.value, 0) + 1
    exact = "every count exact" if spine.complete else "every count known"
    asked = f"query “{query}”" if query.strip() else "no query · every record"
    top = native_head(
        view,
        spine,
        crumb_text=route_crumb(view, spine, "Search"),
        summary=f"{asked} · entities only · {dv.plural(len(hits), 'hit')}" + cursor_note(spine),
    )
    grid = Grid([18, 18, 0])
    # each kind is named before its count; noun() owns the register plural at any count
    stated = " · ".join(
        f"{noun(2, name).partition(' ')[2]} {group(n)}" for name, n in kinds.items()
    )
    body = [
        label("QUERY", f"{query}▏"),
        label("KINDS", stated or "0 hits in any register"),
        thin(w),
        grid.head(["HIT", "WHAT MATCHED", "WHERE"]),
    ]
    foot_rows = 4
    win = window_rows(view, total=len(hits), cursor=cursor, chrome=len(top) + len(body) + foot_rows)
    for i, (row, what) in enumerate(hits[win.start : win.stop], start=win.start):
        where = row.collection.value + (f" · {row.title}" if row.title else "")
        body.append(grid.row([row.key, what, where], i == cursor, w))
    if not hits:
        body.append("   0 hits · no record's key or title carries the query")
    shown = win.stop - win.start
    body += [
        thin(w),
        label("WINDOW", f"{shown} of {len(hits)} · sorted by kind, then id · {exact}"),
        label("SCOPE", "Entities only — event text is not searched."),
    ]
    return finish(view, top, body, _KEYS)


def render(view: View) -> list[str]:
    """Return the Search frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
    s, w = view.session, view.w
    grid = Grid([18, 18, 0])
    dv.sel_in(s, len(_HITS))
    body = [
        " QUERY        replay ▏",
        " KINDS        runs 6 · tasks 4 · batches 2 · milestones 1 · claims 1",
        thin(w),
        grid.head(["HIT", "WHAT MATCHED", "WHERE"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(_HITS))
    body.extend(
        [
            thin(w),
            " WINDOW       6 of 14 · sorted by kind, then id · every count exact",
            " SCOPE        Entities only — event text is not searched.",
        ]
    )
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Search",
        ctx="query “replay” · entities only · 14 hits, exact",
        body=body,
        keys=_KEYS,
    )
