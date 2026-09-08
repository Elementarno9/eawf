"""The command palette: one alphabetical route list, then the entities the same query
matches, shown through a WINDOW that follows the cursor with a `... (N more)` count at
whichever edge hides rows (proto-app.js paletteEntities / paletteAll / paletteView /
overlayPalette)."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from ...chassis.derive import field_of
from ...chassis.frame import Fixed, bar, build, header_row, keybar
from ...chassis.registry import ROUTE_IDS, ROUTE_LIST, ROUTE_WORD, route_for_id
from ...chassis.width import pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


class Hit(TypedDict):
    kind: str
    id: str
    to: str
    what: str


# the entities the fleet fixture does not carry, so every removed route stays reachable by name
PALETTE_NAMED: tuple[Hit, ...] = (
    {"kind": "entity", "id": "CAM-0001", "to": "campaign", "what": "Provider drift · REVIEW"},
    {"kind": "entity", "id": "CLM-0004", "to": "evidence", "what": "normalizer preserves ordering"},
)

RULE_DASHED = "┄"


def route_word(route: str) -> str:
    return ROUTE_WORD.get(route, route)


def _bound(route: str) -> bool:
    # the prototype's `R[to]` test: every registered route is bound in the spec renderer
    return route in ROUTE_IDS


def entities(fixture: Fixture) -> list[Hit]:
    out: list[Hit] = []
    seen: set[str] = set()
    for f in fixture.proto.fleet:
        if not f.run or f.run in seen:
            continue
        seen.add(f.run)
        out.append({"kind": "entity", "id": f.run, "to": "run.detail", "what": f.task or "run"})
    for entity_id in fixture.detail:
        if entity_id in seen:
            continue
        seen.add(entity_id)
        to = route_for_id(entity_id)
        if not to:
            continue
        out.append(
            {
                "kind": "entity",
                "id": entity_id,
                "to": to,
                "what": field_of(fixture, entity_id, "NAME") or route_word(to),
            }
        )
    for e in PALETTE_NAMED:
        if e["id"] in seen or not _bound(e["to"]):
            continue
        seen.add(e["id"])
        out.append(dict(e))  # type: ignore[arg-type]
    return out


def hits(session: Session, fixture: Fixture) -> list[Hit]:
    """Routes first, then entities ranked by HOW they matched: exact id, id prefix, id
    substring, and only then a description mention."""
    q = (session.pq or "").lower()
    routes: list[Hit] = [
        {"kind": "route", "id": route_word(r), "to": r, "what": "route"}
        for r in ROUTE_LIST
        if q in r or q in route_word(r)
    ]
    ranked: list[tuple[int, int, Hit]] = []
    for ix, e in enumerate(entities(fixture)):
        eid, what = e["id"].lower(), str(e["what"]).lower()
        rank = (
            0 if eid == q else 1 if eid.startswith(q) else 2 if q in eid else 3 if q in what else 9
        )
        if rank < 9:
            ranked.append((rank, ix, e))
    ranked.sort(key=lambda x: (x[0], x[1]))
    return routes + [e for _, _, e in ranked]


class View(TypedDict):
    sc: int
    n: int
    top: bool
    bot: bool


def _has_rule(sl: list[Hit]) -> bool:
    return any(
        i and h["kind"] == "entity" and sl[i - 1]["kind"] == "route" for i, h in enumerate(sl)
    )


def view(session: Session, fixture: Fixture, h: int) -> tuple[View, list[Hit]]:
    """The window onto the hits: the cursor is never off screen, and an indicator that
    would hide exactly one row is replaced by that row."""
    all_ = hits(session, fixture)
    body = h - 4
    if session.sel >= len(all_):
        session.sel = max(0, len(all_) - 1)
    if session.sel < 0:
        session.sel = 0

    def fit(sc: int) -> View:
        top = sc > 0
        n = min(len(all_) - sc, body - (1 if top else 0))
        while n > 1:
            sl = all_[sc : sc + n]
            rule = _has_rule(sl)
            bot = (sc + n) < len(all_)
            if n + (1 if rule else 0) + (1 if bot else 0) + (1 if top else 0) <= body:
                break
            n -= 1
        return {"sc": sc, "n": n, "top": sc > 0, "bot": (sc + n) < len(all_)}

    v = fit(session.pscroll or 0)
    for _ in range(5):
        if session.sel < v["sc"]:
            v = fit(session.sel)
            continue
        if session.sel >= v["sc"] + v["n"]:
            v = fit(max(0, session.sel - v["n"] + 1))
            continue
        break

    def rows(x: View) -> int:
        sl = all_[x["sc"] : x["sc"] + x["n"]]
        return (
            x["n"] + (1 if _has_rule(sl) else 0) + (1 if x["top"] else 0) + (1 if x["bot"] else 0)
        )

    if v["top"] and v["sc"] == 1:
        g = fit(0)
        if rows(g) <= body and g["n"] >= v["n"]:
            v = g
    if v["bot"] and (len(all_) - v["sc"] - v["n"]) == 1:
        wv: View = {"sc": v["sc"], "n": v["n"] + 1, "top": v["top"], "bot": False}
        if rows(wv) <= body:
            v = wv
    session.pscroll = v["sc"]
    return v, all_


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    v, all_ = view(session, fixture, h)
    items = all_[v["sc"] : v["sc"] + v["n"]]
    rows: list[str] = [
        header_row(session, fixture, " Eä ▸ palette", w),
        f" / {session.pq}▏",
        bar(w),
    ]

    def more_row(n: int) -> Fixed:
        return Fixed(pad(f"   ... ({n} more)", w))

    if v["top"]:
        rows.append(more_row(v["sc"]))
    for i, hit in enumerate(items):
        if i and hit["kind"] == "entity" and items[i - 1]["kind"] == "route":
            rows.append(Fixed(RULE_DASHED * w))
        mark = "▸ " if v["sc"] + i == session.sel else "  "
        tail = (
            ("" if _bound(hit["to"]) else "designed, not bound here")
            if hit["kind"] == "route"
            else hit["what"]
        )
        rows.append("   " + mark + pad(hit["id"], 22) + tail)
    if v["bot"]:
        rows.append(more_row(len(all_) - v["sc"] - v["n"]))
    if not all_:
        rows.append("   nothing matches")
    return build(
        session,
        rows,
        keybar([("type", "search"), ("↑↓", "row"), ("Enter", "go"), ("Esc", "close")], w),
        w,
        h,
    )
