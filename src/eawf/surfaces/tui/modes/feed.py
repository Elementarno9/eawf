"""``FeedModeScreen`` -- the live daemon event-feed pane (mode digit 5).

The Feed mode renders a live, newest-first view of the daemon
``event.subscribe`` push stream. Unlike the ``/events`` overlay (a modal
that reads the on-disk ``event.jsonl`` tail once on open), this is a
persistent pane that follows the stream: each envelope the daemon pushes
appears at the top of the scroll as it arrives.

Non-blocking delivery
---------------------
The pane never touches the streaming socket itself. The App's read-only
:class:`~eawf.surfaces.tui.state_binding.StateBinding` already consumes the
``event.subscribe`` stream on a worker thread (``asyncio.to_thread`` ->
blocking ``readline`` loop) and marshals each decoded envelope back to the
event loop via ``run_coroutine_threadsafe`` -> :meth:`EaApp._on_event`. So
the off-thread, non-UI-blocking read exists once, app-wide. This pane just
subscribes to that seam: on mount it registers as a Feed listener and
seeds from the App's live ring buffer (the envelopes that arrived before
the pane existed); thereafter the App fans each new envelope out to
:meth:`append_event`, which runs on the event-loop thread. On unmount it
unregisters, so the fan-out never targets a torn-down screen -- clean
teardown on a mode switch away or app exit, with no second daemon
subscription to leak.

Honest empty / degraded
-----------------------
Before any event arrives the pane shows an honest-empty notice. When the
daemon is unreachable (the binding is in its mtime-poll fallback,
``app.degraded``), the notice says so rather than implying a live feed is
flowing. The first arriving envelope replaces the notice with the row
list.

The row formatter (:func:`format_event_row`) is pure so the timestamp /
kind / summary layout is unit-testable without mounting Textual; the
screen is a thin scrollable view over it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from eawf.surfaces.tui.scopes import ScopeScreen

if TYPE_CHECKING:
    from eawf.kernel.store.envelope import Envelope

logger = logging.getLogger(__name__)


@runtime_checkable
class FeedListener(Protocol):
    """Structural contract for a pane fed by the App's live-event seam.

    The App (:class:`~eawf.surfaces.tui.app.EaApp`) consumes the daemon
    ``event.subscribe`` push stream once and fans each decoded envelope out
    to every registered listener via
    :meth:`~eawf.surfaces.tui.app.EaApp.register_feed_listener`. Both the
    :class:`FeedModeScreen` (the full live feed) and the agent-watch zoom
    (which filters the same stream to one session) satisfy this contract, so
    the App fan-out is typed against the structural protocol rather than one
    concrete screen -- a second consumer registers through the same seam
    without widening a concrete-class union.

    Attributes:
        is_mounted: Textual ``Widget.is_mounted`` -- the fan-out skips a
            listener whose pane has been torn down between the push being
            scheduled and this running.
    """

    is_mounted: bool

    def append_event(self, envelope: Envelope) -> None:
        """Receive one live event *envelope* from the App fan-out."""
        ...

    def refresh_empty_notice(self) -> None:
        """Refresh the honest-empty / honest-degraded notice on a state flip."""
        ...


#: Id of the scrollable feed list container.
FEED_LIST_ID: str = "feed-list"

#: Id of the honest-empty / honest-degraded notice shown before any event
#: has arrived. Removed once the first row lands; re-shown if the feed is
#: ever emptied (it is not, in practice -- the buffer only grows).
FEED_EMPTY_ID: str = "feed-empty"

#: CSS class on each rendered event row.
FEED_ROW_CLASS: str = "feed-row"

#: Notice text when the feed is live but no event has arrived yet.
FEED_EMPTY_LIVE: str = "Live feed -- waiting for events..."

#: Notice text when the daemon is unreachable (binding in poll fallback),
#: so the operator knows the feed is not flowing rather than merely quiet.
FEED_EMPTY_DEGRADED: str = (
    "Daemon unreachable -- live feed paused; showing nothing until it returns"
)

#: Width the kind column is padded to so the trailing summary lines up
#: across rows. The longest ``StoreKind`` value is ``domain_specialist_report``
#: (24); pad to that so even the widest kind keeps one trailing space.
_KIND_WIDTH: int = 24

#: Footer hints for the Feed pane (full key names; arrows primary per the
#: keymap convention). The scroll affordances plus the always-live chassis
#: keys (mode digits, palette, help, quit).
_FEED_HINTS: tuple[str, ...] = (
    "up/down scroll",
    "1-6 mode",
    "w/r/u scope",
    "/ palette",
    "? help",
    "q quit",
)


def format_event_row(envelope: Envelope) -> str:
    """Render one event *envelope* as a single feed line.

    Layout is ``<HH:MM:SS> <kind> <summary>`` -- the wall-clock time of the
    event (UTC, second precision), the store kind padded to
    :data:`_KIND_WIDTH`, then the envelope summary. The summary is already
    bounded at 500 chars by the :class:`~eawf.kernel.store.envelope.Envelope`
    model, so no extra truncation is needed; Textual soft-wraps a long row.

    Args:
        envelope: The event envelope to render.

    Returns:
        A plain (markup-free) single-line string for one
        :class:`~textual.widgets.Static`.
    """
    stamp = envelope.created_at.strftime("%H:%M:%S")
    kind = envelope.kind.value
    summary = envelope.summary
    if not summary:
        # No summary to align to -- drop the kind-column pad so the row
        # carries no trailing whitespace.
        return f"{stamp}  {kind}"
    return f"{stamp}  {kind:<{_KIND_WIDTH}}  {summary}"


class FeedModeScreen(ScopeScreen):
    """Persistent pane showing the live daemon event feed, newest-first.

    Subscribes to the App's live-event seam (the App fans
    ``event.subscribe`` pushes out via :meth:`EaApp.register_feed_listener`)
    rather than opening its own daemon subscription, so the off-thread,
    non-UI-blocking stream read stays single-sourced in the read-only
    binding. Seeds from the App's ring buffer on mount, appends live
    thereafter, and tears the subscription (the listener registration)
    down cleanly on unmount.
    """

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _FEED_HINTS

    def compose_body(self) -> ComposeResult:
        """Yield the scrollable feed body (honest-empty until events arrive).

        The list starts with a single honest-empty / honest-degraded
        notice; :meth:`on_mount` seeds any already-buffered envelopes and
        :meth:`append_event` prepends live ones, removing the notice once
        the first row lands.
        """
        with Vertical(id="body", classes="feed-body"), VerticalScroll(id=FEED_LIST_ID):
            yield Static(self._empty_notice(), id=FEED_EMPTY_ID, classes="feed-empty")

    def on_mount(self) -> None:
        """Register as a live-feed listener and seed from the App buffer.

        Calls the base chassis mount (footer hints) first, then registers
        with the App so subsequent pushes fan out to :meth:`append_event`,
        and seeds the scroll with the envelopes the App buffered before the
        pane mounted (oldest-first buffer -> each prepended, so the most
        recent buffered event ends on top). The seed mounts rows directly
        (``_render_event``) rather than via :meth:`append_event` because
        ``is_mounted`` is still ``False`` while ``on_mount`` runs -- the
        compose tree (the scroll container) already exists, so the rows
        mount fine. A bare harness without the App fan-out hooks degrades to
        an empty live pane.
        """
        super().on_mount()
        register = getattr(self.app, "register_feed_listener", None)
        if callable(register):
            register(self)
        buffer = getattr(self.app, "live_event_buffer", ())
        for envelope in buffer:
            self._render_event(envelope)

    def on_unmount(self) -> None:
        """Unregister from the App fan-out so a torn-down pane gets no pushes."""
        unregister = getattr(self.app, "unregister_feed_listener", None)
        if callable(unregister):
            unregister(self)

    def append_event(self, envelope: Envelope) -> None:
        """Prepend one live *envelope* to the top of the feed (newest-first).

        The App-fan-out entry point: a no-op if the pane has been unmounted
        between the App scheduling the push and this running, so a
        late-arriving push never targets a torn-down pane. Delegates the row
        mount to :meth:`_render_event`.

        Args:
            envelope: The live event envelope to render at the top.
        """
        if not self.is_mounted:
            return
        self._render_event(envelope)

    def _render_event(self, envelope: Envelope) -> None:
        """Mount one event row at the top of the scroll (newest-first).

        Removes the honest-empty notice on the first row, then mounts the
        new row before the existing ones so the newest event stays on top
        without scrolling. Shared by the on-mount seed loop and the live
        :meth:`append_event` fan-out so the render path is single-sourced.

        Args:
            envelope: The event envelope to render at the top.
        """
        listing = self.query_one(f"#{FEED_LIST_ID}", VerticalScroll)
        empty = listing.query(f"#{FEED_EMPTY_ID}")
        if empty:
            empty.first().remove()
        row = Static(format_event_row(envelope), classes=FEED_ROW_CLASS)
        listing.mount(row, before=0)
        logger.debug(f"_render_event id={envelope.id!r} kind={envelope.kind.value!r}")

    def refresh_empty_notice(self) -> None:
        """Update the honest-empty notice text to track the degraded flag.

        Called by the App when it flips between live and degraded so a pane
        showing the empty notice swaps between the live-waiting and
        daemon-unreachable wording. A no-op once events have arrived (the
        notice is gone) or before mount.
        """
        if not self.is_mounted:
            return
        notice = self.query(f"#{FEED_EMPTY_ID}")
        if notice:
            notice.first(Static).update(self._empty_notice())

    def _empty_notice(self) -> str:
        """Return the honest-empty notice text for the current daemon state.

        Returns:
            :data:`FEED_EMPTY_DEGRADED` when the App reports degraded
            (daemon unreachable), else :data:`FEED_EMPTY_LIVE`.
        """
        return FEED_EMPTY_DEGRADED if getattr(self.app, "degraded", False) else FEED_EMPTY_LIVE


__all__ = [
    "FEED_EMPTY_DEGRADED",
    "FEED_EMPTY_ID",
    "FEED_EMPTY_LIVE",
    "FEED_LIST_ID",
    "FEED_ROW_CLASS",
    "FeedListener",
    "FeedModeScreen",
    "format_event_row",
]
