"""``EventsModal`` — the ``/events`` live event-log overlay.

The ``/events`` palette verb opens a scrollable ring-buffer view of the
most recent events: the last 50 events, newest first, with an ``f``
filter cycle (all → errors-only → reports-only → all). ``Esc`` closes.

**Data source.** The long-term shape is a session-only ring buffer
fed by the daemon ``event.subscribe`` push stream; that stream
does not exist on the read-only :class:`~eawf.tui_v2.state_binding.StateBinding`
fallback this band ships. So this wave reads the **on-disk event store**
(``<state_dir>/store/event.jsonl``) read-only via :func:`load_recent_events`
and renders the tail — the same rows the daemon would replay on subscribe.
When the push stream lands, the live ring buffer prepends pushed events to
this seed tail; the filter + render path here is reused unchanged.

The row model (:class:`EventRow`) and the filter predicate
(:func:`filter_rows` / :data:`EVENT_FILTERS`) are pure so the parse,
tail-truncation, and filter cycle are unit-testable without mounting
Textual; the modal is a thin scrollable view over them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

import orjson
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)

#: The ring-buffer cap — the overlay shows at most this many events,
#: newest first.
EVENT_RING_SIZE: int = 50

#: The ``f`` filter cycle, in order. ``all`` shows every row; ``errors``
#: keeps non-ok-status rows; ``reports`` keeps agent-report events.
EventFilter = Literal["all", "errors", "reports"]

#: The filter cycle order the ``f`` keypress walks (wraps to the front).
EVENT_FILTERS: tuple[EventFilter, ...] = ("all", "errors", "reports")

#: Status token that marks a healthy event (anything else is an "error"
#: under the errors-only filter).
_OK_STATUS: str = "ok"

#: ``event_type`` substring that marks an agent-report event for the
#: reports-only filter (report events carry ``report`` in their type).
_REPORT_MARKER: str = "report"


@dataclass(frozen=True)
class EventRow:
    """One rendered event row (a flattened view of an event envelope).

    Attributes:
        event_id: The envelope id (``EV-...``).
        timestamp: The ISO-8601 event timestamp string (as stored).
        event_type: The human ``event_type`` label (e.g. ``wave close``).
        status: The event status (``ok`` / an error token).
        summary: The envelope summary line.
    """

    event_id: str
    timestamp: str
    event_type: str
    status: str
    summary: str

    @property
    def is_error(self) -> bool:
        """``True`` when the row's status is not the healthy token."""
        return self.status != _OK_STATUS

    @property
    def is_report(self) -> bool:
        """``True`` when the row is an agent-report event."""
        return _REPORT_MARKER in self.event_type.lower()


def _row_from_envelope(record: dict[str, object]) -> EventRow | None:
    """Flatten a raw event-store JSONL record into an :class:`EventRow`.

    Tolerant of partial rows: a record missing the ``payload`` block or
    its fields falls back to empty strings rather than raising, so a
    single malformed row never blanks the whole overlay.

    Args:
        record: A decoded JSONL envelope dict.

    Returns:
        The flattened row, or ``None`` when the record is not a dict-
        shaped envelope.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    return EventRow(
        event_id=str(record.get("id", "")),
        timestamp=str(payload.get("timestamp", "")),
        event_type=str(payload.get("event_type", "")),
        status=str(payload.get("status", "")),
        summary=str(record.get("summary", "")),
    )


def load_recent_events(
    event_path: Path | None,
    limit: int = EVENT_RING_SIZE,
) -> tuple[EventRow, ...]:
    """Read the tail of the event store, newest first (read-only).

    Reads at most *limit* rows from ``event.jsonl``, newest first. The
    read is total: a missing / unreadable file yields an empty tuple, and
    individual malformed lines are skipped rather than aborting the read,
    so the overlay degrades to "no events" instead of crashing on a fresh
    or partially written store. Never mutates the file.

    Args:
        event_path: Path to ``<state_dir>/store/event.jsonl``, or ``None``
            when no scope state is resolved.
        limit: The ring-buffer cap — the most recent *limit* rows.

    Returns:
        The most recent rows, newest first (empty when none readable).
    """
    if event_path is None or not event_path.is_file():
        return ()
    try:
        raw = event_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"load_recent_events path={event_path!s} unreadable cause={exc!r}")
        return ()
    rows: list[EventRow] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = orjson.loads(stripped)
        except orjson.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        row = _row_from_envelope(record)
        if row is not None:
            rows.append(row)
    rows.reverse()
    return tuple(rows[:limit])


def filter_rows(rows: tuple[EventRow, ...], event_filter: EventFilter) -> tuple[EventRow, ...]:
    """Filter *rows* to the active ``f``-cycle filter.

    Args:
        rows: The full row set (already newest-first, tail-truncated).
        event_filter: The active filter (``all`` / ``errors`` /
            ``reports``).

    Returns:
        The rows matching the filter (the full set for ``all``).
    """
    if event_filter == "errors":
        return tuple(row for row in rows if row.is_error)
    if event_filter == "reports":
        return tuple(row for row in rows if row.is_report)
    return rows


def next_filter(current: EventFilter) -> EventFilter:
    """Return the next filter in the ``f`` cycle (wrapping to the front).

    Args:
        current: The active filter.

    Returns:
        The next filter in :data:`EVENT_FILTERS` order.
    """
    index = EVENT_FILTERS.index(current)
    return EVENT_FILTERS[(index + 1) % len(EVENT_FILTERS)]


def _render_row(row: EventRow) -> str:
    """Render one :class:`EventRow` as a content-markup line.

    Error rows are tinted with the ``$error`` theme var (resolved by
    Textual content markup at render time so the colour follows the active
    theme); healthy rows render plain.

    Args:
        row: The row to render.

    Returns:
        A content-markup string for a single :class:`~textual.widgets.Static`.
    """
    head = f"{row.timestamp}  {row.event_type:<24} {row.status}"
    body = f"{head}  {row.summary}" if row.summary else head
    if row.is_error:
        return f"[$error]{body}[/]"
    return body


class EventsModal(ModalScreen[None]):
    """Scrollable last-50 event ring buffer with an ``f`` filter cycle.

    Built with a pre-loaded tuple of :class:`EventRow` (the host resolves
    them from the event store via :func:`load_recent_events`) so the
    overlay never reaches into App state itself. ``f`` cycles the filter,
    ``Esc`` closes.
    """

    DEFAULT_CSS: ClassVar[str] = """
    EventsModal {
        align: center middle;
    }
    EventsModal > #events-card {
        width: 90%;
        height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    EventsModal .events-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    EventsModal #events-list {
        height: 1fr;
    }
    EventsModal .events-row {
        height: auto;
    }
    EventsModal .events-empty {
        color: $text-muted;
        height: 1;
    }
    EventsModal .events-hint {
        color: $text-muted;
        height: 1;
    }
    """

    #: ``f`` cycles the filter, ``Esc`` closes. The only two bindings the
    #: overlay owns.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f", "cycle_filter", "filter", show=False),
        Binding("escape", "close", "close", show=False),
    ]

    #: The active ``f``-cycle filter. Watched so a cycle repaints the list.
    event_filter: reactive[EventFilter] = reactive[EventFilter]("all")

    def __init__(self, rows: tuple[EventRow, ...]) -> None:
        """Construct the overlay for a pre-loaded row set.

        Args:
            rows: The most-recent events, newest first (built by the host
                from the event store via :func:`load_recent_events`).
        """
        super().__init__()
        self._rows = rows

    def compose(self) -> ComposeResult:
        """Yield the titled card, the filtered event list, and the hint."""
        with VerticalScroll(id="events-card"):
            yield Static(self._title(), classes="events-title", id="events-heading")
            with VerticalScroll(id="events-list"):
                yield from self._row_widgets()
            yield Static(
                "[ f cycle filter (all / errors / reports) · Esc to close ]",
                classes="events-hint",
            )

    def _title(self) -> str:
        """Return the heading line (event count + active filter)."""
        shown = len(filter_rows(self._rows, self.event_filter))
        return f"Events · {shown}/{len(self._rows)} · filter {self.event_filter}"

    def _row_widgets(self) -> list[Static]:
        """Build the Static widgets for the filtered rows (or an empty note).

        Returns:
            One :class:`~textual.widgets.Static` per filtered row, or a
            single "no events" note when the filter yields nothing.
        """
        filtered = filter_rows(self._rows, self.event_filter)
        if not filtered:
            return [Static("(no events)", classes="events-empty")]
        return [Static(_render_row(row), classes="events-row") for row in filtered]

    def action_cycle_filter(self) -> None:
        """Advance the ``f`` filter cycle (all → errors → reports → all)."""
        self.event_filter = next_filter(self.event_filter)

    def watch_event_filter(self) -> None:
        """Repaint the heading + list when the filter cycles."""
        if not self.is_mounted:
            return
        self.query_one("#events-heading", Static).update(self._title())
        listing = self.query_one("#events-list", VerticalScroll)
        listing.remove_children()
        listing.mount_all(self._row_widgets())

    def action_close(self) -> None:
        """Dismiss the events overlay (``Esc``)."""
        self.dismiss(None)


def open_events(app: App[None], rows: tuple[EventRow, ...]) -> bool:
    """Push the events overlay onto *app* (modal-cap-aware).

    Routes through the App's ``push_modal`` helper so the modal-stack
    depth cap is enforced in one place; falls back to a plain
    ``push_screen`` under a bare harness that lacks the cap helper.

    Args:
        app: The running App.
        rows: The pre-loaded most-recent events (newest first).

    Returns:
        ``True`` when the modal was pushed, ``False`` when the cap
        rejected it.
    """
    modal = EventsModal(rows)
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        return bool(push_modal(modal))
    app.push_screen(modal)
    return True


__all__ = [
    "EVENT_FILTERS",
    "EVENT_RING_SIZE",
    "EventFilter",
    "EventRow",
    "EventsModal",
    "filter_rows",
    "load_recent_events",
    "next_filter",
    "open_events",
]
