"""``BacklogTable`` — sortable / filterable backlog grid (C06 widget).

Per the C06 brief §5.3 widget row: a :class:`~textual.widgets.DataTable`
of the current scope's backlog items, **sortable** by priority / id /
status, **filterable** by a substring (driven by the ``/filter backlog``
palette verb), with ``Enter`` on the cursor row opening a detail modal.

The modal screen itself is a later wave of this band (the modal-stack
inventory in the brief §5.7 lands the ``DetailModal``); this wave wires
the Enter → :class:`BacklogTable.RowActivated` message seam carrying the
selected item id, which the host screen routes to the modal once it
exists. Until then a host can subscribe to the message to drive any
drill-in. The seam is intentional — see :class:`BacklogTable.RowActivated`.

Sort + filter logic lives in pure module functions
(:func:`sort_items`, :func:`filter_items`) so the row ordering and the
filtered set are unit-testable without mounting the widget. The table is
driven by the host :class:`~eawf.tui_v2.app.EaApp` reactive ``state``:
on mount it seeds from ``app.state`` and registers a watcher so
daemon-pushed revisions rebuild the rows; standalone tests assign
:attr:`state` directly. Priority colours resolve against the
``theme.tcss`` palette vars — never hardcoded hex.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable

from eawf.state.enums import BacklogPriority

if TYPE_CHECKING:
    from eawf.state.models import BacklogItem, State

#: The sort keys the table cycles through (the C06 ``priority / id /
#: target`` row; ``target`` maps onto ``status`` for backlog rows, which
#: carry no dedicated target field). Pressing the sort key advances to the
#: next member, wrapping at the end.
SORT_KEYS: tuple[str, ...] = ("priority", "id", "status")

#: Numeric rank for each backlog priority so ``P0`` sorts ahead of ``P3``
#: (the enum's string order is already ascending, but an explicit map
#: keeps the sort total and independent of enum declaration order).
_PRIORITY_RANK: dict[BacklogPriority, int] = {
    BacklogPriority.P0: 0,
    BacklogPriority.P1: 1,
    BacklogPriority.P2: 2,
    BacklogPriority.P3: 3,
}

#: Column ids in display order.
_COLUMNS: tuple[str, ...] = ("id", "priority", "status", "title")


def _priority_rank(priority: BacklogPriority) -> int:
    """Return the numeric rank for *priority* (``P0`` lowest = first)."""
    return _PRIORITY_RANK.get(priority, len(_PRIORITY_RANK))


def sort_items(items: list[BacklogItem], sort_key: str) -> list[BacklogItem]:
    """Return *items* ordered by *sort_key*.

    The sort is stable and total: every key falls back to the item id so
    ties resolve deterministically.

    Args:
        items: The backlog items to order.
        sort_key: One of :data:`SORT_KEYS`.

    Returns:
        A new sorted list (the input is not mutated).

    Raises:
        ValueError: When *sort_key* is not in :data:`SORT_KEYS`.
    """
    if sort_key not in SORT_KEYS:
        raise ValueError(f"unknown sort key: {sort_key!r}")
    if sort_key == "priority":
        return sorted(items, key=lambda it: (_priority_rank(it.priority), it.id))
    if sort_key == "status":
        return sorted(items, key=lambda it: (it.status.value, it.id))
    return sorted(items, key=lambda it: it.id)


def filter_items(items: list[BacklogItem], needle: str) -> list[BacklogItem]:
    """Return the subset of *items* matching *needle* (case-insensitive).

    Matches against the item id and title. An empty / whitespace *needle*
    returns the full list unchanged so clearing the filter restores all
    rows.

    Args:
        items: The backlog items to filter.
        needle: The substring to match (case-insensitive).

    Returns:
        The matching items in input order.
    """
    trimmed = needle.strip().lower()
    if not trimmed:
        return list(items)
    return [it for it in items if trimmed in it.id.lower() or trimmed in it.title.lower()]


def next_sort_key(current: str) -> str:
    """Return the sort key after *current* in the :data:`SORT_KEYS` cycle.

    Args:
        current: The current sort key.

    Returns:
        The next key, wrapping at the end. An unrecognised *current*
        resets to the first key.
    """
    try:
        index = SORT_KEYS.index(current)
    except ValueError:
        return SORT_KEYS[0]
    return SORT_KEYS[(index + 1) % len(SORT_KEYS)]


class BacklogTable(DataTable[str]):
    """Sortable / filterable backlog grid with an Enter → modal seam.

    Public surface for a host screen:

    * :meth:`cycle_sort` — advance the sort key (bind to a key).
    * :meth:`apply_filter` — set the substring filter (the ``/filter
      backlog`` palette verb calls this).
    * :class:`RowActivated` — posted on Enter; carries the item id for the
      detail modal (modal screen lands in a later wave).
    """

    DEFAULT_CSS: ClassVar[str] = """
    BacklogTable {
        height: 1fr;
        width: 1fr;
    }
    """

    class RowActivated(Message):
        """Posted when the operator presses Enter on a backlog row.

        The host screen routes this to the ``DetailModal`` (modal stack
        lands in a later wave of this band). Until the modal exists this
        message is the documented drill-in seam.

        Attributes:
            item_id: The activated backlog item's id (the row key).
        """

        def __init__(self, item_id: str) -> None:
            self.item_id = item_id
            super().__init__()

    #: Bound state, watched so a fresh revision rebuilds the rows.
    state: reactive[State | None] = reactive(None)

    #: Active sort key (one of :data:`SORT_KEYS`).
    sort_key: reactive[str] = reactive(SORT_KEYS[0])

    #: Active substring filter (empty = show all).
    filter_text: reactive[str] = reactive("")

    def __init__(self, **kwargs: Any) -> None:
        """Construct the table with row-cursor selection.

        Args:
            **kwargs: Forwarded to :class:`textual.widgets.DataTable`.
        """
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self._rebuilding = False

    def on_mount(self) -> None:
        """Add columns, seed from app state, and watch for revisions."""
        for column in _COLUMNS:
            self.add_column(column, key=column)
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        self._rebuild()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def watch_state(self) -> None:
        """Rebuild rows when the bound state changes."""
        self._rebuild()

    def watch_sort_key(self) -> None:
        """Rebuild rows when the sort key changes."""
        self._rebuild()

    def watch_filter_text(self) -> None:
        """Rebuild rows when the filter changes."""
        self._rebuild()

    def cycle_sort(self) -> None:
        """Advance the sort key to the next member of the cycle."""
        self.sort_key = next_sort_key(self.sort_key)

    def apply_filter(self, needle: str) -> None:
        """Set the substring filter (``/filter backlog`` entry point).

        Args:
            needle: The substring to match; empty clears the filter.
        """
        self.filter_text = needle

    def visible_items(self) -> list[BacklogItem]:
        """Return the current sorted + filtered backlog items.

        Pure-ish accessor (no side effects) so a host / test can read the
        rendered order without scraping the rendered cells.

        Returns:
            The items in display order after sort + filter.
        """
        if self.state is None or self.state.backlog is None:
            return []
        items = list(self.state.backlog.values())
        filtered = filter_items(items, self.filter_text)
        return sort_items(filtered, self.sort_key)

    def _rebuild(self) -> None:
        """Repopulate the table rows from the current state + sort + filter.

        Columns are added once in :meth:`on_mount`; this only clears and
        re-adds rows so the header survives. Each row key is the item id so
        :meth:`on_data_table_row_selected` can resolve the selection.

        The :attr:`_rebuilding` re-entrancy guard coalesces nested calls:
        when the widget mounts with the app's reactive ``state`` already
        populated, the ``state`` / ``sort_key`` / ``filter_text`` watchers
        and the explicit ``on_mount`` call can all fire in the same flush.
        Without the guard a watcher re-enters :meth:`_rebuild` mid
        add-loop and re-adds a row key the outer loop is still iterating,
        raising :class:`~textual.widgets._data_table.DuplicateKey`. The
        guarded re-entrant call is a no-op; the outer call renders the
        authoritative row set from the current reactive values.
        """
        if not self.columns or self._rebuilding:
            return
        self._rebuilding = True
        try:
            self.clear()
            for item in self.visible_items():
                self.add_row(
                    item.id,
                    item.priority.value,
                    item.status.value,
                    item.title,
                    key=item.id,
                )
        finally:
            self._rebuilding = False

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Post :class:`RowActivated` for the Enter-selected row.

        Args:
            event: The Textual row-selected event; ``row_key.value`` is the
                backlog item id used as the row key.
        """
        item_id = event.row_key.value
        if item_id is not None:
            self.post_message(self.RowActivated(item_id))


__all__ = [
    "SORT_KEYS",
    "BacklogTable",
    "filter_items",
    "next_sort_key",
    "sort_items",
]
