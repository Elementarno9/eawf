"""history: what changed, from which source, at which revision.

The source note for the fact under the cursor docks to the foot of the frame.

History is a ledger of facts, never a list of the records they are about. The native
frame lists the tree's change feed, newest first: one row per record a commit changed,
naming the fields, the revision it left, who asked and when. Enter opens the diff of the
record under the caret. A tree whose feed holds nothing says since when nothing was
recorded, because the feed starts the day its producer shipped and holds no older change.
"""

from __future__ import annotations

from datetime import date

from eawf.kernel.projection.spine import SpineView
from eawf.kernel.store.changes import ChangeRecord
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import NO_VALUE
from eawf.surfaces.tui.console.format import clock_minute, day
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
    window_rows,
)
from eawf.surfaces.tui.console.keybar import KEY, ROUTE_KEYS
from eawf.surfaces.tui.console.live_reads import HISTORY_READ, held_changes
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.spine import held
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


#: The ledger's columns in the prototype frame.
LEDGER = Table([34, 10, 12, 0], 2)

#: The ledger's columns in the native frame, whose source is a principal key of up to 32
#: characters rather than a short source name, so it is given the room the fact gives up.
NATIVE_LEDGER = Table([32, 9, 24, 0], 2)

#: What the ledger says before the change feed has been read.
UNREAD = f"{UNKNOWN_WORD} · the change feed has not been read yet, so no fact is listed"

#: What the source pane says while the ledger lists no fact.
NO_SOURCE = f"{UNKNOWN_WORD} · no fact is listed, so none has a source to name"


def none_since(since: date) -> str:
    """Return what a feed holding no change says: since the day it began."""
    return f"no changes recorded since {since.isoformat()}"


def _change_cells(record: ChangeRecord) -> list[str]:
    """Return one ledger row: the record and its fields, the revision, the actor, the time."""
    fields = ", ".join(change.field for change in record.changes)
    revision = f"rev {record.revision_after}" if record.revision_after is not None else NO_VALUE
    return [
        f"{record.record_key} {fields}",
        revision,
        record.actor_ref or UNKNOWN_WORD,
        f"{day(record.recorded_at)} {clock_minute(record.recorded_at)}",
    ]


def _source(record: ChangeRecord) -> list[str]:
    """Return the source pane of the change under the caret."""
    count = len(record.changes)
    return [
        f" {'SOURCE':<12}{record.event_name} · by {record.actor_ref or UNKNOWN_WORD} "
        f"· sequence {record.canonical_sequence}",
        f" {'':<12}{count} field{'s' if count != 1 else ''} changed · Enter shows them "
        "before and after",
    ]


def ledger_frame(view: View, spine: SpineView) -> list[str]:
    """Return the History ledger drawn from the read model the daemon served.

    Args:
        view: The render being built.
        spine: The route's read model, whose scope and cursor head the frame.

    Returns:
        The full frame, keybar last: the ledger's head, one row per change in the feed
        or the one line saying why none is listed, and the source pane docked at the foot.
    """
    session, w, h = view.session, view.w, view.h
    page = held_changes(view.live, HISTORY_READ)
    records = page.changes if page is not None else ()
    cursor = dv.sel_in(session, len(records))
    session.sel_id = records[cursor].record_key if records else None
    rows = [
        *native_head(
            view,
            spine,
            crumb_text=route_crumb(view, spine, "History"),
            summary="what changed, from what source, at which revision",
        ),
        NATIVE_LEDGER.head(["FACT", "REVISION", "SOURCE", "WHEN"]),
    ]
    if not records:
        empty = UNREAD if page is None else none_since(page.since)
        rows.append(f"   {empty}")
        foot = [thin(w), f" {'SOURCE':<12}{NO_SOURCE}"]
        rows.extend(Fixed(pad("", w)) for _ in range(h - 1 - len(foot) - len(rows)))
        # a ledger listing no fact has no row to walk, open, filter or copy
        return build(view, [*rows, *foot], route_keys_bar(view, [KEY["esc"]]))
    foot = [thin(w), *_source(records[cursor])]
    win = window_rows(view, total=len(records), cursor=cursor, chrome=len(rows) + len(foot))
    rows.extend(
        Fixed(pad(NATIVE_LEDGER.row(_change_cells(records[i]), i == cursor), w))
        for i in range(win.start, win.stop)
    )
    rows.extend(Fixed(pad("", w)) for _ in range(h - 1 - len(foot) - len(rows)))
    keys = [KEY["up"], KEY["enter"], KEY["esc"]]
    return build(view, [*rows, *foot], route_keys_bar(view, keys))


def render(view: View) -> list[str]:
    """Return the History frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return ledger_frame(view, spine)
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
    rows.append(LEDGER.head(["FACT", "REVISION", "SOURCE", "WHEN"]))
    rows.extend(Fixed(pad(TABLES["RN"].row(list(f), i == s.sel), w)) for i, f in enumerate(facts))
    if not facts:
        rows.append("   nothing matches")
    fact = facts[s.sel] if facts else FACTS[0]
    foot = [thin(w), *_note(fact)]
    rows.extend(pad("", w) for _ in range(h - 1 - len(foot) - len(rows)))
    rows.extend(foot)
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["history"]))
