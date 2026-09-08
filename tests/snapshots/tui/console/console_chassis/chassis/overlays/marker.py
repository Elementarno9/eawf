"""The marker card: one marker, captured at open time, with its record when the register
holds one and the absence stated in place when it does not."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import bar, build, header_row, keybar, thin
from ...chassis.renderers.timeline import TL_NAMES, marker_glyph
from ...chassis.width import pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_TRACK_ROW = re.compile(r"^\s{2}TRACK\s")
_RULE_ROW = re.compile(r"^[\s─│┼]*$")
_RULE_RUN = re.compile("─{4,}")
_LABEL = re.compile(r"^[A-Z][A-Z0-9 ]*\s*$")
_LEAD = re.compile(r"^[\s▸]+")


def _card_row(label: str, value: str, caret: bool = False) -> str:
    return " " + pad(label or "", 9) + ("▸ " if caret else "  ") + value


def _recast(row: str) -> str:
    """A record-body row re-laid on the card's nine-column label gutter."""
    if _RULE_ROW.match(row) or _RULE_RUN.search(row):
        return row
    head = row[2:14].replace("▸", " ")
    label = head.strip() if _LABEL.match(head) else ""
    tail = row[12:]
    at = tail.find("▸")
    caret = 0 <= at < 4
    return _card_row(label, _LEAD.sub("", tail), caret)


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    card = s.marker_card
    if not card:
        lane0 = TL_NAMES[s.sel] if s.sel < len(TL_NAMES) else TL_NAMES[0]
        card = {
            "id": s.timeline_marker or "MLS-0001",
            "lane": lane0,
            "glyph": marker_glyph(lane0, s.mark),
        }
    mid = str(card["id"])
    lane = str(card["lane"])
    g = str(card["glyph"])
    rec = fixture.record(mid)
    name = (dv.field_of(fixture, mid, "NAME") or "") if rec else ""
    forecast = g == "○"
    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ marker · {mid}", w),
        " " + (f"{name} · " if name else "") + lane + " · " + ("forecast" if forecast else "dated"),
        bar(w),
        _card_row(
            "MARKER",
            "○ forecast · a proposal, not a commitment"
            if forecast
            else "● dated · the date is committed, not forecast",
        ),
        _card_row("TRACK", lane),
    ]
    if rec:
        body = dv.record_body(s, fixture, list(rec), mid, rows[1])
        rows.extend(_recast(r) for r in body if not _TRACK_ROW.match(r))
    else:
        rows.extend(
            [
                thin(w),
                f" ∅ no state, dependencies or a bundle recorded for {mid}",
                "   the lane carries its date; nothing deeper is held for it",
            ]
        )
    rows.append(thin(w))
    rows.append(_card_row("NOT", "A marker never carries its fact by glyph alone — this card is"))
    rows.append(_card_row("", "what the glyph on the lane stands for."))
    # the record body publishes a record frame's navigation, which build() would turn into
    # a keybar of verbs no overlay dispatches; the card owns its footer
    s.record_facts = None
    s.record_nav = None
    return build(s, rows, keybar([("Esc", "back")], w), w, h)
