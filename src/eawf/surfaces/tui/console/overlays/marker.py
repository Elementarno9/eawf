"""The marker card: one roadmap marker, captured when the card opened.

The card shows the marker's record when the register holds one and says in place that
nothing is held when it does not.
"""

from __future__ import annotations

import re

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.renderers.timeline import FORECAST, lane_name, marker_glyph
from eawf.surfaces.tui.console.width import pad

_TRACK_ROW = re.compile(r"^\s{2}TRACK\s")
_RULE_ROW = re.compile(r"^[\s─│┼]*$")
_RULE_RUN = re.compile("─{4,}")
_LABEL = re.compile(r"^[A-Z][A-Z0-9 ]*\s*$")
_LEAD = re.compile(r"^[\s▸]+")
DEFAULT_MARKER = "MLS-0001"


def _card_row(label: str, value: str, caret: bool = False) -> str:
    return " " + pad(label, 9) + ("▸ " if caret else "  ") + value


def _recast(row: str) -> str:
    """Return a record-body row re-laid on the card's nine-column label gutter."""
    if _RULE_ROW.match(row) or _RULE_RUN.search(row):
        return row
    head = row[2:14].replace("▸", " ")
    label = head.strip() if _LABEL.match(head) else ""
    tail = row[12:]
    at = tail.find("▸")
    return _card_row(label, _LEAD.sub("", tail), 0 <= at < 4)


def render(view: View) -> list[str]:
    """Return the marker card."""
    s, fx, w = view.session, view.fixture, view.w
    card = s.marker_card
    if not card:
        lane = lane_name(s.sel)
        card = {"id": s.timeline_marker or DEFAULT_MARKER, "lane": lane}
        card["glyph"] = marker_glyph(lane, s.mark)
    mid, lane, glyph = str(card["id"]), str(card["lane"]), str(card["glyph"])
    record = fx.record(mid)
    name = (dv.field_of(fx, mid, "NAME") or "") if record else ""
    forecast = glyph == FORECAST
    rows = [
        header(view, f" Eä ▸ marker · {mid}"),
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
    if record:
        body = dv.record_body(s, fx, record, entity_id=mid, subject=rows[1])
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
    # the record body publishes a record frame's navigation, which the frame builder would
    # turn into a keybar of verbs no overlay dispatches; the card owns its keybar
    s.record_facts = None
    s.record_nav = None
    return build(view, rows, keybar([("Esc", "back")], w))
