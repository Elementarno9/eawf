"""The labelled facts and child lists a native detail frame draws about one record.

A detail frame is about its subject, so what it lists are the records filed under that
subject, found by their parent key -- a Batch lists its own Tasks, a Milestone its own
Batches -- and never the register the subject sits in, whose other rows are siblings.
The facts beside the list sit in one label gutter, two cells in, so the value column is
the same on every detail route and the caret lands in the cells between label and value.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from eawf.kernel.projection.truth import TruthField
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.frame import (
    Fixed,
    View,
    recede,
    window_rows,
)
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import pad

#: The cells a fact label takes after its two-cell gutter.
LABEL_W = 11


class Record(Protocol):
    """A projected record a detail frame names: the spine rows and the route rows alike."""

    @property
    def key(self) -> str:
        """The record's public key."""

    @property
    def title(self) -> str | None:
        """The record's own title, when it states one."""

    @property
    def parent_key(self) -> str | None:
        """The key of the record it is filed under, when it names one."""

    @property
    def collection(self) -> Epoch2Collection:
        """The collection the record was read from."""

    def field(self, name: str) -> TruthField[str]:
        """Return one declared field."""


def lrow(label: str, value: str, cur: bool = False) -> str:
    """Return a labelled fact row, the caret between label and value when ``cur``."""
    return "  " + pad(label, LABEL_W) + ("▸ " if cur else "  ") + value


def status(row: Record) -> str:
    """Return the record's stored status as its cell, or the unknown token."""
    return value_cell(row.field("status")).slot


def named(row: Record) -> str:
    """Return the record's key followed by its title, when it states one."""
    return f"{row.key} {row.title}" if row.title else row.key


def reference(row: Record) -> str:
    """Return the record as a child row reads it: key, title, then status."""
    return f"{named(row)} · {status(row)}"


def children[R: Record](rows: Sequence[R], parent: str, collection: Epoch2Collection) -> list[R]:
    """Return the records of ``collection`` filed under ``parent``, in projection order."""
    return [row for row in rows if row.collection is collection and row.parent_key == parent]


def child_cursor(session: Session, keys: Sequence[str], *, subject: str) -> int:
    """Return the child the caret sits on, restored by stable id, and publish the list.

    The published keys are what Enter drills into, so the caret and the drill always
    agree about which record is under the cursor.

    Args:
        session: The session whose ``sel_id`` names the child the cursor was on.
        keys: The children's keys, in the order the frame lists them.
        subject: The frame's own record, which the selection names when nothing is
            filed under it, so the action menu acts on the record on screen.

    Returns:
        The offset of the child under the caret; ``0`` for an empty list.
    """
    found = keys.index(session.sel_id) if session.sel_id in keys else None
    index = found if found is not None else min(max(session.sel, 0), max(len(keys) - 1, 0))
    session.sel = index
    session.sel_id = keys[index] if keys else subject
    # the arrows walk the children and Enter opens one, so an empty list offers neither
    session.nav_rows = len(keys)
    dv.publish_nav(session, list(keys))
    return index


def child_rows(
    view: View,
    label: str,
    lines: Sequence[str],
    cursor: int | None,
    *,
    chrome: int,
    empty: str,
) -> list[str]:
    """Return one labelled child list, windowed so the caret stays on screen.

    Args:
        view: The render being built; its height bounds the window.
        label: The label the first row carries.
        lines: One value per child, in list order.
        cursor: The offset of the child under the caret, or ``None`` when the list does
            not hold the focus, which draws it receded and without a caret.
        chrome: Every frame row that is not a row of this list, the keybar excepted.
        empty: What the list says when nothing is filed under the subject.

    Returns:
        The list's rows, with a ``WINDOW`` row only when some child is off screen.
    """
    if not lines:
        return [lrow(label, empty)]
    win = window_rows(view, total=len(lines), cursor=cursor or 0, chrome=chrome + 1)
    rows: list[str] = []
    for index in range(win.start, win.stop):
        on = cursor is not None and index == cursor
        line = lrow(label if index == win.start else "", lines[index], on)
        held = cursor is not None
        rows.append(line if on else Fixed(pad(line, view.w)) if held else recede(line, view.w))
    if win.hides:
        rows.append(lrow("", win.line().removeprefix(" WINDOW    ")))
    return rows


__all__ = [
    "LABEL_W",
    "Record",
    "child_cursor",
    "child_rows",
    "children",
    "lrow",
    "named",
    "reference",
    "status",
]
