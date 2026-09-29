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

import re
import textwrap
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Protocol

from eawf.kernel.projection.registers import RegisterView
from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.spine import SpineView
from eawf.kernel.projection.truth import TruthField
from eawf.kernel.projection.verification import HealthReadModel, RuntimeTupleRow
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.derive import plural
from eawf.surfaces.tui.console.format import group, span
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    acting_pairs,
    bar,
    boxed,
    build,
    g_row,
    needs_count,
    route_keys_bar,
    scope_label,
    thin,
    window_rows,
)
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import Pair, keybar
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.reads import attached, reads
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import BRAND, CRUMB_SEP, truth_cell
from eawf.surfaces.tui.console.width import cell_len, pad

#: What a count with no register in this read model renders as. A register that was read
#: and held nothing renders ``0``; this token says no register was read at all.
UNAVAILABLE = "∅ unavailable"

#: The label a count carries while the projection cannot claim it holds every row.
KNOWN = "known"

#: What a declared column no producer states shows beside its name: the unknown token
#: with its state word, so the column reads as silent without a legend.
UNKNOWN_WORD = f"{truth_cell('unknown')} unknown"

#: What ``y`` copies from a card whose register holds no row: there is no address to give.
NOTHING_TO_COPY = "nothing is held here to copy"

#: What the tuple section says when no conformance verdict is held. An empty section is
#: an absence of evidence, never a healthy tuple.
NO_VERDICT = "∅ no conformance verdict is held · nothing here claims a tuple is in service"

#: The gutter a labelled section row sets its label in, the leading space excluded: the
#: text of every labelled row starts at one column whatever the label.
LABEL_W = 13

_ROWS = Table([34, 12, 0], 2)
_TUPLES = Table([28, 8, 11, 24, 0], 2)
_EMPTY = "   this scope holds no record the read model renders"


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
    leaf = REGISTRY.read_leaf(session.route, session.subj_id)
    steps = [BRAND, scope_label(view, model.scope_id), *([leaf] if leaf else [])]
    return " " + CRUMB_SEP.join(steps)


#: The operator's word, singular and plural, for a register whose collection name is a
#: storage term rather than a noun.
_REGISTER_NOUNS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "health_view": ("check", "checks"),
        "sandbox_policy": ("sandbox policy", "sandbox policies"),
        "pending_action": ("pending action", "pending actions"),
    }
)


def noun(n: int, word: str) -> str:
    """Return ``n word`` in the plural a register name takes: ``16 batches``, ``13 runs``."""
    if word in _REGISTER_NOUNS:
        one, many = _REGISTER_NOUNS[word]
        return f"{group(n)} {one if n == 1 else many}"
    return plural(n, word, "es" if word.endswith(("ch", "sh", "s", "x")) else "s")


def cursor_note(model: Projected) -> str:
    """Return ``· cursor N`` for a read that cannot claim every row, else nothing.

    The cursor a read stopped at is provenance: a complete read is stated by its counts,
    and only an incomplete one needs to say how far it got.
    """
    return "" if model.complete else f" · cursor {group(int(model.source_cursor))}"


def counts(model: Projected) -> str:
    """Return the derived count of every register the route binds, in binding order.

    Args:
        model: The read model the counts are taken off.

    Returns:
        The counts as the row under the header prints them. Only a read that cannot claim
        every row adds the cursor it stopped at: a complete count needs no provenance.
    """
    parts = [noun(count, name) for name, count in model.counts.items()]
    if not parts:
        return UNAVAILABLE
    if model.complete:
        return " · ".join(parts)
    return " · ".join([*parts, KNOWN]) + cursor_note(model)


def unstated_rows(model: RouteReadModel) -> list[str]:
    """Return the declared columns no producer states, each beside the unknown token.

    A column whose missing producer is named gets its own line, because the item is what
    an operator would look up; the rest share one line with the token.

    Args:
        model: The read model whose column table is read.

    Returns:
        One or more rows, never an empty list: a route with nothing silent says so.
    """
    token = UNKNOWN_WORD
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
    """Return one truth field as a frame cell: its slot, then its reason when it wears a mark.

    Each of the five absences keeps its own token, so a purged or invalidated value never
    reads as an unavailable one.
    """
    return value_cell(field).full


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


def label(name: str, text: str = "") -> str:
    """Return a labelled row: ``name`` in the gutter the packet frames share, then ``text``."""
    return f" {name:<{LABEL_W}}{text}"


def more(text: str) -> str:
    """Return a continuation row: ``text`` under the text of the labelled row above it."""
    return " " * (LABEL_W + 1) + text


def wrapped(name: str, text: str, w: int) -> list[str]:
    """Return a labelled row whose ``text`` wraps under itself rather than being clipped.

    Args:
        name: The label in the gutter.
        text: The value, wrapped at word boundaries to the room right of the gutter.
        w: The frame width in cells.
    """
    lines = textwrap.wrap(text, max(8, w - LABEL_W - 2)) or [""]
    return [label(name, lines[0]), *(more(line) for line in lines[1:])]


def route_crumb(view: View, model: Projected, *steps: str) -> str:
    """Return a crumb from the projection's scope through ``steps``, with its leading gutter."""
    return " " + CRUMB_SEP.join([BRAND, scope_label(view, model.scope_id), *steps])


def read_age(view: View, model: Projected) -> str:
    """Return how old the rows are, measured from their read to the frame's instant.

    Empty when either instant is unknown, which states the revision alone rather than
    a guess at its age.
    """
    read_at = model.generated_at if isinstance(model, (SpineView, RegisterView)) else None
    if read_at is None or view.now is None:
        return ""
    return span(max(0, int((view.now - read_at).total_seconds())))


def native_head(
    view: View, model: Projected, *, crumb_text: str, summary: str, terminal: bool = False
) -> list[str]:
    """Return a native frame's head: header, summary line, heavy rule, and the reads line.

    Every packet frame opens the same way -- its crumb, one line saying what the frame is
    about, the heavy rule -- and a connection that cannot vouch for the rows adds the
    ``ATTACHED`` line under the rule, with the rows' age where it is known, so a frame
    drawn from an old read says so before any row does and every count on it reads as
    ``known``.

    Args:
        view: The render being built.
        model: The read model the frame draws; its scope and cursor head the frame.
        crumb_text: The crumb, with its leading gutter.
        summary: The line under the header, without its leading gutter.
        terminal: Whether the frame's subject is an entity whose lifecycle has ended. No
            reads line is drawn, because a finished record does not age with the link;
            the header still states the link, which is a fact about the console, not
            about the record.

    Returns:
        The head rows, the rule under them last.
    """
    session, w = view.session, view.w
    rd = reads(session)
    known = "" if rd.complete or re.search(rf"\b{KNOWN}\b", summary) else f" · {KNOWN}"
    line = f" {summary}{known}" + ("" if rd.complete else f" · {rd.label}")
    rows: list[str] = [
        header_row(
            session,
            crumb=crumb_text,
            scope=scope_label(view, model.scope_id),
            needs=needs_count(view),
            w=w,
        ),
        Fixed(pad(line, w)),
        bar(w),
    ]
    if not terminal and not rd.complete:
        revision = group(int(model.source_cursor))
        text = attached(rd, revision=revision, age=read_age(view, model))
        rows.extend([f" ATTACHED  {text}", thin(w)])
    return rows


def finish(
    view: View,
    top: Sequence[str],
    body: Sequence[str],
    keys: Sequence[Pair],
    *,
    foot: Sequence[str] = (),
) -> list[str]:
    """Return the frame: ``top``, ``body``, ``foot`` docked above the keybar, the keybar last.

    A readout that follows the cursor is docked at the foot rather than floated under a
    list, so it stays where the eye expects it however long the list grows.

    Args:
        view: The render being built.
        top: The rows :func:`native_head` returned.
        body: The frame's sections; a row not yet :class:`Fixed` is laid out here.
        keys: The keybar pairs, in the order the packet lists them.
        foot: The docked readout, drawn last above the keybar; empty docks nothing.
    """
    w = view.w
    laid = [row if isinstance(row, Fixed) else g_row(row, w) for row in body]
    gap = view.h - 1 - len(top) - len(laid) - len(foot)
    filler = [Fixed(" " * w)] * max(0, gap) if foot else []
    tail = [row if isinstance(row, Fixed) else g_row(row, w) for row in foot]
    return build(
        view, [*top, *laid, *filler, *tail], keybar(acting_pairs(view, keys), w, keep_actions=True)
    )


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


def tuple_rows(tuples: tuple[RuntimeTupleRow, ...], w: int) -> list[str]:
    """Return the runtime-tuple section: one line per verdict, or the honest absence."""
    quarantined = sum(1 for row in tuples if row.quarantined)
    counted = f"{plural(len(tuples), 'runtime tuple')} · {quarantined} quarantined"
    rows = [label("TUPLES", counted if tuples else NO_VERDICT)]
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
            session,
            crumb=crumb(view, model),
            scope=scope_label(view, model.scope_id),
            needs=needs_count(view),
            w=w,
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
    below = [thin(w), *tuple_rows(model.tuples, w)] if isinstance(model, HealthReadModel) else []
    below += [thin(w), *unstated_rows(model)]
    rows.extend(record_rows(view, model, cursor, above=len(rows), below=len(below)))
    rows.extend(below)
    return build(
        view, rows, route_keys_bar(view, native_keys(session.route, windowed=session.windowed))
    )


def absent_card(
    view: View,
    model: Projected,
    *,
    steps: Sequence[str],
    what: str,
    unstated: Sequence[str],
    keys: Sequence[Pair],
) -> list[str]:
    """Return a card route's card when the read model holds no record for it to draw.

    A step, an artifact or a rung is opened over its parent's frame as a card, so an
    absent one is still that card -- its crumb, its box and the fields it would state,
    each wearing the unknown token -- rather than a register table of nothing.

    Args:
        view: The render being built; its session's subject is the record asked for.
        model: The read model the route was served, whose scope and counts head the card.
        steps: The crumb steps under the scope, the card's own leaf last.
        what: The record the card draws, in words (``step``, ``artifact``, ``rung``).
        unstated: The fields the card would state, none of which a producer states yet.
        keys: The keybar pairs the card still binds.

    Returns:
        The full frame, keybar last.
    """
    subject = view.session.subj_id
    held_for = f" for {subject}" if subject else ""
    labels = [name.replace("_", " ").upper() for name in unstated]
    # one label column, wide enough that the longest field name keeps a gap before its value
    width = max([LABEL_W, *(cell_len(label) + 2 for label in labels)])
    lines = [
        f"{'HELD':<{width}}{truth_cell('unavailable')} no {what} is held{held_for}",
        "",
        *(f"{label:<{width}}{UNKNOWN_WORD} · no producer states it yet" for label in labels),
    ]
    return boxed(
        view,
        crumb=route_crumb(view, model, *steps).lstrip(),
        ctx=f"no {what} is held · {counts(model)}",
        pre=[],
        title=what.upper(),
        lines=lines,
        foot=f"{'an' if what[:1] in 'aeiou' else 'a'} {what} is drawn once a producer states it",
        keys=keys,
    )


def native(view: View) -> RouteReadModel | None:
    """Return the read model the session's route draws from, when the console holds one.

    A projection built for another family is not this frame's rows, so it is not adopted:
    the route falls back to its epoch-1 frame rather than drawing another route's records.
    """
    model = view.projection
    if isinstance(model, RouteReadModel) and model.route == view.session.route:
        return model
    return None
