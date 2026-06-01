"""``AttentionFeed`` -- the Home overview band ranking what needs the operator.

A thin, focusable band that renders the ranked attention list (most-urgent
first) the pure :func:`~eawf.surfaces.tui.attention.build_attention_feed`
reducer produces. It is the Home mode's overview strip: it sits **above**
the resolved scope body (roadmap quadrant / portfolio table) so the
operator sees their open pauses, failed waves, open incidents, blocking
questions, and ready-to-claim waves at a glance without leaving the scope
view -- the scope axis (``w`` / ``r`` / ``u``) and the scope widgets stay
fully reachable underneath.

The band is driven by the host :class:`~eawf.surfaces.tui.app.EaApp` reactive
``state`` (seeds on mount, rebuilds on every daemon-pushed revision) and
the app's open-pause source (the same set the footer badge + global inbox
read), so the feed never disagrees with the badge. Only the needs_user
rows are directly actionable from the band: ``Enter`` on a pause row posts
:class:`AttentionFeed.PauseSelected`, which the host routes through the
shared ``open_needs_user_pause`` so the modal-cap + resume path is reused.
An empty feed renders the honest-empty
:data:`~eawf.surfaces.tui.attention.EMPTY_FEED_TEXT` note rather than a blank
strip.

The ranking + item derivation live in the pure reducer module so the order
is unit-testable without a Textual mount; this widget only renders the
ranked tuple and owns the row highlight + the select seam.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from eawf.surfaces.tui.attention import (
    EMPTY_FEED_TEXT,
    AttentionItem,
    AttentionKind,
    build_attention_feed,
    format_time_ago,
)

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.workflow.skills.bodies.user_question import UserQuestion
    from eawf.workflow.skills.needs_user import OpenPause

logger = logging.getLogger(__name__)

#: Per-kind row glyph (ASCII only -- no en-dash / non-ASCII per the source
#: hygiene rule). Draws the eye to the source category at a glance.
_KIND_GLYPH: dict[AttentionKind, str] = {
    AttentionKind.NEEDS_USER: "?",
    AttentionKind.FAILED_WAVE: "x",
    AttentionKind.INCIDENT: "!",
    AttentionKind.OPEN_QUESTION: "?",
    AttentionKind.READY_WAVE: ">",
}

#: Urgency values that render in the warn-attention tint (the rest stay
#: muted). Mirrors the needs_user inbox attention band.
_ATTENTION_URGENCIES: frozenset[str] = frozenset({"high", "urgent"})


def _render_row(item: AttentionItem, *, now: datetime) -> str:
    """Render one attention item as a single content-markup line.

    The urgency token is tinted to draw the eye to the more-immediate rows
    (``HIGH`` / ``URGENT`` in the ``$warn`` colour, the calmer tiers in
    ``$text-muted``); the kind glyph leads, the muted detail follows, and a
    muted relative ``time-ago`` trails right-aligned (blank when the row has
    no source clock). A portfolio (cross-repo) row prefixes its repo code so
    the operator sees which repo needs them. The colours resolve against the
    active theme at render time via Textual content markup.

    Args:
        item: The attention item to render.
        now: The reference instant the row's ``time-ago`` is measured from.

    Returns:
        A content-markup string for one :class:`~textual.widgets.Static`.
    """
    glyph = _KIND_GLYPH.get(item.kind, "-")
    cell = f"{item.urgency.value:<7}"
    tier = f"[$warn]{cell}[/]" if item.urgency.value in _ATTENTION_URGENCIES else cell
    repo = f"[$accent]{item.repo_tag}[/] " if item.repo_tag else ""
    ago = format_time_ago(item.occurred_at, now)
    ago_cell = f"  [$text-muted]{ago:>8}[/]" if ago else ""
    return f"{glyph} {tier} {repo}{item.title}  [$text-muted]{item.detail}[/]{ago_cell}"


class AttentionFeed(VerticalScroll):
    """Focusable Home overview band of the ranked attention feed.

    Public surface for a host screen:

    * bound to the app reactive ``state`` (seeds on mount, rebuilds on
      revision); standalone tests assign :attr:`state` directly.
    * ``Up`` / ``Down`` move the row highlight, ``Enter`` activates the
      highlighted row.
    * :class:`PauseSelected` -- posted on ``Enter`` over a needs_user row;
      carries the ``pause_urn`` + question so the host re-opens the modal.
    """

    can_focus = True

    DEFAULT_CSS: ClassVar[str] = """
    AttentionFeed {
        height: auto;
        max-height: 6;
    }
    AttentionFeed:focus {
        background: $boost;
    }
    AttentionFeed .attention-row {
        height: 1;
    }
    AttentionFeed .attention-row.-selected {
        text-style: bold reverse;
    }
    AttentionFeed .attention-empty {
        color: $text-muted;
        height: 1;
    }
    """

    #: ``Up`` / ``Down`` move the highlight, ``Enter`` activates, ``d``
    #: dismisses (acknowledges) the highlighted row for the session. Vim
    #: ``j`` / ``k`` ride the arrows per the operator keymap convention.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("k", "move(-1)", "up", show=False),
        Binding("j", "move(1)", "down", show=False),
        Binding("enter", "activate", "open", show=False),
        Binding("d", "dismiss", "dismiss", show=False),
    ]

    class PauseSelected(Message):
        """Posted when the operator activates a needs_user attention row.

        The host screen routes this through the shared
        ``open_needs_user_pause`` so the pause modal opens on the same
        cap-checked + resume path the auto-open and global inbox use.

        Attributes:
            pause_urn: The pause the activated row re-opens.
            question: The paused question to render in the modal.
        """

        def __init__(self, pause_urn: str, question: UserQuestion) -> None:
            self.pause_urn = pause_urn
            self.question = question
            super().__init__()

    #: Bound state, watched so a fresh revision rebuilds the feed rows.
    state: reactive[State | None] = reactive(None)

    #: Index of the highlighted row (``-1`` when the feed is empty).
    selected: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        """Construct the band.

        Args:
            **kwargs: Forwarded to :class:`~textual.containers.VerticalScroll`.
        """
        super().__init__(**kwargs)
        self._items: tuple[AttentionItem, ...] = ()
        # Reference instant for the row time-ago labels, captured in the
        # synchronous fold so the deferred async DOM build renders every row
        # against one clock (no skew from a mid-rebuild wall-clock advance).
        self._now_at: datetime = datetime.now(UTC)
        # Re-entrancy coalescing for the async DOM rebuild: ``_rebuilding``
        # is held while a rebuild's await-the-remove path runs, and
        # ``_rebuild_pending`` records a request that arrived mid-rebuild so
        # the loop re-runs once with the latest items (the mount races the
        # async ``remove_children`` otherwise -> DuplicateIds).
        self._rebuilding = False
        self._rebuild_pending = False

    def on_mount(self) -> None:
        """Seed from app state and watch for revisions, then build the feed."""
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
        """Rebuild the feed when the bound state changes."""
        self._rebuild()

    def _open_pauses(self) -> tuple[OpenPause, ...]:
        """Resolve the open pauses from the host app (empty under a bare harness).

        Reads the same cross-scope pause set the footer badge + global inbox
        use via the app's ``_all_open_pauses`` hook, so the band never
        disagrees with the badge. A bare test harness (no such hook) yields
        no pauses, leaving the feed driven by the state-derived signals only.

        Returns:
            The open pauses, or an empty tuple when the host exposes none.
        """
        resolver = getattr(self.app, "_all_open_pauses", None)
        if callable(resolver):
            return tuple(resolver())
        return ()

    def _dismissed(self) -> frozenset[str]:
        """Resolve the session-dismissed attention keys from the host app.

        Reads the App's ``attention_dismissed`` set so the band filters the
        same acknowledged rows the ``d`` keypress recorded. A bare harness
        (no such hook) yields the empty set, leaving every live row visible.

        Returns:
            The dismissed-key set, or an empty set under a bare harness.
        """
        resolver = getattr(self.app, "attention_dismissed", None)
        if callable(resolver):
            return frozenset(resolver())
        return frozenset()

    def _now(self) -> datetime:
        """Resolve the time-ago reference instant from the host app.

        Reads the App's ``_attention_now`` seam so the band's relative
        timestamps measure off one clock a deterministic harness can pin;
        falls back to the wall clock under a bare harness.

        Returns:
            The reference instant for this rebuild's ``time-ago`` labels.
        """
        resolver = getattr(self.app, "_attention_now", None)
        if callable(resolver):
            now = resolver()
            if isinstance(now, datetime):
                return now
        return datetime.now(UTC)

    def _is_portfolio_scope(self) -> bool:
        """Return whether the host app is on the user / portfolio scope."""
        return getattr(self.app, "_scope", None) == "user"

    def items(self) -> tuple[AttentionItem, ...]:
        """Return the current ranked attention items (read accessor).

        Pure read of the last-built feed so a host / test can assert the
        rendered order without scraping cells.

        Returns:
            The ranked items in display order (most-urgent first).
        """
        return self._items

    def rebuild(self) -> None:
        """Recompute + repaint the feed (public; e.g. after a dismiss).

        The App calls this on every mounted band when the session
        dismissed-set changes so an acknowledged row disappears immediately.
        """
        self._rebuild()

    def _rebuild(self) -> None:
        """Recompute the ranked items, then schedule the async DOM rebuild.

        The item fold is synchronous so :meth:`items` is always fresh for a
        caller / test the moment a rebuild is requested; the DOM mount/unmount
        is deferred to the coalesced async :meth:`_rebuild_dom` because
        ``remove_children`` resolves asynchronously and a back-to-back rebuild
        (mount + on_mount seed both fire) would otherwise race a mount ahead
        of the pending removal (``DuplicateIds``).

        On the user / portfolio scope the feed aggregates across the
        registered repos (the App's ``_portfolio_attention_feed`` hook, which
        resolves the explicit registry -- never a scan); every other scope
        folds the bound single-repo state plus the cross-scope pauses.
        """
        dismissed = self._dismissed()
        self._now_at = self._now()
        portfolio = getattr(self.app, "_portfolio_attention_feed", None)
        if self._is_portfolio_scope() and callable(portfolio):
            self._items = tuple(portfolio(dismissed))
        else:
            self._items = build_attention_feed(self.state, self._open_pauses(), dismissed=dismissed)
        if not 0 <= self.selected < len(self._items):
            self.selected = 0 if self._items else -1
        self._request_rebuild_dom()

    def _request_rebuild_dom(self) -> None:
        """Coalesce a DOM-rebuild request, running at most one at a time.

        A request arriving while a rebuild is in flight sets the pending
        flag so the running loop re-runs once more with the latest items
        rather than launching a second racing rebuild.
        """
        if self._rebuilding:
            self._rebuild_pending = True
            return
        self._rebuilding = True
        self.run_worker(self._rebuild_dom(), exclusive=False)

    async def _rebuild_dom(self) -> None:
        """Repaint the feed rows, awaiting the prior removal before mounting.

        Awaiting ``remove_children`` (a Textual ``AwaitComplete``) before the
        re-mount guarantees the previous ``attention-row-*`` ids are gone from
        the child ``NodeList`` first, so the mount cannot trip
        ``DuplicateIds``. Re-runs once when a request coalesced mid-rebuild.
        """
        try:
            while True:
                await self.remove_children()
                if not self._items:
                    await self.mount(Static(EMPTY_FEED_TEXT, classes="attention-empty"))
                else:
                    await self.mount_all(
                        [
                            Static(
                                _render_row(item, now=self._now_at),
                                classes="attention-row",
                                id=f"attention-row-{index}",
                            )
                            for index, item in enumerate(self._items)
                        ]
                    )
                    self._repaint_selection()
                if not self._rebuild_pending:
                    return
                self._rebuild_pending = False
        finally:
            self._rebuilding = False

    def watch_selected(self) -> None:
        """Repaint the row highlight when the selection moves."""
        if self.is_mounted and self._items:
            self._repaint_selection()

    def _repaint_selection(self) -> None:
        """Toggle the ``-selected`` class onto the highlighted row."""
        for index in range(len(self._items)):
            rows = self.query(f"#attention-row-{index}")
            if rows:
                rows.only_one(Static).set_class(index == self.selected, "-selected")

    def action_move(self, delta: int) -> None:
        """Move the highlight by *delta*, clamped to the row range.

        Args:
            delta: ``-1`` to move up, ``+1`` to move down.
        """
        if not self._items:
            return
        self.selected = max(0, min(self.selected + delta, len(self._items) - 1))

    def action_activate(self) -> None:
        """Activate the highlighted row.

        Posts :class:`PauseSelected` for a needs_user row (the only
        directly-actionable kind); other kinds are informational, so the
        activation is a logged no-op. A no-op when the feed is empty.
        """
        if not self._items or not 0 <= self.selected < len(self._items):
            return
        item = self._items[self.selected]
        if item.actionable and item.pause_urn is not None and item.question is not None:
            logger.info(f"attention_feed activate kind={item.kind.value!r} urn={item.pause_urn!r}")
            self.post_message(self.PauseSelected(item.pause_urn, item.question))
            return
        logger.info(f"attention_feed activate kind={item.kind.value!r} outcome=informational")

    def action_dismiss(self) -> None:
        """Acknowledge (hide) the highlighted row for this session.

        Records the highlighted row's
        :attr:`~eawf.surfaces.tui.attention.AttentionItem.dismiss_key` on the
        App's session dismissed-set (via ``dismiss_attention``), which
        rebuilds every mounted band so the row disappears at once and does
        not reappear that session. The general-kind analog of the inbox's
        ``defer`` (which is pause-specific). A no-op when the feed is empty or
        the host lacks the hook (a bare harness).
        """
        if not self._items or not 0 <= self.selected < len(self._items):
            return
        item = self._items[self.selected]
        dismiss = getattr(self.app, "dismiss_attention", None)
        if callable(dismiss):
            logger.info(f"attention_feed dismiss key={item.dismiss_key!r}")
            dismiss(item.dismiss_key)


__all__ = ["AttentionFeed"]
