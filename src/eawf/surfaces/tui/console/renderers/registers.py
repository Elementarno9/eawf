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

from eawf.kernel.projection.attention import NOTIFICATION_MATRIX, attention_mine
from eawf.kernel.projection.registers import RegisterView, budget_reading
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    route_keys_bar,
    thin,
    window_rows,
)
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.renderers.read_model import (
    UNAVAILABLE,
    UNKNOWN_WORD,
    cell,
    counts,
    crumb,
    native_header,
    restore,
)
from eawf.surfaces.tui.console.width import pad

#: The row the frame shows where a register nothing writes would have been counted.
UNWRITTEN_ROW = " UNWRITTEN "

_ROWS = Table([34, 12, 0], 2)
_EMPTY = "   this scope holds no record the read model renders"


def _withheld(register: RegisterView) -> list[str]:
    """Return the row naming every bound register nothing writes, or no row at all."""
    if not register.withheld:
        return []
    return [
        UNWRITTEN_ROW + " " + " · ".join(f"{name} {UNKNOWN_WORD}" for name in register.withheld)
    ]


def _activity_block(_view: View, register: RegisterView) -> list[str]:
    """Return Activity's bucket counts, each derived from the rows that stated it."""
    buckets = register.status_counts()
    stated = " · ".join(f"{name} {group(count)}" for name, count in buckets.items())
    rows = [" BUCKETS   " + (stated or UNAVAILABLE)]
    unstated = register.unstated_rows()
    if unstated:
        rows.append(f" UNBUCKETED {group(unstated)} in no bucket · the row states no status")
    return rows


def _attention_block(view: View, register: RegisterView) -> list[str]:
    """Return Attention's ``mine`` row, the one count the header prints too."""
    mine = value_cell(
        attention_mine(register, principal=view.principal),
        spell=lambda n: group(int(n)),
        exempt=True,
    )
    rows = [f" MINE      {mine.slot}"]
    if mine.reason:
        rows.append(f"           {mine.reason}")
    return rows


def _budget_block(_view: View, register: RegisterView) -> list[str]:
    """Return the budget reading both budget routes draw, read off the notice contract."""
    reading = budget_reading(register)
    answer = "interrupts you" if reading.interrupts else "does not interrupt you"
    rows = [
        f" BUDGET    a crossing opens {reading.control} · leaves {reading.terminal_status}",
        f"           a budget notice {answer}",
    ]
    rows.append(f" STOPPED   {value_cell(reading.stopped).full}")
    return rows


def _notifications_block(_view: View, register: RegisterView) -> list[str]:
    """Return the presentation matrix: each class, whether it may toast, who decided."""
    return [
        " CLASS                TOAST              DECIDED BY",
        *(
            f" {pad(row.notification_class.value, 21)}{pad(row.may_interrupt.value, 19)}"
            f"{row.decided_by}"
            for row in NOTIFICATION_MATRIX.classes
        ),
        f" STOPPED   {value_cell(budget_reading(register).stopped).full}",
    ]


_BLOCKS: Mapping[str, Callable[[View, RegisterView], list[str]]] = MappingProxyType(
    {
        "activity": _activity_block,
        "attention": _attention_block,
        "cost.ceiling": _budget_block,
        "notifications": _notifications_block,
    }
)


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
        native_header(view, crumb(view, register), register.scope_id),
        " " + counts(register),
        bar(w),
    ]
    rows.extend(_BLOCKS[register.route](view, register))
    rows.append(thin(w))
    rows.append(_ROWS.head(["ROW", "KIND", "STATUS"]))
    withheld = _withheld(register)
    below = [thin(w), *withheld] if withheld else []
    win = window_rows(
        view, total=len(register.rows), cursor=cursor, chrome=len(rows) + 1 + len(below)
    )
    if not register.rows:
        rows.append(_EMPTY)
    for index in range(win.start, win.stop):
        row = register.rows[index]
        # the kind is the collection the read model states, never guessed from the id
        cells = [row.key, row.collection.value, cell(row.status)]
        line = _ROWS.row(cells, index == cursor)
        rows.append(line if index == cursor else Fixed(pad(line, w)))
    rows.append(win.line(complete=register.complete))
    rows.extend(below)
    return build(
        view,
        rows,
        route_keys_bar(view, native_keys(register.route, windowed=view.session.windowed)),
    )
