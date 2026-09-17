"""The command palette: routes first, then entities, through a window that follows the cursor.

The palette reaches routes and entities, never verbs, so a verb stays behind the action
menu and its guards. Its route list is the registry's, alphabetical by route id and shown
by route word; the entities a query matches follow, ranked by how they matched. The window
never leaves the cursor off screen, marks each hidden edge with a ``… N above`` or
``… N below`` row, and shows the one row such a marker would hide instead of the marker.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.keybar import KEY, KeyEntry, Pair
from eawf.surfaces.tui.console.registry import REGISTRY, RouteRegistry
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import BRAND, CARET, CRUMB_SEP, RULE_HEAVY, RULE_PALETTE
from eawf.surfaces.tui.console.width import pad, rule

ELLIPSIS = "…"
PROMPT_CURSOR = "▏"
CRUMB = f" {BRAND}{CRUMB_SEP}palette"
PAIRS: tuple[Pair, ...] = (
    ("type", "search"),
    KEY["up"].pair(),
    KeyEntry("go", ("Enter",)).pair(),
    KeyEntry("close", ("Escape",)).pair(),
)
# Rows the frame spends outside the window: header, prompt, rule and keybar.
CHROME_ROWS = 4
# Cells the id column takes before a hit's description.
ID_COLUMN = 22
_INDENT = "   "
# The ranks an entity match can take, best first; a non-match takes none.
_EXACT, _PREFIX, _SUBSTRING, _MENTION = range(4)


class HitKind(StrEnum):
    """What a palette row opens."""

    ROUTE = "route"
    ENTITY = "entity"


@dataclass(frozen=True, slots=True, kw_only=True)
class PaletteEntity:
    """One entity the palette can open.

    Attributes:
        id: The entity id the row shows and a query matches.
        route: The registered route that renders the entity.
        what: The description a query may also mention.
    """

    id: str
    route: str
    what: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Hit:
    """One palette row.

    Attributes:
        kind: Whether the row opens a route or an entity.
        name: The route word or entity id the row shows.
        route: The route the row opens.
        subject: The entity the route opens onto; ``None`` for a route row.
        what: The text beside the name; empty for a route row.
    """

    kind: HitKind
    name: str
    route: str
    subject: str | None
    what: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Window:
    """The slice of the hits the palette shows.

    Attributes:
        start: Index of the first shown hit.
        count: How many hits are shown.
        above: Whether hits are hidden above the first shown one.
        below: Whether hits are hidden below the last shown one.
    """

    start: int
    count: int
    above: bool
    below: bool


def _rank(entity: PaletteEntity, query: str) -> int | None:
    """Return how ``entity`` matched ``query``, best rank lowest, or ``None``."""
    entity_id = entity.id.lower()
    if entity_id == query:
        return _EXACT
    if entity_id.startswith(query):
        return _PREFIX
    if query in entity_id:
        return _SUBSTRING
    if query in entity.what.lower():
        return _MENTION
    return None


def hits(
    query: str, entities: Iterable[PaletteEntity], *, registry: RouteRegistry = REGISTRY
) -> list[Hit]:
    """Return the palette rows for ``query``: matching routes, then ranked entities.

    Args:
        query: What the operator typed; matched case-insensitively.
        entities: The entities the palette can open; a repeated id keeps its first row.
        registry: The route rows the route list and entity routes resolve against.

    Returns:
        The route rows in registry order, then the entity rows by match rank and, within
        a rank, in the order ``entities`` gave them.

    Raises:
        ValueError: an entity names a route the registry does not hold.
    """
    q = query.lower()
    rows = [
        Hit(kind=HitKind.ROUTE, name=registry.route_word(r), route=r, subject=None, what="")
        for r in registry.route_list
        if q in r or q in registry.route_word(r)
    ]
    ranked: list[tuple[int, Hit]] = []
    seen: set[str] = set()
    for entity in entities:
        if entity.route not in registry.by_id:
            raise ValueError(f"palette entity {entity.id} names unregistered {entity.route!r}")
        if entity.id in seen:
            continue
        seen.add(entity.id)
        rank = _rank(entity, q)
        if rank is not None:
            hit = Hit(
                kind=HitKind.ENTITY,
                name=entity.id,
                route=entity.route,
                subject=entity.id,
                what=entity.what,
            )
            ranked.append((rank, hit))
    ranked.sort(key=lambda item: item[0])
    return rows + [hit for _, hit in ranked]


def _has_rule(shown: Sequence[Hit]) -> bool:
    """Return whether a dashed rule separates routes from entities inside ``shown``."""
    return any(
        before.kind == HitKind.ROUTE and after.kind == HitKind.ENTITY
        for before, after in pairwise(shown)
    )


def _rows_used(all_hits: Sequence[Hit], view: Window) -> int:
    """Return the rows ``view`` takes: its hits, the dashed rule and the edge markers."""
    shown = all_hits[view.start : view.start + view.count]
    return view.count + _has_rule(shown) + view.above + view.below


def _fit(all_hits: Sequence[Hit], start: int, body: int) -> Window:
    """Return the largest window from ``start`` whose rows fit ``body``."""
    total = len(all_hits)
    above = start > 0
    count = min(total - start, body - above)
    while count > 1:
        below = start + count < total
        shown = all_hits[start : start + count]
        if count + _has_rule(shown) + below + above <= body:
            break
        count -= 1
    return Window(start=start, count=count, above=above, below=start + count < total)


def window(all_hits: Sequence[Hit], *, sel: int, scroll: int, body: int) -> Window:
    """Return the window onto ``all_hits`` that keeps ``sel`` visible.

    Args:
        all_hits: Every palette row.
        sel: The cursor, already clamped into ``all_hits``.
        scroll: The previous window's first index, so the window moves only when the
            cursor leaves it.
        body: The rows the window may take.

    Raises:
        ValueError: ``body`` is not positive.
    """
    if body < 1:
        raise ValueError(f"the palette window needs at least one row, got {body}")
    total = len(all_hits)
    if not total:
        return Window(start=0, count=0, above=False, below=False)
    view = _fit(all_hits, scroll, body)
    for _ in range(5):
        if sel < view.start:
            view = _fit(all_hits, sel, body)
        elif sel >= view.start + view.count:
            view = _fit(all_hits, max(0, sel - view.count + 1), body)
        else:
            break
    if view.above and view.start == 1:
        top = _fit(all_hits, 0, body)
        if _rows_used(all_hits, top) <= body and top.count >= view.count:
            view = top
    if view.below and total - view.start - view.count == 1:
        grown = Window(start=view.start, count=view.count + 1, above=view.above, below=False)
        if _rows_used(all_hits, grown) <= body:
            view = grown
    return view


def _marker(n: int, edge: str, w: int) -> str:
    """Return the row that stands for ``n`` hidden hits at one edge."""
    return pad(f"{_INDENT}{ELLIPSIS} {group(n)} {edge}", w)


def palette_rows(session: Session, all_hits: Sequence[Hit], *, w: int, h: int) -> list[str]:
    """Return the palette's rows between its header and its keybar, each ``w`` cells.

    The cursor is clamped into the hits and the window's first index is written back to
    ``session.pscroll``, so the next render starts from the window the operator saw.

    Args:
        session: The session whose query, cursor and scroll are read and updated.
        all_hits: The rows :func:`hits` returned for ``session.pq``.
        w: The frame width in cells.
        h: The frame height in rows.

    Raises:
        ValueError: ``h`` leaves no row for the window, or ``w`` is negative.
    """
    total = len(all_hits)
    session.sel = min(max(session.sel, 0), max(total - 1, 0))
    view = window(all_hits, sel=session.sel, scroll=session.pscroll, body=h - CHROME_ROWS)
    session.pscroll = view.start
    rows = [pad(f" / {session.pq}{PROMPT_CURSOR}", w), rule(RULE_HEAVY, w)]
    if view.above:
        rows.append(_marker(view.start, "above", w))
    previous: Hit | None = None
    for index, hit in enumerate(all_hits[view.start : view.start + view.count], view.start):
        if previous is not None and previous.kind == HitKind.ROUTE and hit.kind == HitKind.ENTITY:
            rows.append(rule(RULE_PALETTE, w))
        mark = f"{CARET} " if index == session.sel else "  "
        rows.append(pad(f"{_INDENT}{mark}{pad(hit.name, ID_COLUMN)}{hit.what}", w))
        previous = hit
    if view.below:
        rows.append(_marker(total - view.start - view.count, "below", w))
    if not all_hits:
        rows.append(pad(f"{_INDENT}nothing matches", w))
    return rows
