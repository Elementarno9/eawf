"""batch.detail: the batch's own tasks, found by their back-reference.

The frame therefore cannot show a different set from the milestone that summarises it.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import Table, View, bar, build, header, route_keys_bar, thin
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.renderers.spine import held, native_frame

OWN = "BAT-0001"


def _task_rows(view: View, bid: str) -> list[tuple[str, str, str]]:
    fx = view.fixture
    rows = []
    for x in dv.children_of(fx, bid, "EAWF-", "BATCH"):
        runs = len(dv.children_of(fx, x, "RUN-", "SCOPE")) or (
            1 if dv.field_of(fx, x, "RUNS") else 0
        )
        name = dv.field_of(fx, x, "NAME") or ""
        rows.append((f"{x} {name}", dv.ref_state(fx, x) or "∅", str(runs)))
    return rows


def render(view: View) -> list[str]:
    """Return the Batch frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
    s, fx, w = view.session, view.fixture, view.w
    bid = dv.subj_of(s, OWN)
    tasks = _task_rows(view, bid)
    if dv.own_body(s, OWN):
        dv.sel_in(s, len(tasks))
    milestone = (dv.field_of(fx, bid, "MILESTONE") or "").split(" ")[0]
    parent = f"{milestone} ▸ " if milestone else ""
    facts = [("NAME", "field"), ("STATE", "field"), ("CHECKS", "worst")]
    subject = dv.subj_facts(fx, bid, facts) or "∅ name and state unavailable"
    table = Table([34, 19, 0], 2)
    rows = [
        header(view, f" Eä ▸ … ▸ {parent}{bid}"),
        f" Batch {bid} {subject}",
        bar(w),
        " HEAD            Integration base a1f4c9e matches the shared branch.",
        "                 authorized at      a1f4c9e   still exact",
        thin(w),
        table.head(["TASK FRONTIER", "STATE", "RUNS"]),
    ]
    dv.publish_nav(s, [t[0].split(" ")[0] for t in tasks])
    rows.extend(table.row(list(t), i == s.sel) for i, t in enumerate(tasks))
    rows.append(thin(w))
    rows.append(" VERIFICATION CYCLE   checking → audit review → changes → repair")
    rows.append("   checking          3 of 4 checks passed          ≈6m left")
    if not dv.own_body(s, OWN):
        rows = dv.absent(s, fx, rows, entity_id=bid, what="tasks or integration detail", w=w)
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["batch.detail"]))
