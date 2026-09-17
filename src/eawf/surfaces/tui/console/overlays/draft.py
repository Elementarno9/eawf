"""The draft card: what the draft under the backlog cursor still needs before promotion.

It reads the same rows the route lists, so the card can never describe a hidden row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import pad

FIELDS: tuple[str, ...] = ("criteria", "owner", "batch")
_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "field"),
    ("Enter", "set"),
    ("p", "promote"),
    ("x", "defer"),
    ("Esc", "back"),
)


@dataclass(frozen=True, slots=True)
class Draft:
    """One draft: its id and name, its status text, its due date and the fields it has."""

    title: str
    status: str
    due: str
    has: Mapping[str, bool]


def _all_drafts(fixture: Fixture) -> list[Draft]:
    drafts = []
    for row in fixture.registers.bl_drafts:
        status = str(row[2])
        has = {f: f not in status for f in FIELDS}
        drafts.append(Draft(f"{row[0]} {row[1]}", status, str(row[3]), has))
    return drafts


def backlog_drafts(session: Session, fixture: Fixture) -> list[Draft]:
    """Return the drafts the route's filter leaves; each field's state is read from its status."""
    drafts = _all_drafts(fixture)
    query = dv.filter_of(session).lower()
    return [d for d in drafts if query in (d.title + d.status).lower()] if query else drafts


def list_of(items: list[str]) -> str:
    """Return ``items`` joined with commas and one final ``and``."""
    if not items:
        return "nothing"
    if len(items) < 2:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _field_row(session: Session, field: str, what: str, by: str | None, i: int) -> str:
    mark = "▸ " if session.draft_field == i else "  "
    return " " + pad("", 9) + mark + pad(field, 9) + pad(what, 34) + (by or "∅ not answered yet")


def render(view: View) -> list[str]:
    """Return the draft card."""
    s, fx, w = view.session, view.fixture, view.w
    drafts = backlog_drafts(s, fx)
    dv.sel_in(s, len(drafts))
    # a filter that empties the list still leaves a card to draw
    drafts = drafts or _all_drafts(fx)
    draft = drafts[s.sel] if s.sel < len(drafts) else drafts[0]
    s.draft_field = max(0, min(2, s.draft_field))
    did = draft.title.split(" ")[0]
    missing = [f for f in FIELDS if not draft.has[f]]
    s.draft_miss = list(missing)
    verb = "are" if len(missing) > 1 else "is"
    promotion = (
        f"p is refused while {list_of(missing)} {verb} unanswered."
        if missing
        else "p promotes it into BAT-0002, where the batch decides when it runs."
    )
    has = draft.has
    rows = [
        header(view, f" Eä ▸ {fx.scope} ▸ Backlog ▸ {did}"),
        " a draft is not work until it can be promoted",
        bar(w),
        " DRAFT     " + did,
        " DUE       It must be defined before its due scope closes.",
        thin(w),
        " NEEDS     " + (" · ".join(missing) if missing else "nothing — it can be promoted"),
        thin(w),
        " "
        + pad("PROMOTION", 9)
        + "  "
        + pad("FIELD", 9)
        + pad("WHAT IT ANSWERS", 34)
        + "ANSWERED BY",
        _field_row(
            s,
            "criteria",
            "what proof will show it is done",
            "EVT-2249 · the ledger check" if has["criteria"] else None,
            0,
        ),
        _field_row(
            s, "owner", "who answers for it", "you · since Jul 28" if has["owner"] else None, 1
        ),
        _field_row(
            s,
            "batch",
            "where it will be integrated",
            "BAT-0002 Bound replay" if has["batch"] else None,
            2,
        ),
        thin(w),
        " NOT       Promoting does not dispatch it and does not claim a run.",
        " " + pad("", 9) + promotion,
    ]
    return build(view, rows, keybar(_KEYS, w))
