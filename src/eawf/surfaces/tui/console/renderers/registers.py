"""The native register frame: what Activity, Attention, Notifications and Cost ceiling draw.

The four register routes share this frame for the reason the five spine routes share
theirs: what they draw is rows the daemon projected at one committed cursor, and nothing
else. Only what the read model states is rendered.

The one thing this frame says that the spine frame does not is the difference between a
register that was read and held nothing and a register nothing writes. The first prints
``0``, because the count was taken. The second prints the unknown truth token beside the
register's name, because no count exists to take -- an operator reading ``0 actions`` off
a register with no producer would be reading a claim the console is in no position to make.

Activity's buckets are the statuses its rows state, counted off those rows. A row that
states no status falls in no bucket and is reported apart, so the buckets never claim more
rows than stated one.

Cost ceiling and Notifications draw the budget reading, which names the control a budget
termination opens, the status it leaves and whether the crossing interrupts anybody. Which
Runs a cap stopped is a run-ledger fact, so it prints the unknown token rather than
promoting a terminal status into a reason a Run ended.

A route whose register is not held renders its epoch-1 frame instead: the console still
opens against the prototype registers, and the tracked golden contract is that mode.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from eawf.kernel.projection.registers import (
    RegisterView,
    attention_mine,
    budget_reading,
)
from eawf.kernel.projection.truth import TruthState
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
)
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.renderers.read_model import UNAVAILABLE, counts, crumb
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import truth_cell
from eawf.surfaces.tui.console.width import pad

#: The row the frame shows where a register nothing writes would have been counted.
UNWRITTEN_ROW = " UNWRITTEN "

_ROWS = Table([34, 12, 0], 2)
_EMPTY = "   this scope holds no record the read model renders"


def _unknown_token() -> str:
    """Return the truth token a value with no producer renders as."""
    return truth_cell("unknown")


def _withheld(register: RegisterView) -> list[str]:
    """Return the row naming every bound register nothing writes, or no row at all."""
    if not register.withheld:
        return []
    token = _unknown_token()
    return [UNWRITTEN_ROW + " " + " · ".join(f"{name} {token}" for name in register.withheld)]


def _activity_block(_view: View, register: RegisterView) -> list[str]:
    """Return Activity's bucket counts, each derived from the rows that stated it."""
    buckets = register.status_counts()
    stated = " · ".join(f"{name} {group(count)}" for name, count in buckets.items())
    rows = [" BUCKETS   " + (stated or UNAVAILABLE)]
    unstated = register.unstated_rows()
    if unstated:
        rows.append(f" UNBUCKETED {group(unstated)} in no bucket · the row states no status")
    return rows


def _attention_block(_view: View, register: RegisterView) -> list[str]:
    """Return Attention's ``mine`` row, the one count the header prints too."""
    mine = attention_mine(register)
    known = mine.state is TruthState.KNOWN and mine.value is not None
    cell = group(int(mine.value)) if known and mine.value else _unknown_token()
    rows = [f" MINE      {cell} · nothing here opened itself"]
    if not known and mine.missing_reason:
        rows.append(f"           {mine.missing_reason}")
    return rows


def _budget_block(_view: View, register: RegisterView) -> list[str]:
    """Return the budget reading both budget routes draw, read off the notice contract."""
    reading = budget_reading(register)
    answer = "interrupts you" if reading.interrupts else "does not interrupt you"
    rows = [
        f" BUDGET    a crossing opens {reading.control} · leaves {reading.terminal_status}",
        f"           a budget notice {answer}",
    ]
    stopped = reading.stopped
    rows.append(f" STOPPED   {_unknown_token()} {stopped.missing_reason or ''}".rstrip())
    return rows


_BLOCKS: Mapping[str, Callable[[View, RegisterView], list[str]]] = MappingProxyType(
    {
        "activity": _activity_block,
        "attention": _attention_block,
        "cost.ceiling": _budget_block,
        "notifications": _budget_block,
    }
)


def restore(session: Session, register: RegisterView) -> int:
    """Return the row the cursor sits on, restored by stable id, and publish that id.

    Args:
        session: The session whose ``sel_id`` names the row the cursor was on and whose
            ``sel`` is the offset the frame draws the caret at.
        register: The read model the frame draws.

    Returns:
        The row offset the caret goes on; ``0`` for an empty read model, which draws no
        caret at all.
    """
    found = register.index_of(session.sel_id)
    index = found if found is not None else min(max(session.sel, 0), max(len(register.rows) - 1, 0))
    session.sel = index
    session.sel_id = register.rows[index].key if register.rows else None
    return index


def native_frame(view: View, register: RegisterView) -> list[str]:
    """Return one register route's frame, drawn from the read model the daemon served.

    Args:
        view: The render being built; its session carries the cursor and the selection.
        register: The route's read model at the committed cursor.

    Returns:
        The full frame, keybar last.

    Raises:
        KeyError: ``register`` names a route this frame draws no block for, which the
            register declarations make unreachable for a declared route.
    """
    session, w = view.session, view.w
    cursor = restore(session, register)
    rows: list[str] = [
        header_row(
            session,
            crumb=crumb(view, register),
            scope=register.scope_id,
            needs=needs_count(view),
            w=w,
        ),
        " " + counts(register),
        bar(w),
    ]
    rows.extend(_BLOCKS[register.route](view, register))
    rows.append(thin(w))
    rows.append(_ROWS.head(["ROW", "KIND", "STATUS"]))
    if not register.rows:
        rows.append(_EMPTY)
    for index, row in enumerate(register.rows):
        status = row.status
        stated = status.value if status.state is TruthState.KNOWN and status.value else None
        # the kind is the collection the read model states, never guessed from the id
        cells = [row.key, row.collection.value, stated or _unknown_token()]
        line = _ROWS.row(cells, index == cursor)
        rows.append(line if index == cursor else Fixed(pad(line, w)))
    withheld = _withheld(register)
    if withheld:
        rows.append(thin(w))
        rows.extend(withheld)
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS[register.route]))
