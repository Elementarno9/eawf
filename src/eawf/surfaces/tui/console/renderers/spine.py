"""The native spine frame: what a spine route draws when a read model is held.

The five projection-backed spine routes share this frame because they share the thing it
draws: rows the daemon projected at one committed cursor, and nothing else. Only what the
read model states is rendered. A count comes from
:meth:`~eawf.kernel.projection.spine.SpineView.count`, so a register the route binds shows
its number and a register it does not bind shows the unavailable token rather than a zero.
A declared column no epoch-2 producer states yet shows the unknown truth token beside its
name, so the frame says which cells are silent instead of leaving them blank.

The selection is restored by stable id, never by offset. A keyed patch may insert a row
above the cursor, and an offset kept across that lands on a neighbour; the session's
``sel_id`` is looked up in the rebuilt rows on every render, so the cursor moves with its
row and a row that has gone leaves the selection at the top rather than on whatever
slid into its place.

A route whose projection is not held renders its epoch-1 frame instead: the console still
opens against the prototype registers, and the tracked golden contract is that mode.
"""

from __future__ import annotations

from eawf.kernel.projection.spine import SpineView
from eawf.kernel.projection.truth import TruthState
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import Fixed, Table, View, bar, build, route_keys_bar, thin
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import BRAND, CRUMB_SEP, truth_cell
from eawf.surfaces.tui.console.width import pad

#: What a count with no register in this read model renders as. A register that was read
#: and held nothing renders ``0``; this token says no register was read at all.
UNAVAILABLE = "∅ unavailable"

#: The label a count carries while the projection cannot claim it holds every row.
KNOWN = "known"

_ROWS = Table([34, 12, 0], 2)
_EMPTY = "   this scope holds no record the read model renders"


def _crumb(view: View, spine: SpineView) -> str:
    """Return the frame's breadcrumb, ending at the route's own leaf."""
    session = view.session
    leaf = REGISTRY.step_leaf(session.route, session.subj_id)
    steps = [BRAND, spine.scope_id, *([leaf] if leaf else [])]
    return " " + CRUMB_SEP.join(steps)


def _counts(spine: SpineView) -> str:
    """Return the derived count of every register the route binds, in binding order."""
    parts = [
        f"{group(count)} {name}" + ("" if count == 1 else "s")
        for name, count in spine.counts.items()
    ]
    if not parts:
        parts = [UNAVAILABLE]
    elif not spine.complete:
        parts.append(KNOWN)
    return " · ".join([*parts, f"cursor {group(int(spine.source_cursor))}"])


def _unstated(spine: SpineView) -> str:
    """Return the declared columns no producer states, each beside the unknown token."""
    token = truth_cell("unknown")
    return " UNSTATED  " + " · ".join(f"{name} {token}" for name in spine.unproduced())


def held(view: View) -> SpineView | None:
    """Return the spine read model the route draws from, when the console holds one.

    The view carries whichever family's read model the seam answered with, so a spine
    renderer asks for its own and falls back to the epoch-1 frame for anything else.
    """
    model = view.projection
    return model if isinstance(model, SpineView) else None


def restore(session: Session, spine: SpineView) -> int:
    """Return the row the cursor sits on, restored by stable id, and publish that id.

    Args:
        session: The session whose ``sel_id`` names the row the cursor was on and whose
            ``sel`` is the offset the frame draws the caret at.
        spine: The read model the frame draws.

    Returns:
        The row offset the caret goes on; ``0`` for an empty read model, which draws no
        caret at all.
    """
    found = spine.index_of(session.sel_id)
    index = found if found is not None else min(max(session.sel, 0), max(len(spine.rows) - 1, 0))
    session.sel = index
    session.sel_id = spine.rows[index].key if spine.rows else None
    return index


def native_frame(view: View, spine: SpineView) -> list[str]:
    """Return one spine route's frame, drawn from the read model the daemon served.

    Args:
        view: The render being built; its session carries the cursor and the selection.
        spine: The route's read model at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    session, w = view.session, view.w
    cursor = restore(session, spine)
    regions = REGISTRY.focus_regions.get(session.route, ())
    rows: list[str] = [
        header_row(session, crumb=_crumb(view, spine), scope=spine.scope_id, needs=0, w=w),
        " " + _counts(spine),
        bar(w),
    ]
    if regions:
        rows.append(" REGIONS   " + " · ".join(regions))
        rows.append(thin(w))
    rows.append(_ROWS.head(["ROW", "KIND", "STATUS"]))
    if not spine.rows:
        rows.append(_EMPTY)
    for index, row in enumerate(spine.rows):
        status = row.field("status")
        stated = status.value if status.state is TruthState.KNOWN and status.value else None
        # the kind is the collection the read model states, never guessed from the id
        cells = [row.key, row.collection.value, stated or truth_cell("unknown")]
        line = _ROWS.row(cells, index == cursor)
        rows.append(line if index == cursor else Fixed(pad(line, w)))
    rows.append(thin(w))
    rows.append(_unstated(spine))
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS[session.route]))
