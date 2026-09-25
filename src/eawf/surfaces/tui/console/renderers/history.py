"""history: what changed, from which source, at which revision.

The source note for the fact under the cursor docks to the foot of the frame.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import (
    TABLES,
    Fixed,
    Table,
    View,
    bar,
    build,
    header,
    route_keys_bar,
    thin,
)
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.renderers.spine import held, native_frame
from eawf.surfaces.tui.console.width import pad

Fact = tuple[str, str, str, str]
FACTS: tuple[Fact, ...] = pt.HISTORY_FACTS


def _note(fact: Fact) -> list[str]:
    fid = fact[0].split(" ")[0]
    if fact[2] == "imported":
        return [
            f" IMPORTED    {fid}",
            "             It arrived from a v0.6 wave as alias W-0044, and never counts as",
            "             native completion.",
        ]
    if fact[2] == "retention":
        return [
            f" RESOLVED    {fid}",
            "             Purged under a 7d retention, so there is nothing left to open.",
            "             The fact remains; only its events are gone.",
        ]
    return [
        f" SOURCE      {fid}",
        f"             Recorded live by {fact[2]} at revision {fact[1]}.",
        "             Nothing was imported and nothing has been purged.",
    ]


def filtered_facts(query: str) -> list[Fact]:
    """Return the facts whose text or source mentions ``query``, case-insensitively."""
    q = query.lower()
    return [f for f in FACTS if q in (f[0] + f[2]).lower()] if q else list(FACTS)


def render(view: View) -> list[str]:
    """Return the History frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
    s, fx, w, h = view.session, view.fixture, view.w, view.h
    query = dv.filter_of(s)
    facts = filtered_facts(query)
    dv.sel_in(s, len(facts))
    rows = [
        header(view, f" Eä ▸ {fx.scope} ▸ History"),
        " what changed, from what source, at which revision",
        bar(w),
    ]
    if s.typing or query:
        rows.append(" SEARCH    \\" + query + ("▏" if s.typing else ""))
    rows.append(Table([34, 10, 12, 0], 2).head(["FACT", "REVISION", "SOURCE", "WHEN"]))
    rows.extend(Fixed(pad(TABLES["RN"].row(list(f), i == s.sel), w)) for i, f in enumerate(facts))
    if not facts:
        rows.append("   nothing matches")
    fact = facts[s.sel] if facts else FACTS[0]
    foot = [thin(w), *_note(fact)]
    rows.extend(pad("", w) for _ in range(h - 1 - len(foot) - len(rows)))
    rows.extend(foot)
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["history"]))
