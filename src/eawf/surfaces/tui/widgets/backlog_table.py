"""``BacklogTable`` — sortable / filterable backlog grid (widget).

A :class:`~textual.widgets.DataTable` of the current scope's backlog
items, **sortable** by priority / id / status, **filterable** by a
substring (driven by the ``/filter backlog`` palette verb), with
``Enter`` on the cursor row opening a detail modal.

The modal screen itself is a later wave of this band (the modal-stack
inventory in the brief §5.7 lands the ``DetailModal``); this wave wires
the Enter → :class:`BacklogTable.RowActivated` message seam carrying the
selected item id, which the host screen routes to the modal once it
exists. Until then a host can subscribe to the message to drive any
drill-in. The seam is intentional — see :class:`BacklogTable.RowActivated`.

Sort + filter logic lives in pure module functions
(:func:`sort_items`, :func:`filter_items`) so the row ordering and the
filtered set are unit-testable without mounting the widget. The table is
driven by the host :class:`~eawf.surfaces.tui.app.EaApp` reactive ``state``:
on mount it seeds from ``app.state`` and registers a watcher so
daemon-pushed revisions rebuild the rows; standalone tests assign
:attr:`state` directly. Priority colours resolve against the
``theme.tcss`` palette vars — never hardcoded hex.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from textual.binding import Binding, BindingType
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable

from eawf.kernel.state.enums import BacklogPriority, BacklogStatus
from eawf.kernel.state.ids import natural_key

if TYPE_CHECKING:
    from textual.events import Resize

    from eawf.kernel.state.models import BacklogItem, State

logger = logging.getLogger(__name__)

#: The sort keys the table cycles through (the ``priority / id /
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

#: Backlog statuses that ``show_closed=False`` filters out. CLOSED rows
#: are the obvious "done" set; the toggle reveals them so an operator
#: can audit the closure trail without scrolling past them by default.
_CLOSED_STATUSES: frozenset[BacklogStatus] = frozenset({BacklogStatus.CLOSED})

#: Column ids in display order. The free-text ``title`` is last so the
#: three fixed-shape columns (id / priority / status) absorb a deterministic
#: width and the title takes the remaining budget.
_COLUMNS: tuple[str, ...] = ("id", "priority", "status", "title")

#: The non-title columns whose rendered width is subtracted from the
#: content area to size the ``title`` budget. Kept in sync with
#: :data:`_COLUMNS` (every column except the trailing free-text one).
_FIXED_COLUMNS: tuple[str, ...] = ("id", "priority", "status")

#: Floor for the computed ``title`` budget. The title column is normally
#: sized to the *rendered* column width (content area minus the fixed
#: columns + cell padding), but never below this so an extremely narrow
#: pane still shows a usable, ellipsised stub rather than a bare ``…``.
_TITLE_MIN_WIDTH: int = 8

#: The single-character ellipsis appended to a truncated title.
_ELLIPSIS: str = "…"


def _priority_rank(priority: BacklogPriority) -> int:
    """Return the numeric rank for *priority* (``P0`` lowest = first)."""
    return _PRIORITY_RANK.get(priority, len(_PRIORITY_RANK))


def _truncate(text: str, max_width: int) -> str:
    """Return *text* clipped to *max_width* with a trailing ellipsis.

    Strings within *max_width* are returned unchanged; longer strings are
    cut to ``max_width - 1`` characters plus the single-cell ellipsis so
    the result never exceeds *max_width* cells. A *max_width* below ``1`` is
    floored to ``1`` so the result is always at least the ellipsis.

    Args:
        text: The cell text to clip.
        max_width: The maximum rendered width.

    Returns:
        The original text, or a truncated ``…``-suffixed copy.
    """
    width = max(max_width, 1)
    if len(text) <= width:
        return text
    if width == 1:
        return _ELLIPSIS
    return text[: width - 1] + _ELLIPSIS


def title_budget(
    content_width: int,
    fixed_columns_width: int,
    cell_padding: int,
    column_count: int,
    *,
    floor: int = _TITLE_MIN_WIDTH,
) -> int:
    """Return the rendered cell budget for the trailing ``title`` column.

    The :class:`~textual.widgets.DataTable` lays the columns out across the
    widget's content area: every column carries *cell_padding* on each side,
    so the available text width is the content area minus the fixed columns'
    text widths and the total padding. The remainder is the title's budget,
    floored at *floor* so a very narrow pane still renders a usable stub.

    Args:
        content_width: The widget's content-area width in cells.
        fixed_columns_width: Combined text width of the non-title columns.
        cell_padding: Padding cells on *each* side of every column.
        column_count: Total number of columns (fixed + title).
        floor: Minimum budget; the result is never below this.

    Returns:
        The title column's cell budget (≥ *floor*).
    """
    padding_total = cell_padding * 2 * column_count
    available = content_width - fixed_columns_width - padding_total
    return max(available, floor)


def sort_items(items: list[BacklogItem], sort_key: str) -> list[BacklogItem]:
    """Return *items* ordered by *sort_key*, then by the remaining keys.

    The sort is **compound**, stable, and total: *sort_key* picks the
    primary ordering and the other two canonical keys (priority /
    status / id, in that fixed order) act as deterministic
    tiebreakers. Tying on priority alone — e.g. two ``P0`` rows — falls
    through to status, then to the natural id sort, so two rows with the
    same primary value still resolve in a single deterministic order
    across renders.

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
        return sorted(
            items,
            key=lambda it: (
                _priority_rank(it.priority),
                it.status.value,
                natural_key(it.id),
            ),
        )
    if sort_key == "status":
        return sorted(
            items,
            key=lambda it: (
                it.status.value,
                _priority_rank(it.priority),
                natural_key(it.id),
            ),
        )
    return sorted(
        items,
        key=lambda it: (
            natural_key(it.id),
            _priority_rank(it.priority),
            it.status.value,
        ),
    )


def hide_closed(items: list[BacklogItem]) -> list[BacklogItem]:
    """Return the subset of *items* whose status is not :attr:`BacklogStatus.CLOSED`.

    The default backlog render hides closed rows so the operator sees only
    actionable work; flipping the widget's ``show_closed`` reactive
    bypasses this filter.

    Args:
        items: The backlog items to filter.

    Returns:
        The non-CLOSED items in input order.
    """
    return [it for it in items if it.status not in _CLOSED_STATUSES]


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


def _fixed_columns_width(items: list[BacklogItem]) -> int:
    """Return the combined text width of the id / priority / status columns.

    Each fixed column auto-sizes to the wider of its header label and its
    widest cell value across *items*. Summing these gives the width the
    three fixed columns claim, which is subtracted from the content area to
    size the trailing ``title`` column.

    Args:
        items: The rows whose id / priority / status values drive the
            per-column max (empty falls back to the header-label widths).

    Returns:
        The total text width claimed by the fixed columns.
    """
    id_width = max([len("id"), *(len(it.id) for it in items)])
    priority_width = max([len("priority"), *(len(it.priority.value) for it in items)])
    status_width = max([len("status"), *(len(it.status.value) for it in items)])
    return id_width + priority_width + status_width


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
    * :meth:`clear_filter` — reset the filter to empty (the ``x`` key
      calls this via :meth:`action_clear_filter`).
    * :class:`RowActivated` — posted on Enter; carries the item id for the
      detail modal (modal screen lands in a later wave).
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "toggle_show_closed", "Show closed", show=False),
        Binding("x", "clear_filter", "Clear filter", show=False),
    ]

    DEFAULT_CSS: ClassVar[str] = """
    BacklogTable {
        height: 1fr;
        width: 1fr;
        overflow-x: hidden;
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

    #: When ``False`` (the default), backlog items with
    #: :attr:`BacklogStatus.CLOSED` are filtered out of the rendered
    #: rows; bound to the ``c`` key via
    #: :meth:`action_toggle_show_closed` so the operator can flip
    #: visibility without touching the palette.
    show_closed: reactive[bool] = reactive(False)

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

    def watch_show_closed(self) -> None:
        """Rebuild rows when the show-closed toggle flips."""
        self._rebuild()

    def action_toggle_show_closed(self) -> None:
        """Flip the :attr:`show_closed` toggle (bound to the ``c`` key)."""
        self.show_closed = not self.show_closed

    def action_clear_filter(self) -> None:
        """Clear the active substring filter (bound to the ``x`` key).

        The ``/filter backlog`` palette verb is the only way to *set* the
        filter, so without this an operator who filtered the pane had to
        reopen the palette and submit an empty needle just to see all rows
        again. The ``x`` key restores the full list in one keystroke; a
        no-op (no rebuild) when no filter is active.
        """
        if self.filter_text:
            self.clear_filter()

    def clear_filter(self) -> None:
        """Reset the substring filter to empty (restores all rows)."""
        self.filter_text = ""

    def on_resize(self, event: Resize) -> None:
        """Re-truncate titles when the pane width changes.

        The ``title`` column is sized to the rendered column width, so a
        resize must rebuild the rows for the new budget. Width-only resizes
        and height-only resizes both arrive here; the rebuild is cheap and
        idempotent, so it runs unconditionally.

        Args:
            event: The Textual resize event (unused beyond triggering the
                rebuild).
        """
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
        rendered order without scraping the rendered cells. Applies the
        substring filter, then the ``show_closed`` toggle (off by
        default — hides ``CLOSED`` rows), then the compound sort.

        Returns:
            The items in display order after sort + filter + closed-toggle.
        """
        if self.state is None or self.state.backlog is None:
            return []
        items = list(self.state.backlog.values())
        filtered = filter_items(items, self.filter_text)
        if not self.show_closed:
            filtered = hide_closed(filtered)
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
            items = self.visible_items()
            budget = self._title_budget(items)
            for item in items:
                self.add_row(
                    item.id,
                    item.priority.value,
                    item.status.value,
                    _truncate(item.title, budget),
                    key=item.id,
                )
        finally:
            self._rebuilding = False

    def _title_budget(self, items: list[BacklogItem]) -> int:
        """Return the current cell budget for the ``title`` column.

        Sizes the title to the rendered column width: the content area
        minus the fixed columns' text widths (driven by *items* + the
        header labels) and the per-cell padding. Before the widget is laid
        out (zero content width, e.g. pre-mount) this falls back to the
        floor so the first build still truncates sanely.

        Args:
            items: The rows about to be rendered (their id / priority /
                status drive the fixed columns' widths).

        Returns:
            The title column's cell budget (≥ :data:`_TITLE_MIN_WIDTH`).
        """
        content_width = self.content_size.width
        if content_width <= 0:
            return _TITLE_MIN_WIDTH
        fixed_width = _fixed_columns_width(items)
        return title_budget(
            content_width=content_width,
            fixed_columns_width=fixed_width,
            cell_padding=self.cell_padding,
            column_count=len(_COLUMNS),
        )

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
    "hide_closed",
    "next_sort_key",
    "sort_items",
    "title_budget",
]
