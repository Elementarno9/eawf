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

Cosmic-terminal reskin
----------------------
Each event row leads with a two-cell lifecycle-sigil column -- the
shared SHAPE glyph (:func:`~eawf.surfaces.tui.widgets.sigils.glyph`) tinted
by its Wong status hue (:func:`~eawf.surfaces.tui.widgets.sigils.tint`) --
so the operator reads the lifecycle of each event (claimed / running /
closed / failed) at a glance without parsing the summary prose. The
sigil is followed by a fixed-width wall-clock column and the
kind-padded summary, so the columns line up across rows. The sigil
column repaints on a unicode <-> ASCII render-mode flip, like every
other reskinned pane.

The row formatter (:func:`format_event_row`) is pure (markup-free,
layout only) so the sigil / timestamp / kind / summary column layout is
unit-testable without mounting Textual; the tinted content markup is
composed at mount time by :func:`format_event_markup`. The screen is a
thin scrollable view over both.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

import orjson
from pydantic import ValidationError
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.empty_state import (
    HONEST_EMPTY_CSS,
    SEAL_HERO_ID,
    render_empty_state,
    seal_empty_hero,
    seal_hero_css,
)
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph, tint

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
FEED_EMPTY_LIVE: str = "live feed waiting for events"

#: Notice text when the daemon is unreachable (binding in poll fallback),
#: so the operator knows the feed is not flowing rather than merely quiet.
#: Voiced to match the global degraded banner's calm "daemon unreachable,
#: reconnecting" lead, then states the pane-specific consequence.
FEED_EMPTY_DEGRADED: str = "daemon unreachable · live feed paused until it returns"

#: Id of the persistent connection-state badge at the top of the feed. It is
#: the connection indicator distinguishing "daemon disconnected" (the daemon
#: is unreachable, so the rows below are the on-disk log tail, not a live
#: stream) from "connected" (the badge is hidden and the rows are live pushes).
FEED_STATUS_ID: str = "feed-status"

#: CSS class that hides the connection badge. Applied while the daemon is
#: reachable so a healthy live feed carries no persistent chrome; removed the
#: moment the binding drops into its poll fallback (``app.degraded``).
FEED_STATUS_HIDDEN_CLASS: str = "feed-status--hidden"

#: The connection-badge text shown when the daemon is unreachable. Names the
#: consequence -- the rows below are seeded from the on-disk event store, not a
#: live push stream -- so the operator reads the feed as a recent-history view
#: rather than a stalled live one.
FEED_STATUS_DISCONNECTED: str = "daemon disconnected · showing recent events from the log"

#: Cap on the on-disk event-store tail the feed seeds when the App's live ring
#: is empty (daemonless / pre-push). Mirrors the ``/events`` overlay ring size
#: so the two on-disk event surfaces show the same depth of recent history.
FEED_SEED_LIMIT: int = 50

#: Width the kind column is padded to so the trailing summary lines up
#: across rows. The longest ``StoreKind`` value is ``domain_specialist_report``
#: (24); pad to that so even the widest kind keeps one trailing space.
_KIND_WIDTH: int = 24

#: Width the wall-clock timestamp column is laid out to. ``HH:MM:SS`` is
#: eight cells; pinning the width keeps the kind column left-aligned across
#: rows even if a future timestamp form changes length.
_STAMP_WIDTH: int = 8

#: The render-mode label threaded into the sigil helpers when the host App
#: exposes no ``render_mode`` (a bare standalone test harness). The unicode
#: column is the default content surface; ``"ascii"`` only when the App
#: resolves it. Mirrors the attention-feed band's default.
_DEFAULT_RENDER_MODE: str = "unicode"

#: Lifecycle sigil keyed off a lowercased event-status / event-kind token.
#: The lifecycle of an event is read first off the payload ``status`` word
#: and then off the ``event_kind`` / ``event_type`` tag (see
#: :func:`event_sigil`): a claim transition wears the half-filled CLAIMED
#: ring, a completed transition (closed / activated-then-done / ok) the
#: filled CLOSED circle, a failure / drift / alarm the FAILED cross, and an
#: in-flight transition the RUNNING diamond. The SHAPE comes from the shared
#: :mod:`~eawf.surfaces.tui.widgets.sigils` home; no glyph is invented here.
_STATUS_SIGIL: dict[str, Sigil] = {
    "pending": Sigil.PENDING,
    "claimed": Sigil.CLAIMED,
    "running": Sigil.RUNNING,
    "in_progress": Sigil.RUNNING,
    "closed": Sigil.CLOSED,
    "ok": Sigil.CLOSED,
    "failed": Sigil.FAILED,
    "error": Sigil.FAILED,
}

#: Substrings scanned (in order) against a lowercased ``event_kind`` /
#: ``event_type`` tag when the payload ``status`` word maps onto no sigil.
#: A ``claimed`` tag reads as the CLAIMED ring, a ``fail`` / ``error`` /
#: ``drift`` / ``alarm`` / ``unavailable`` / ``oom`` tag as the FAILED
#: cross, a ``closed`` tag as the CLOSED circle; anything else falls through
#: to the in-flight RUNNING default (see :func:`event_sigil`).
_KIND_SIGIL_SUBSTRINGS: tuple[tuple[str, Sigil], ...] = (
    ("claimed", Sigil.CLAIMED),
    ("fail", Sigil.FAILED),
    ("error", Sigil.FAILED),
    ("drift", Sigil.FAILED),
    ("alarm", Sigil.FAILED),
    ("unavailable", Sigil.FAILED),
    ("oom", Sigil.FAILED),
    ("dropped", Sigil.FAILED),
    ("closed", Sigil.CLOSED),
)

#: The lifecycle sigil an event with no recognisable status / kind token
#: wears: a generic event represents in-flight activity, so it reads as the
#: RUNNING diamond rather than a terminal mark.
_DEFAULT_SIGIL: Sigil = Sigil.RUNNING

#: Footer hints for the Feed pane (arrows primary per the keymap convention).
#: The scroll affordances plus the always-live chassis keys (palette, help,
#: quit). The mode digits are surfaced by the always-visible mode row, not
#: duplicated here. Every label is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
#: tokens stay pinned to the canonical vocabulary.
_FEED_HINTS: tuple[str, ...] = (
    render_hint_label("↑↓", "select"),
    render_hint_label("w/r/u", "scope"),
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
)


def event_sigil(envelope: Envelope) -> Sigil:
    """Return the lifecycle :class:`Sigil` naming *envelope*'s event state.

    The lifecycle is read first off the payload ``status`` word (via
    :data:`_STATUS_SIGIL`) and then, when that maps onto nothing, off the
    ``event_kind`` / ``event_type`` tag by substring scan (via
    :data:`_KIND_SIGIL_SUBSTRINGS`): a claim transition wears the CLAIMED
    ring, a completed / ok transition the CLOSED circle, a failure / drift /
    alarm the FAILED cross. An event with no recognisable token defaults to
    the in-flight :data:`_DEFAULT_SIGIL` (RUNNING) -- a generic event reads
    as live activity, not a terminal mark.

    Args:
        envelope: The event envelope whose lifecycle sigil to resolve.

    Returns:
        The lifecycle :class:`Sigil` the row's leading mark renders.
    """
    payload = envelope.payload
    status = str(payload.get("status", "")).strip().lower()
    if status in _STATUS_SIGIL:
        return _STATUS_SIGIL[status]
    tag = str(payload.get("event_kind") or payload.get("event_type") or "").lower()
    for substring, sigil in _KIND_SIGIL_SUBSTRINGS:
        if substring in tag:
            return sigil
    return _DEFAULT_SIGIL


def format_event_row(envelope: Envelope) -> str:
    """Render one event *envelope* as a single feed line (markup-free).

    Layout is ``<sigil> <HH:MM:SS> <kind> <summary>``: a two-cell leading
    lifecycle-sigil column (the sigil glyph plus one trailing space), the
    wall-clock time of the event (UTC, second precision) laid out to a
    fixed :data:`_STAMP_WIDTH`, the store kind padded to :data:`_KIND_WIDTH`,
    then the envelope summary. The sigil is resolved in the unicode column
    (the markup form swaps to ASCII when the App's render mode is ``ascii``);
    the summary is already bounded at 500 chars by the
    :class:`~eawf.kernel.store.envelope.Envelope` model, so no extra
    truncation is needed and Textual soft-wraps a long row.

    Args:
        envelope: The event envelope to render.

    Returns:
        A plain (markup-free) single-line string for one
        :class:`~textual.widgets.Static`.
    """
    sigil = glyph(event_sigil(envelope), mode=_DEFAULT_RENDER_MODE)
    stamp = envelope.created_at.strftime("%H:%M:%S")
    kind = envelope.kind.value
    summary = envelope.summary
    if not summary:
        # No summary to align to -- drop the kind-column pad so the row
        # carries no trailing whitespace after the sigil + time + kind head.
        return f"{sigil} {stamp:<{_STAMP_WIDTH}}  {kind}"
    return f"{sigil} {stamp:<{_STAMP_WIDTH}}  {kind:<{_KIND_WIDTH}}  {summary}"


def format_event_markup(envelope: Envelope, *, mode: str) -> str:
    """Return the tinted content markup for one feed row in render *mode*.

    Composes the SHAPE (:func:`~eawf.surfaces.tui.widgets.sigils.glyph`) and
    the COLOUR (:func:`~eawf.surfaces.tui.widgets.sigils.tint`) of the event's
    lifecycle sigil so the leading two-cell column reads as a tinted
    lifecycle mark, then escapes the timestamp / kind / summary tail so an
    arbitrary summary (which may carry literal ``[`` brackets) renders
    verbatim through Textual's content-markup parser rather than being
    swallowed as a style tag. The column layout mirrors
    :func:`format_event_row` (sigil + fixed-width time + padded kind +
    summary).

    Args:
        envelope: The event envelope to render.
        mode: The App's resolved render-mode label -- ``"ascii"`` selects the
            ASCII sigil column, any other value the unicode column.

    Returns:
        A content-markup string for one :class:`~textual.widgets.Static`.
    """
    sigil = event_sigil(envelope)
    mark = escape_markup(glyph(sigil, mode=mode))
    hex_tint = tint(sigil)
    mark_cell = f"[{hex_tint}]{mark}[/]" if hex_tint else mark
    stamp = envelope.created_at.strftime("%H:%M:%S")
    kind = envelope.kind.value
    summary = envelope.summary
    if not summary:
        tail = f"{stamp:<{_STAMP_WIDTH}}  {kind}"
    else:
        tail = f"{stamp:<{_STAMP_WIDTH}}  {kind:<{_KIND_WIDTH}}  {summary}"
    return f"{mark_cell} {escape_markup(tail)}"


def load_recent_envelopes(
    event_path: Path | None, limit: int = FEED_SEED_LIMIT
) -> tuple[Envelope, ...]:
    """Read the tail of the event store as validated envelopes, oldest-first.

    The daemonless seed source: the App live ring is fed only by daemon
    pushes, so with no reachable daemon it stays empty. This reads the same
    on-disk ``event.jsonl`` store the ``/events`` overlay reads and validates
    each JSONL line back into an :class:`~eawf.kernel.store.envelope.Envelope`,
    so the feed can render the real recent activity rather than an
    unconditional empty notice. The read is total: a missing / unreadable
    file yields an empty tuple, and a malformed line is skipped rather than
    aborting the read, so a fresh or partially written store degrades to
    "no rows" instead of crashing. Never mutates the file.

    Args:
        event_path: Path to ``<state_dir>/store/event.jsonl``, or ``None``
            when no scope state is resolved.
        limit: The most recent *limit* rows to keep.

    Returns:
        The most recent envelopes in arrival order (oldest first), so the
        feed's newest-first prepend seed lands the freshest event on top.
    """
    if event_path is None or not event_path.is_file():
        return ()
    try:
        raw = event_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"load_recent_envelopes path={event_path!s} unreadable cause={exc!r}")
        return ()
    envelopes: list[Envelope] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            envelopes.append(Envelope.model_validate(orjson.loads(stripped)))
        except (orjson.JSONDecodeError, ValidationError, ValueError) as exc:
            logger.debug(f"load_recent_envelopes skip cause={exc!r}")
            continue
    # The store is append-ordered (oldest first, newest last), so the tail is
    # the most recent `limit`; keep them oldest-first for the prepend seed.
    return tuple(envelopes[-limit:])


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

    #: Centers the honest-empty hero in the feed list pane. Overrides the
    #: top-left single-line ``.feed-empty`` rule in ``theme.tcss`` with the
    #: shared :data:`~eawf.surfaces.tui.widgets.empty_state.HONEST_EMPTY_CSS`
    #: snippet (content-align center middle, no border) so the live-waiting /
    #: degraded surface reads as the calm centered hero -- the same hero the
    #: research board + sandbox timeline already render.
    DEFAULT_CSS: ClassVar[str] = f"""
    FeedModeScreen #{FEED_EMPTY_ID} {{ {HONEST_EMPTY_CSS} }}
    FeedModeScreen #{FEED_STATUS_ID} {{ height: 1; padding: 0 1; }}
    FeedModeScreen #{FEED_STATUS_ID}.{FEED_STATUS_HIDDEN_CLASS} {{ display: none; }}
    {seal_hero_css("FeedModeScreen")}
    """

    def compose_body(self) -> ComposeResult:
        """Yield the connection badge + scrollable feed body.

        The body leads with the connection-state badge (hidden while the
        daemon is reachable, shown as the "daemon disconnected" indicator
        once the binding drops into its poll fallback), then the scrollable
        list. The list starts with a single honest-empty / honest-degraded
        notice; :meth:`on_mount` seeds any already-buffered envelopes (and,
        when the live ring is empty, the on-disk event-store tail) and
        :meth:`append_event` prepends live ones, removing the notice once the
        first row lands.
        """
        mode = self._render_mode()
        with Vertical(id="body", classes="feed-body"):
            # The connection badge starts hidden (the healthy live feed carries
            # no persistent chrome); :meth:`_sync_connection_badge` reveals it
            # with the disconnected wording when ``app.degraded`` flips.
            yield Static("", id=FEED_STATUS_ID, classes=f"feed-status {FEED_STATUS_HIDDEN_CLASS}")
            with VerticalScroll(id=FEED_LIST_ID):
                # The honest-empty hero carries NO ``feed-empty`` class: the
                # ``theme.tcss`` ``.feed-empty`` rule pins a top-left single line,
                # and an app-tier rule outranks this screen's ``DEFAULT_CSS`` even
                # at lower specificity, so the class would shadow the centered-hero
                # ``#feed-empty`` id rule. The id alone styles + identifies it.
                #
                # Unicode path leads the honest-empty feed with the centered
                # ASCII-art Seal (the research-board brand-mark pattern); the body
                # drops its glyph sigil so the art is the single brand mark. ASCII
                # path keeps the small brand glyph (the half-block art needs
                # block-glyph coverage).
                body = Static(self._empty_hero(with_sigil=mode != "unicode"), id=FEED_EMPTY_ID)
                if mode == "unicode":
                    yield seal_empty_hero(body)
                else:
                    yield body

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
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        self._sync_connection_badge()
        buffer = getattr(self.app, "live_event_buffer", ())
        for envelope in buffer:
            self._render_event(envelope)
        if not buffer:
            # Daemonless / pre-push: the App ring is fed only by daemon pushes,
            # so with no reachable daemon it stays empty. Seed the newest events
            # off the on-disk event store tail (the same store the /events
            # overlay reads) so the feed shows real recent activity instead of
            # an unconditional empty notice. Only when the ring is empty, so a
            # live push already in the ring is never double-rendered from disk.
            for envelope in self._file_tail_envelopes():
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
        # Remove the seal-hero wrapper (unicode path) when present so the art
        # seal does not linger above the first feed row; else remove the bare
        # body notice (ascii path).
        hero = listing.query(f"#{SEAL_HERO_ID}")
        if hero:
            hero.first().remove()
        else:
            empty = listing.query(f"#{FEED_EMPTY_ID}")
            if empty:
                empty.first().remove()
        mode = self._render_mode()
        row = Static(format_event_markup(envelope, mode=mode), classes=FEED_ROW_CLASS)
        # Stash the source envelope on the row so a render-mode flip can
        # repaint the tinted sigil column in place without re-fetching the
        # App buffer (the buffer may have rolled past this row by then).
        row._feed_envelope = envelope  # type: ignore[attr-defined]
        listing.mount(row, before=0)
        logger.debug(f"_render_event id={envelope.id!r} kind={envelope.kind.value!r}")

    def _render_mode(self) -> str:
        """Resolve the active render-mode label from the host app.

        Threads :attr:`eawf.surfaces.tui.app.EaApp.render_mode` into the
        sigil helpers so an ``ascii`` flip swaps every row's sigil to its
        ASCII column; falls back to :data:`_DEFAULT_RENDER_MODE` (the unicode
        column) under a bare test harness whose host App carries no
        ``render_mode`` attribute.

        Returns:
            The render-mode label (``"ascii"`` or a unicode label).
        """
        return getattr(self.app, "render_mode", _DEFAULT_RENDER_MODE)

    def _on_render_mode(self, _mode: object) -> None:
        """Repaint every mounted event row when the App's render mode flips.

        Re-renders each row's tinted content markup from its stashed source
        envelope so a unicode <-> ASCII flip swaps the sigil column in place.
        A no-op before mount (the watcher is wired in :meth:`on_mount`).
        """
        if not self.is_mounted:
            return
        mode = self._render_mode()
        for row in self.query(f".{FEED_ROW_CLASS}").results(Static):
            envelope = getattr(row, "_feed_envelope", None)
            if envelope is not None:
                row.update(format_event_markup(envelope, mode=mode))

    def refresh_empty_notice(self) -> None:
        """Sync the connection badge + honest-empty notice on a degraded flip.

        Called by the App when it flips between live and degraded. Reveals /
        hides the connection badge (the disconnected indicator) and, for a
        pane still showing the empty notice, swaps that notice between the
        live-waiting and daemon-unreachable wording. A no-op before mount.
        """
        if not self.is_mounted:
            return
        self._sync_connection_badge()
        notice = self.query(f"#{FEED_EMPTY_ID}")
        if notice:
            # Preserve with_sigil=False when the ASCII-art Seal leads the hero
            # so a live/degraded flip never re-adds the glyph beside the art.
            seal_mounted = bool(self.query(f"#{SEAL_HERO_ID}"))
            notice.first(Static).update(self._empty_hero(with_sigil=not seal_mounted))

    def _file_tail_envelopes(self) -> tuple[Envelope, ...]:
        """Return the on-disk event-store tail for the daemonless seed.

        Resolves the host App's bound ``state.json`` path to its sibling
        event store (:func:`~eawf.kernel.store.paths.store_path`) and reads the
        most recent envelopes (:func:`load_recent_envelopes`). Empty under a
        bare harness whose host App exposes no ``_state_path`` or when no store
        exists yet.

        Returns:
            The recent event envelopes, oldest-first (empty when none readable).
        """
        state_path = getattr(self.app, "_state_path", None)
        if not isinstance(state_path, Path):
            return ()
        return load_recent_envelopes(store_path(state_path, StoreKind.EVENT))

    def _sync_connection_badge(self) -> None:
        """Reveal / hide the connection badge to track the daemon-reachable flag.

        While the daemon is reachable the badge stays hidden (a healthy live
        feed carries no persistent chrome); once the App reports degraded the
        badge is revealed with the :data:`FEED_STATUS_DISCONNECTED` indicator
        so the operator reads the rows below as the on-disk log tail rather
        than a live stream. A no-op before the badge is mounted.
        """
        badge = self.query(f"#{FEED_STATUS_ID}")
        if not badge:
            return
        degraded = bool(getattr(self.app, "degraded", False))
        widget = badge.first(Static)
        widget.set_class(not degraded, FEED_STATUS_HIDDEN_CLASS)
        if degraded:
            mark = escape_markup(chrome("attention", mode=self._render_mode()))
            widget.update(f"[$warn]{mark} {FEED_STATUS_DISCONNECTED}[/]")

    def _empty_notice(self) -> str:
        """Return the honest-empty notice headline for the current daemon state.

        Returns:
            :data:`FEED_EMPTY_DEGRADED` when the App reports degraded
            (daemon unreachable), else :data:`FEED_EMPTY_LIVE`.
        """
        return FEED_EMPTY_DEGRADED if getattr(self.app, "degraded", False) else FEED_EMPTY_LIVE

    def _empty_hero(self, *, with_sigil: bool = True) -> str:
        """Return the centered honest-empty hero body for the feed list.

        Routes the current :meth:`_empty_notice` headline (live-waiting or
        daemon-unreachable) through the shared
        :func:`~eawf.surfaces.tui.widgets.empty_state.render_empty_state` hero
        so the pre-event surface reads as the calm centered hero (a muted
        brand sigil over a ``$muted`` headline) rather than a top-left
        one-liner. The wording is calm (a passive waiting state, not an
        alarm), so the headline wears ``$muted``; the feed has no
        operator-facing action to take while it waits, so the hero carries no
        action chips.

        Args:
            with_sigil: When ``False`` the leading brand glyph is dropped -- the
                unicode path leads the hero with the ASCII-art Seal instead, so
                the glyph would be a redundant second brand mark.
        """
        return render_empty_state(
            self._empty_notice(),
            mode=self._render_mode(),
            headline_tint="$muted",
            sigil=with_sigil,
        )


__all__ = [
    "FEED_EMPTY_DEGRADED",
    "FEED_EMPTY_ID",
    "FEED_EMPTY_LIVE",
    "FEED_LIST_ID",
    "FEED_ROW_CLASS",
    "FEED_SEED_LIMIT",
    "FEED_STATUS_DISCONNECTED",
    "FEED_STATUS_HIDDEN_CLASS",
    "FEED_STATUS_ID",
    "FeedListener",
    "FeedModeScreen",
    "event_sigil",
    "format_event_markup",
    "format_event_row",
    "load_recent_envelopes",
]
