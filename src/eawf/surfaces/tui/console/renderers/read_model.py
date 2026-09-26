"""The native frame the verification and operations routes draw from a read model.

The crumb, the counts row and the unstated section are the parts every native frame
shares whatever it draws below them, so :func:`crumb`, :func:`counts` and
:func:`unstated_rows` are public: the integration and transcript routes draw their own
bodies and still print the same three things about the projection they were served.
The first two read nothing but the projection header, which is what :class:`Projected`
names, so the spine and register frames draw them off their own read models too.

The seven routes share this frame because they share what it draws: rows the daemon
projected at one committed cursor, and nothing else. Only what the read model states is
rendered. A count comes from
:meth:`~eawf.kernel.projection.route_view.RouteReadModel.count`, so a register the route
binds shows its number and a register it does not bind shows the unavailable token rather
than a zero. A declared column no producer states shows the unknown truth token beside its
name, and where the read model knows which item is missing, the frame names that item --
"waiting on the sandbox-decision record" is an answer an operator can act on where a blank
cell is not.

Health adds one section. Doctor observes nothing about a runtime tuple itself, so each
verdict states the stage it was reached at, the daemon verb that wrote the stage record
and the artifact that stage filed its evidence under, and a quarantined tuple names every
trigger its recorded failure code can have come from. A tuple section with no verdict in
it says so; it never draws a healthy tuple nobody observed.

The selection is restored by stable id, never by offset, so a keyed patch that inserts a
row above the cursor moves the cursor with its row.

A route whose projection is not held renders its epoch-1 frame instead: the console still
opens against the prototype registers, and the tracked golden contract is that mode.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.truth import TruthField, TruthState
from eawf.kernel.projection.verification import HealthReadModel, RuntimeTupleRow
from eawf.surfaces.tui.console.derive import plural
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    needs_count,
    route_keys_bar,
    thin,
    window_rows,
)
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.reads import attached, reads
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import BRAND, CRUMB_SEP, truth_cell
from eawf.surfaces.tui.console.width import pad

#: What a count with no register in this read model renders as. A register that was read
#: and held nothing renders ``0``; this token says no register was read at all.
UNAVAILABLE = "∅ unavailable"

#: The label a count carries while the projection cannot claim it holds every row.
KNOWN = "known"

#: What the tuple section says when no conformance verdict is held. An empty section is
#: an absence of evidence, never a healthy tuple.
NO_VERDICT = "∅ no conformance verdict is held · nothing here claims a tuple is in service"

_ROWS = Table([34, 12, 0], 2)
_TUPLES = Table([28, 8, 11, 24, 0], 2)
_EMPTY = "   this scope holds no record the read model renders"

# The console's token vocabulary carries three absences, and the truth states carry five.
# A purged or invalidated value is one the store no longer holds, which reads as
# unavailable rather than as a value the projection simply never learned.
_TOKEN_OF_STATE: dict[TruthState, str] = {
    TruthState.UNKNOWN: "unknown",
    TruthState.UNAVAILABLE: "unavailable",
    TruthState.DENIED: "denied",
    TruthState.PURGED: "unavailable",
    TruthState.INVALIDATED: "unavailable",
}


class Projected(Protocol):
    """The projection header the crumb and the counts row are drawn off.

    The spine, the register and the route read models are three row shapes over one
    header, and these two rows read the header alone. Naming it here is what lets the
    three families share one crumb builder and one counts builder instead of each
    keeping its own copy.
    """

    @property
    def scope_id(self) -> str:
        """The scope the projection was built for."""

    @property
    def source_cursor(self) -> str:
        """The committed sequence the rows were read through."""

    @property
    def complete(self) -> bool:
        """Whether the projection claimed every row of its scope."""

    @property
    def counts(self) -> Mapping[str, int]:
        """The rows per collection the route binds, keyed by collection name."""


def crumb(view: View, model: Projected) -> str:
    """Return the frame's breadcrumb, ending at the route's own leaf.

    Args:
        view: The render being built; its session names the route and its subject.
        model: The read model the frame draws, whose scope opens the crumb.

    Returns:
        The crumb, with its leading gutter.
    """
    session = view.session
    leaf = REGISTRY.step_leaf(session.route, session.subj_id)
    steps = [BRAND, model.scope_id, *([leaf] if leaf else [])]
    return " " + CRUMB_SEP.join(steps)


def counts(model: Projected) -> str:
    """Return the derived count of every register the route binds, in binding order.

    Args:
        model: The read model the counts are taken off.

    Returns:
        The counts and the cursor, as the row under the header prints them.
    """
    parts = [plural(count, name) for name, count in model.counts.items()]
    if not parts:
        parts = [UNAVAILABLE]
    elif not model.complete:
        parts.append(KNOWN)
    return " · ".join([*parts, f"cursor {group(int(model.source_cursor))}"])


def unstated_rows(model: RouteReadModel) -> list[str]:
    """Return the declared columns no producer states, each beside the unknown token.

    A column whose missing producer is named gets its own line, because the item is what
    an operator would look up; the rest share one line with the token.

    Args:
        model: The read model whose column table is read.

    Returns:
        One or more rows, never an empty list: a route with nothing silent says so.
    """
    token = truth_cell("unknown")
    plain = [spec.name for spec in model.unproduced() if spec.missing_producer is None]
    rows = [" UNSTATED  " + " · ".join(f"{name} {token}" for name in plain)] if plain else []
    named: dict[str, list[str]] = {}
    for spec in model.unproduced():
        if spec.missing_producer is not None:
            named.setdefault(spec.missing_producer, []).append(spec.name)
    for item, names in named.items():
        cells = " · ".join(f"{name} {token}" for name in names)
        rows.append(f" WAITING   {cells} · {item}")
    return rows or [" UNSTATED  every declared column is stated"]


def cell(field: TruthField[str]) -> str:
    """Return one truth field as a frame cell: its value, or the token naming its absence."""
    if field.state is TruthState.KNOWN and field.value:
        return field.value
    return truth_cell(_TOKEN_OF_STATE[field.state])


def restore(session: Session, model: RouteReadModel) -> int:
    """Return the row the cursor sits on, restored by stable id, and publish that id.

    Args:
        session: The session whose ``sel_id`` names the row the cursor was on and whose
            ``sel`` is the offset the frame draws the caret at.
        model: The read model the frame draws.

    Returns:
        The row offset the caret goes on; ``0`` for an empty read model, which draws no
        caret at all.
    """
    found = model.index_of(session.sel_id)
    index = found if found is not None else min(max(session.sel, 0), max(len(model.rows) - 1, 0))
    session.sel = index
    session.sel_id = model.rows[index].key if model.rows else None
    return index


def record_rows(
    view: View, model: RouteReadModel, cursor: int, *, above: int, below: int
) -> list[str]:
    """Return the record table: its head, the rows around the cursor, and its window.

    The table is the part every native frame shares below its own sections, so the routes
    that draw a bundle, a candidate's readiness or a card above it print the same rows in
    the same columns as the routes that draw nothing else. The rows are windowed into
    whatever height the sections around the table leave.

    Args:
        view: The render being built; its width is what each line is padded to.
        model: The read model whose rows are drawn.
        cursor: The row offset the caret sits on, as :func:`restore` published it.
        above: The frame rows drawn above the table.
        below: The frame rows drawn below the table.

    Returns:
        The head row, the windowed rows, then the ``WINDOW`` row; a read model with no
        rows says so on one line.
    """
    rows = [_ROWS.head(["ROW", "KIND", "STATUS"])]
    win = window_rows(view, total=len(model.rows), cursor=cursor, chrome=above + 2 + below)
    if not model.rows:
        rows.append(_EMPTY)
    for index in range(win.start, win.stop):
        row = model.rows[index]
        # the kind is the collection the read model states, never guessed from the id
        cells = [row.key, row.collection.value, cell(row.field("status"))]
        line = _ROWS.row(cells, index == cursor)
        rows.append(line if index == cursor else Fixed(pad(line, view.w)))
    rows.append(win.line(complete=model.complete))
    return rows


def _tuple_rows(tuples: tuple[RuntimeTupleRow, ...], w: int) -> list[str]:
    """Return the runtime-tuple section: one line per verdict, or the honest absence."""
    quarantined = sum(1 for row in tuples if row.quarantined)
    head = f" TUPLES    {plural(len(tuples), 'runtime tuple')}"
    rows = [f"{head} · {quarantined} quarantined" if tuples else f" TUPLES    {NO_VERDICT}"]
    if not tuples:
        return rows
    rows.append(_TUPLES.head(["CHECK", "RESULT", "STAGE", "ANSWERED BY", "TRIGGER"]))
    rows.extend(
        Fixed(
            pad(
                _TUPLES.row(
                    [
                        row.check,
                        row.status,
                        cell(row.stage),
                        cell(row.producer),
                        cell(row.trigger),
                    ]
                ),
                w,
            )
        )
        for row in tuples
    )
    return rows


def native_frame(view: View, model: RouteReadModel) -> list[str]:
    """Return one route's frame, drawn from the read model the daemon served.

    Args:
        view: The render being built; its session carries the cursor and the selection.
        model: The route's read model at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    session, w = view.session, view.w
    cursor = restore(session, model)
    regions = REGISTRY.focus_regions.get(session.route, ())
    rows: list[str] = [
        header_row(
            session, crumb=crumb(view, model), scope=model.scope_id, needs=needs_count(view), w=w
        ),
        " " + counts(model),
        bar(w),
    ]
    rd = reads(session)
    if not rd.complete:
        rows.extend(
            [f" ATTACHED  {attached(rd, revision=group(int(model.source_cursor)))}", thin(w)]
        )
    if regions:
        rows.append(" REGIONS   " + " · ".join(regions))
        rows.append(thin(w))
    below = [thin(w), *_tuple_rows(model.tuples, w)] if isinstance(model, HealthReadModel) else []
    below += [thin(w), *unstated_rows(model)]
    rows.extend(record_rows(view, model, cursor, above=len(rows), below=len(below)))
    rows.extend(below)
    return build(view, rows, route_keys_bar(view, native_keys(session.route)))


def native(view: View) -> RouteReadModel | None:
    """Return the read model the session's route draws from, when the console holds one.

    A projection built for another family is not this frame's rows, so it is not adopted:
    the route falls back to its epoch-1 frame rather than drawing another route's records.
    """
    model = view.projection
    if isinstance(model, RouteReadModel) and model.route == view.session.route:
        return model
    return None
