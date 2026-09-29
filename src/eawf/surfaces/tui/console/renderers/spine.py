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

from collections.abc import Callable

from eawf.kernel.projection.connection import staleness_target_seconds
from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.state.epoch2.transitions import TERMINAL_STATUSES, LifecycleEntity
from eawf.surfaces.tui.console.action_menu import MenuVerb, VerbWeight
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.format import group, seconds
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    needs_count,
    route_keys_bar,
    scope_label,
    thin,
    window_rows,
)
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import KEY, KeyEntry
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.lifecycle import ELAPSED_WORDS, Layout
from eawf.surfaces.tui.console.reads import attached, reads
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers.children import Record, lrow, status
from eawf.surfaces.tui.console.renderers.detail import (
    GUTTER,
    at,
    state_of,
    subject_line,
    subject_of,
    subject_rows,
    unknown_frame,
)
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    cell,
    counts,
    crumb,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import pad

_ROWS = Table([34, 12, 0], 2)
_EMPTY = "   this scope holds no record the read model renders"


def _label(name: str, text: str) -> str:
    """Return a row in the gutter the generic spine frame's subject rows sit in."""
    return f" {name:<{GUTTER}}{text}"


def finished_subject(session: Session, model: SpineView | RouteReadModel) -> Record | None:
    """Return the frame's subject when it is an entity whose lifecycle has ended.

    Terminality is read off the stored status against the entity's own status machine,
    never off the connection: a finished Run on a live console is still finished, and a
    running one on an offline console is not.

    Args:
        session: The session whose subject the frame is drawn for.
        model: The read model the frame draws.

    Returns:
        The subject's row, or ``None`` when there is no subject, it is not a lifecycle
        entity, its status is not stated, or the status has an outgoing edge.
    """
    found = model.index_of(session.subj_id)
    if found is None:
        return None
    row: Record = model.rows[found]
    return row if finished(row) else None


def finished(row: Record) -> bool:
    """Return whether ``row`` is a lifecycle entity whose stored status has no way out."""
    status = row.field("status")
    entity = next((e for e in LifecycleEntity if e.value == row.collection.value), None)
    if entity is None or status.state is not TruthState.KNOWN or status.value is None:
        return False
    return status.value in TERMINAL_STATUSES[entity]


def finished_rows(route: str, row: Record, labelled: Callable[[str, str], str]) -> list[str]:
    """Return what a finished subject states in place of the connection line.

    Args:
        route: The route the frame is drawn for, whose terminal staleness target it states.
        row: The finished subject.
        labelled: The frame's own label row, so the band sits in the frame's one gutter.
    """
    ages = seconds(staleness_target_seconds(route, terminal=True))
    return [
        labelled("FINAL", f"{row.key} {row.field('status').value} · finished, nothing is wrong"),
        labelled("", f"it ages informationally after {ages} · no lifecycle left"),
    ]


def offered_verbs(session: Session, fixture: Fixture, model: object) -> tuple[MenuVerb, ...]:
    """Return the verbs the route's action menu offers on the frame's subject.

    A finished subject has no lifecycle left, so its menu keeps only the light verbs --
    the ones that open another surface of the record, such as its Git -- and none that
    would move it.

    Args:
        session: The session whose route and subject the menu is for.
        fixture: The registers holding the route's menu.
        model: The read model the frame draws; only a projected model states a finished
            subject.
    """
    verbs = fixture.menus.verbs(session.route)
    if (
        isinstance(model, SpineView | RouteReadModel)
        and finished_subject(session, model) is not None
    ):
        return tuple(verb for verb in verbs if verb.weight is VerbWeight.LIGHT)
    return verbs


def detail_keys(view: View, spine: SpineView) -> list[KeyEntry]:
    """Return the keys a detail frame offers: the route's, the menu only while it offers a verb.

    A finished subject's menu keeps its light verbs, so ``.`` is offered on it while one
    is left and neither offered nor bound once none is.
    """
    session = view.session
    offered = bool(offered_verbs(session, view.fixture, spine))
    return [
        e
        for e in native_keys(session.route, windowed=session.windowed)
        if offered or e != KEY["actions"]
    ]


def detail_head(view: View, spine: SpineView, subject: SpineRow) -> list[str]:
    """Return a detail frame's head: the crumb through its parent, then its subject line.

    The line under the header names what the frame is about -- the kind, the record and
    its stored status -- and the crumb climbs one level to the record it is filed under.
    A finished subject states its final state under the rule instead of a reads line, and
    the frame's own sections open with the state's meaning and the clock it allows.

    Args:
        view: The render being built.
        spine: The route's read model, whose scope opens the crumb.
        subject: The record the frame is about.

    Returns:
        The head rows, then the subject's state and clock rows when its status is stated.
    """
    session, w = view.session, view.w
    finished = finished_subject(session, spine)
    steps = [*([subject.parent_key] if subject.parent_key else []), subject.key]
    rows = native_head(
        view,
        spine,
        crumb_text=route_crumb(view, spine, *steps),
        # terminal is the subject's property, so the summary states it, not the header
        summary=subject_line(
            subject, f"{status(subject)}{' · final' if finished is not None else ''}", w
        ),
        terminal=finished is not None,
    )
    if finished is not None:
        rows += [*finished_rows(session.route, finished, lrow), thin(w)]
    state = state_of(subject)
    if state is not None:
        stamp = subject.facts.get("updated_at")
        moved = f" · last moved {at(stamp)}" if stamp else ""
        rows += [
            lrow("STATE", f"{state.state} · {state.meaning}"),
            lrow("CLOCK", f"{ELAPSED_WORDS[state.elapsed]}{moved}"),
        ]
    return rows


def _unstated(spine: SpineView) -> str:
    """Return the declared columns no producer states, each beside the unknown token."""
    return " UNSTATED  " + " · ".join(f"{name} {UNKNOWN_WORD}" for name in spine.unproduced())


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
    finished = finished_subject(session, spine)
    subject = subject_of(view, spine)
    state = state_of(subject) if subject is not None else None
    if subject is not None and state is not None and state.layout is Layout.UNKNOWN:
        return unknown_frame(view, spine, subject, detail_keys(view, spine))
    landing = f"{session.route}:{subject.key}" if subject is not None else None
    seen = session.rec_seen or {}
    if subject is not None and landing is not None and not seen.get(landing):
        # a frame opened on a record starts with the cursor on that record, and only then:
        # a later move walks off it rather than snapping back
        session.sel_id = subject.key
        session.rec_seen = {**seen, landing: 1}
    cursor = restore(session, spine)
    regions = REGISTRY.focus_regions.get(session.route, ())
    rows: list[str] = [
        header_row(
            session,
            crumb=crumb(view, spine),
            scope=scope_label(view, spine.scope_id),
            needs=needs_count(view),
            w=w,
        ),
        " " + (counts(spine) if subject is None else _subject_summary(subject, finished, w)),
        bar(w),
    ]
    rd = reads(session)
    if finished is not None:
        rows.extend([*finished_rows(session.route, finished, _label), thin(w)])
    elif not rd.complete:
        rows.extend(
            [f" ATTACHED  {attached(rd, revision=group(int(spine.source_cursor)))}", thin(w)]
        )
    if subject is not None and state is not None:
        rows.extend(subject_rows(subject, state, w))
    if regions:
        rows.append(" REGIONS   " + " · ".join(regions))
        rows.append(thin(w))
    rows.append(_ROWS.head(["ROW", "KIND", "STATUS"]))
    below = [thin(w), _unstated(spine)]
    win = window_rows(view, total=len(spine.rows), cursor=cursor, chrome=len(rows) + 1 + len(below))
    if not spine.rows:
        rows.append(_EMPTY)
    for index in range(win.start, win.stop):
        row = spine.rows[index]
        # the kind is the collection the read model states, never guessed from the id
        cells = [row.key, row.collection.value, cell(row.field("status"))]
        line = _ROWS.row(cells, index == cursor)
        rows.append(line if index == cursor else Fixed(pad(line, w)))
    rows.append(win.line(complete=spine.complete))
    rows.extend(below)
    return build(view, rows, route_keys_bar(view, detail_keys(view, spine)))


def _subject_summary(subject: SpineRow, finished: Record | None, w: int) -> str:
    """Return the summary of a frame drawn for one record: its name and its state."""
    state = value_cell(subject.field("status")).slot
    return subject_line(subject, f"{state} · final" if finished is not None else state, w)
