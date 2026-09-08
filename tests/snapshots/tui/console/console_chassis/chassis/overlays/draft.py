"""The draft card: what the draft under the backlog cursor still needs before it can be
promoted, read from the same rows the route lists (proto-app's overlayDraft)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...chassis.derive import filter_of, sel_in
from ...chassis.frame import bar, build, header_row, keybar, thin
from ...chassis.width import pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_FIELDS = ("criteria", "owner", "batch")
_KEYS: list[tuple[str, str]] = [
    ("↑↓", "field"),
    ("Enter", "set"),
    ("p", "promote"),
    ("x", "defer"),
    ("Esc", "back"),
]

Draft = tuple[str, str, str, dict[str, int]]


def _all_drafts(fixture: Fixture) -> list[Draft]:
    rows: list[Draft] = []
    for r in fixture.g.BL_DRAFTS:
        st = str(r[2])
        have = {f: (0 if f in st else 1) for f in _FIELDS}
        rows.append((f"{r[0]} {r[1]}", st, str(r[3]), have))
    return rows


def backlog_drafts(session: Session, fixture: Fixture) -> list[Draft]:
    """One filter-aware list for the route and the card: each draft's three fields are read
    from the STATUS the route already prints, so the card can never describe a hidden row."""
    rows = _all_drafts(fixture)
    q = filter_of(session).lower()
    return [d for d in rows if q in (d[0] + d[1]).lower()] if q else rows


def list_of(xs: list[str]) -> str:
    """A list of three reads with commas and one and, not two ands."""
    if not xs:
        return "nothing"
    if len(xs) < 2:
        return xs[0]
    return ", ".join(xs[:-1]) + " and " + xs[-1]


def _f_row(session: Session, field: str, what: str, by: str | None, i: int) -> str:
    on = (session.draft_field or 0) == i
    return (
        " "
        + pad("", 9)
        + ("▸ " if on else "  ")
        + pad(field, 9)
        + pad(what, 34)
        + (by or "∅ not answered yet")
    )


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    drafts = backlog_drafts(s, fixture)
    sel_in(s, len(drafts))
    if not drafts:
        # a filter that empties the list still leaves a card to draw rather than a crash
        drafts = _all_drafts(fixture)
    sd = drafts[s.sel] if s.sel < len(drafts) else drafts[0]
    have: dict[str, Any] = sd[3]
    s.draft_field = max(0, min(2, s.draft_field or 0))
    did = sd[0].split(" ")[0]
    missing = [f for f in _FIELDS if not have[f]]
    s.draft_miss = list(missing)
    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ {fixture.scope} ▸ Backlog ▸ {did}", w),
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
        _f_row(
            s,
            "criteria",
            "what proof will show it is done",
            "EVT-2249 · the ledger check" if have["criteria"] else None,
            0,
        ),
        _f_row(
            s, "owner", "who answers for it", "you · since Jul 28" if have["owner"] else None, 1
        ),
        _f_row(
            s,
            "batch",
            "where it will be integrated",
            "BAT-0002 Bound replay" if have["batch"] else None,
            2,
        ),
        thin(w),
        " NOT       Promoting does not dispatch it and does not claim a run.",
        " "
        + pad("", 9)
        + (
            f"p is refused while {list_of(missing)} {'are' if len(missing) > 1 else 'is'} unanswered."
            if missing
            else "p promotes it into BAT-0002, where the batch decides when it runs."
        ),
    ]
    return build(s, rows, keybar(_KEYS, w), w, h)
