"""run.detail: the Run's event timeline with its usage, controls and lineage.

A Run other than the fixture's own renders its record from the register or states the
absence.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import Table, View, bar, build, header, route_keys_bar, thin
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.renderers.spine import native_frame
from eawf.surfaces.tui.console.width import cell_len

OWN = "RUN-9e3779b1"
# Cells the timeline's first two columns and their gutter take.
TL_PREFIX = 38


def tl_label(w: int) -> str:
    """Return the priority column's head, abbreviated only below 120 columns."""
    return "PRIORITY" if w >= 120 else "PRI"


def tl_text(w: int) -> int:
    """Return the cells the event detail column gets at width ``w``."""
    return w - TL_PREFIX - 1 - cell_len(tl_label(w))


def tl_header(w: int) -> str:
    """Return the timeline head row.

    Raises:
        ValueError: the head does not fit ``w`` cells.
    """
    line = Table([10, 23, tl_text(w) + 1, 0], 2).head(["TIMELINE", "EVENT", "DETAIL", tl_label(w)])
    if cell_len(line) > w:
        raise ValueError(f"timeline header overruns the frame: {cell_len(line)}/{w}")
    return line


def _facts(view: View, rid: str) -> str:
    s, fx = view.session, view.fixture
    row = dv.fleet_of(fx, rid)
    if dv.own_body(s, OWN):
        return "claude · RUNNING · started 10:00:00"
    if row:
        return f"{row.prov} · {row.state}" + (f" · {row.reason}" if row.reason else "")
    facts = [("PROVIDER", "token"), ("STATE", "field")]
    return dv.subj_facts(fx, rid, facts) or "∅ provider and state unavailable"


def render(view: View) -> list[str]:
    """Return the Run frame, native when a read model is held.

    Raises:
        ValueError: an event's detail is wider than its column.
    """
    if view.projection is not None:
        return native_frame(view, view.projection)
    s, fx, w = view.session, view.fixture, view.w
    proto = fx.proto
    dv.sel_in(s, len(proto.timeline))
    rid = dv.subj_of(s, OWN)
    parent = "EAWF-0001 ▸ " if rid == OWN else ""
    rows = [
        header(view, f" Eä ▸ … ▸ {parent}{rid}"),
        f" Run {rid} · {_facts(view, rid)} · seq {dv.num(proto.revision)}",
        bar(w),
        tl_header(w),
    ]
    text_w = tl_text(w)
    table = Table([10, 23, text_w + 1, 0], 2)
    for i, event in enumerate(proto.timeline):
        if cell_len(event[2]) > text_w:
            raise ValueError(f"event text exceeds its column: {event[2]}")
        rows.append(table.row(list(event), i == s.sel))
    rows.extend(
        [
            thin(w),
            " USAGE     elapsed 18m 04s of 60m · ≈41m left · cost ~4.62 of 20.00",
            "           tokens out 128,410 · peak rss ∅ probe uncertified",
            thin(w),
            " CONTROLS  no control outstanding · last confirmed 10:01:12",
            " LINEAGE   attempt 1 of 1 · no retry · no fork",
        ]
    )
    if not dv.own_body(s, OWN):
        rows = dv.absent(s, fx, rows, entity_id=rid, what="timeline or usage", w=w)
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["run.detail"]))
