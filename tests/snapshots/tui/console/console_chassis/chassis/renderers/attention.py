"""attention: the eight exception buckets, severity-first, as a strip at 80 and a rail at
120/160; the counts derive from the attention register and stay fleet-wide whatever the
bucket filter shows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.attention import attn_list, open_count, verbs_for, xcount, xlabel, xtop
from ...chassis.frame import TBL, Fixed, bar, bar_, build, header_row, thin
from ...chassis.keys import KEY
from ...chassis.seam import attn_cell, can_mutate, reads
from ...chassis.width import cell_len, pad

if TYPE_CHECKING:
    from ...chassis.fixture import Action, Fixture
    from ...chassis.session import Session

RAIL_W = 29
_KIND_INDENT = "              "
_LEDGER_LEAD = "              ledger  "
_LEDGER_CONT = "                      "


def _group_of(a: Action) -> str:
    return xtop(a.bucket) if a.state == "OPEN" else "RESOLVED"


def _ledger_rows(a: Action, bw: int) -> list[str]:
    """The ledger wraps rather than truncating: a cut confirmed stage is the one thing a
    control ledger may never do."""
    out: list[str] = []
    line_w = bw - cell_len(_LEDGER_LEAD)
    cur = ""
    first = True
    for stage, when in a.ledger:
        s = f"{stage} {when}"
        piece = (cur + " → " if cur else "") + s
        if cell_len(piece) > line_w:
            out.append((_LEDGER_LEAD if first else _LEDGER_CONT) + cur + " →")
            first = False
            cur = s
        else:
            cur = piece
    if cur:
        out.append((_LEDGER_LEAD if first else _LEDGER_CONT) + cur)
    return out


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    P = fixture.proto
    n = open_count(fixture)
    rd = reads(s)
    shown = attn_list(s, fixture)
    head: list[str] = [
        header_row(s, fixture, f" Eä ▸ {P.scope} ▸ Needs you", w),
        f" {attn_cell(s, n)} mine · {attn_cell(s, n)}"
        + (" fleet-wide" if rd.complete else " known")
        + " · nothing here opened itself",
        bar(w),
    ]
    if not rd.complete:
        head.append(f" ATTACHED  {rd.age}")
        head.append(thin(w))
    wide = w >= 120
    col = w - RAIL_W - 1
    bw = col if wide else w
    items: list[dict[str, object]] = [
        {"key": None, "label": "all", "n": sum(1 for a in P.attention if a.state == "OPEN")}
    ]
    for b in P.xbuckets:
        items.append({"key": b.key, "label": b.label, "n": xcount(fixture, b.key)})
        for sub in b.sub or ():
            items.append({"key": sub.key, "label": "↳ " + sub.label, "n": xcount(fixture, sub.key)})
    if not wide:
        head.append(dv.strip_row(s, items, w))
    dv.sel_by_id(s, [a.id for a in shown])
    body: list[str] = []
    last: str | None = None
    T = TBL([9, max(30, bw - 32), 10, 0], 2)
    for i, a in enumerate(shown):
        group = _group_of(a)
        if group != last:
            # the count belongs to the bucket it counts: a fleet total restates the header
            bn = sum(1 for x in shown if _group_of(x) == group)
            body.append(
                " "
                + ("RESOLVED" if group == "RESOLVED" else xlabel(fixture, group).upper())
                + f"  {bn}"
            )
            last = group
        body.append(T.row([a.id, a.text, a.state, a.due], i == s.sel))
        if i == s.sel:
            body.append(_KIND_INDENT + a.kind)
            if a.ledger:
                body.extend(_ledger_rows(a, bw))
    if not shown:
        body.append("   nothing in this bucket needs you")
    if wide:
        # the rail keeps every bucket its register carries, the sub-buckets indented under
        # needs operator; a bucket the filter excludes is dimmed rather than dropped, and a
        # chosen parent keeps its own sub-buckets bright
        rail = ["BUCKETS"] + [
            ("▸" if x["key"] == s.bucket else " ") + pad(str(x["label"]), 24) + str(x["n"])
            for x in items[1:]
        ]
        out2: list[str] = []
        for ri in range(max(len(body), len(rail))):
            left = pad(body[ri] if ri < len(body) else "", col)
            rr = pad(rail[ri] if ri < len(rail) else "", RAIL_W - 2)
            it = items[ri] if 0 < ri < len(items) else None
            off = bool(
                it
                and s.bucket
                and not (
                    it["key"] == s.bucket
                    or (s.bucket == "needs" and xtop(str(it["key"])) == "needs")
                )
            )
            raw = pad(left + "│ " + rr, w)
            out2.append(Fixed(raw) if off else raw)
        body = out2
    rows = head + body
    if s.bucket:
        # the readout docks to the foot of its frame, always in the same place rather than
        # floating with the length of the list above it
        while len(rows) < h - 3:
            rows.append("")
        rows.append(thin(w))
        open_n = sum(1 for a in shown if a.state == "OPEN")
        rows.append(
            f" WINDOW    ▸{xlabel(fixture, s.bucket).upper()}  {open_n} of {items[0]['n']} matching"
        )
    sel_row = shown[s.sel] if s.sel < len(shown) else None
    live = can_mutate(s) and sel_row is not None and sel_row.state == "OPEN"
    vk = {"a": KEY["answer"], "x": KEY["deny"], "z": KEY["snooze"], "v": KEY["resolve"]}
    esc_e = ("Esc", "clear bucket") if s.bucket else KEY["esc"]
    if live:
        ks = [KEY["up"], KEY["tab"], *[vk[v] for v in verbs_for(sel_row)], KEY["actions"], esc_e]
    else:
        ks = [KEY["up"], KEY["tab"], KEY["actions"], esc_e]
    return build(s, rows, bar_(s, ks, w), w, h)
