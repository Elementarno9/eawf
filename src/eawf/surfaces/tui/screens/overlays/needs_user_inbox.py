"""``NeedsUserInbox`` — the global needs_user pause inbox overlay.

The ``i`` keypress (and the ``/inbox`` palette verb) opens a single
list of every open ``needs_user`` pause across all scopes, ranked by
:class:`~eawf.kernel.state.enums.Urgency` (most-immediate first). Each
row shows the pause's scope / session and its question summary plus the
urgency token. ``Enter`` opens the highlighted pause's
:class:`~eawf.surfaces.tui.screens.overlays.needs_user.NeedsUserModal`
(reusing :func:`~eawf.surfaces.tui.screens.overlays.needs_user.open_needs_user`);
``Esc`` closes. When no pause is open the overlay renders an
honest-empty note rather than a blank card.

**Data source.** The pauses are the same
:class:`~eawf.workflow.skills.needs_user.OpenPause` records the
``needs_user_pause`` auto-open path reads — resolved by the host from
:func:`~eawf.workflow.skills.needs_user.list_open_pauses` (with
``scope_id=None`` so the inbox spans every scope) and ranked by
:func:`rank_pauses_by_urgency`. The overlay is built with a pre-ranked
tuple so it never reaches into the store itself.

The ranking helper is pure so the urgency order is unit-testable
without mounting Textual; the modal is a thin scrollable highlight-list
over the ranked rows, mirroring the
:class:`~eawf.surfaces.tui.screens.overlays.pr_list.PrListModal` pattern.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.kernel.state.enums import Urgency
from eawf.surfaces.tui.attention import AttentionKind, format_time_ago
from eawf.surfaces.tui.widgets.sigils import chrome

if TYPE_CHECKING:
    from textual.app import App

    from eawf.workflow.skills.needs_user import OpenPause

logger = logging.getLogger(__name__)

#: Render-mode label threaded into the sigil helpers when the host App
#: exposes no ``render_mode`` (a bare standalone harness): the unicode
#: column is the default surface, ``"ascii"`` only when the App resolves it.
_DEFAULT_RENDER_MODE: str = "unicode"


def _pause_dismiss_key(pause: OpenPause) -> str:
    """Return the session dismiss key for a pause row.

    Mirrors the band's needs_user
    :attr:`~eawf.surfaces.tui.attention.AttentionItem.dismiss_key` (an untagged
    ``":needs_user:<pause_urn>"``) so a pause dismissed from the inbox stays
    dismissed in the Home band, and vice versa -- one acknowledge set, two
    surfaces.

    Args:
        pause: The pause whose dismiss key to derive.

    Returns:
        The stable per-pause dismiss key.
    """
    return f":{AttentionKind.NEEDS_USER.value}:{pause.pause_urn}"


#: Honest-empty placeholder shown when no pause is open across any scope.
EMPTY_INBOX_TEXT: str = "no pending pauses"


def _urgency_rank(urgency: Urgency) -> int:
    """Return a descending sort key for *urgency* (most-immediate first).

    The :class:`~eawf.kernel.state.enums.Urgency` members are declared
    from most-deferrable (``LOW``) to most-immediate (``URGENT``), so the
    declaration index already ranks them ascending. Negating it yields a
    key that sorts ``URGENT`` before ``HIGH`` before ``NORMAL`` before
    ``LOW`` under a plain ascending ``sorted``.

    Args:
        urgency: The pause urgency to rank.

    Returns:
        A negative integer; lower (more negative) is more urgent.
    """
    return -list(Urgency).index(urgency)


def rank_pauses_by_urgency(pauses: tuple[OpenPause, ...]) -> tuple[OpenPause, ...]:
    """Rank *pauses* by urgency, most-immediate first (stable within a tier).

    Sorts by descending :class:`~eawf.kernel.state.enums.Urgency` so an
    ``URGENT`` pause precedes a ``HIGH`` one, and so on down to ``LOW``.
    The sort is stable, so pauses sharing a tier keep their input order —
    the inbox seeds them in append order (oldest first per
    :func:`~eawf.workflow.skills.needs_user.list_open_pauses`), so within a
    tier the oldest pause stays on top.

    Args:
        pauses: The open pauses to rank (any order).

    Returns:
        The pauses ordered most-urgent first.
    """
    return tuple(sorted(pauses, key=lambda pause: _urgency_rank(pause.urgency)))


def _row_label(pause: OpenPause) -> str:
    """Return the scope / session label prefix for a pause row.

    Prefers the originating session URN's trailing segment (the operator
    recognises ``SES-...`` over the full scope URN), falling back to the
    scope id when the pause carries no session.

    Args:
        pause: The pause to label.

    Returns:
        A short ``scope`` / ``session`` identifier for the row.
    """
    if pause.session:
        return pause.session.rsplit(":", 1)[-1]
    return pause.scope_id


def _render_row(pause: OpenPause, *, now: datetime, mode: str) -> str:
    """Render one pause as a single content-markup line.

    The row leads with the shared ``attention`` chrome sigil (the triangle,
    or the ASCII ``!`` fallback) in the ``$warn`` attention colour so each
    open pause is marked at a glance through the single
    :mod:`~eawf.surfaces.tui.widgets.sigils` home. The urgency token is
    tinted to draw the eye to the more-immediate rows: ``URGENT`` / ``HIGH``
    render in ``$warn``, the calmer tiers in ``$text-muted``. The pause's
    scope / session label names it in the green ``$accent`` and a muted
    relative ``time-ago`` of the pause's raise time trails right-aligned
    (blank when the pause has no recorded raise timestamp). The colours
    resolve against the active theme at render time via Textual content
    markup.

    Args:
        pause: The pause row to render.
        now: The reference instant the ``time-ago`` is measured from.
        mode: The App's resolved render-mode label (``"ascii"`` or unicode),
            selecting the attention sigil's glyph column.

    Returns:
        A content-markup string for one :class:`~textual.widgets.Static`.
    """
    sigil = chrome("attention", mode=mode)
    cell = f"{pause.urgency.value:<8}"
    tier = f"[$warn]{cell}[/]" if _is_attention(pause.urgency) else cell
    label = _row_label(pause)
    ago = format_time_ago(pause.occurred_at, now)
    ago_cell = f"  [$text-muted]{ago:>8}[/]" if ago else ""
    return f"[$warn]{sigil}[/] {tier} [$accent]{label}[/]  {pause.question.question}{ago_cell}"


def _is_attention(urgency: Urgency) -> bool:
    """Return whether *urgency* sits in the attention (warn-tinted) band.

    Args:
        urgency: The urgency to classify.

    Returns:
        ``True`` for :attr:`~eawf.kernel.state.enums.Urgency.HIGH` /
        :attr:`~eawf.kernel.state.enums.Urgency.URGENT`, ``False`` for the
        calmer tiers.
    """
    return urgency in (Urgency.HIGH, Urgency.URGENT)


class NeedsUserInbox(ModalScreen[None]):
    """Scrollable global needs_user pause inbox (Enter opens, Esc closes).

    Built with a pre-ranked tuple of
    :class:`~eawf.workflow.skills.needs_user.OpenPause` (the host resolves
    + ranks them from
    :func:`~eawf.workflow.skills.needs_user.list_open_pauses` via
    :func:`rank_pauses_by_urgency`) so the overlay never spawns the scan
    itself. ``↑`` / ``↓`` move the highlight, ``Enter`` opens the
    highlighted pause's :class:`NeedsUserModal`, ``Esc`` closes. An empty
    row set renders the :data:`EMPTY_INBOX_TEXT` honest-empty note.
    """

    #: One inbox overlay at a time -- a re-fired ``i`` over an already-open
    #: inbox is a no-op (deduped by
    #: :meth:`~eawf.surfaces.tui.app.EaApp.push_modal`) rather than stacking a
    #: second identical inbox.
    dedupe_singleton: ClassVar[bool] = True

    DEFAULT_CSS: ClassVar[str] = """
    NeedsUserInbox {
        align: center middle;
    }
    NeedsUserInbox > #inbox-card {
        width: 80%;
        max-width: 120;
        height: 70%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    NeedsUserInbox .inbox-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    NeedsUserInbox #inbox-list {
        height: 1fr;
    }
    NeedsUserInbox .inbox-row {
        height: auto;
    }
    NeedsUserInbox .inbox-row.-selected {
        text-style: bold reverse;
    }
    NeedsUserInbox .inbox-empty {
        color: $text-muted;
        height: auto;
    }
    NeedsUserInbox .inbox-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``↑`` / ``↓`` move the highlight, ``Enter`` opens the highlighted
    #: pause's modal, ``a`` approves it (resolves with its first option) and
    #: ``h`` holds it (resolves with its second option) -- both through the
    #: shared resume path -- ``x`` (alias ``d``) dismisses (acknowledges) it for
    #: the session, ``Esc`` closes. Vim ``j`` / ``k`` ride the arrows.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("k", "move(-1)", "up", show=False),
        Binding("j", "move(1)", "down", show=False),
        Binding("enter", "open_pause", "open", show=False),
        Binding("a", "approve_row", "approve", show=False),
        Binding("h", "hold_row", "hold", show=False),
        Binding("x", "dismiss_row", "dismiss", show=False),
        Binding("d", "dismiss_row", "dismiss", show=False),
        Binding("escape", "close", "close", show=False),
    ]

    #: Index of the highlighted pause row (``-1`` when the inbox is empty).
    selected: reactive[int] = reactive(0)

    def __init__(self, pauses: tuple[OpenPause, ...], *, now: datetime | None = None) -> None:
        """Construct the inbox for a pre-ranked pause set.

        Args:
            pauses: The open pauses, already ranked most-urgent first by
                :func:`rank_pauses_by_urgency` (the host resolves them from
                :func:`~eawf.workflow.skills.needs_user.list_open_pauses`).
            now: Reference instant for the per-row ``time-ago`` labels; a
                deterministic harness pins it for stable goldens. Defaults to
                the wall clock.
        """
        super().__init__()
        self._pauses = pauses
        self._now = now if now is not None else datetime.now(UTC)

    def compose(self) -> ComposeResult:
        """Yield the titled card, the ranked pause list (or note), and hint.

        When the pause set is empty the list collapses to the literal
        :data:`EMPTY_INBOX_TEXT` calm note -- no fabricated pause row is
        emitted, so an empty inbox never paints a hollow card masquerading
        as an open pause.
        """
        mode = self._render_mode()
        sigil = chrome("attention", mode=mode)
        with VerticalScroll(id="inbox-card"):
            yield Static(
                f"[$warn]{sigil}[/] [$accent]needs_user inbox[/] - {len(self._pauses)}",
                classes="inbox-title",
            )
            with VerticalScroll(id="inbox-list"):
                if self._pauses:
                    for index, pause in enumerate(self._pauses):
                        yield Static(
                            _render_row(pause, now=self._now, mode=mode),
                            classes="inbox-row",
                            id=f"inbox-row-{index}",
                        )
                else:
                    yield Static(EMPTY_INBOX_TEXT, classes="inbox-empty")
            yield Static(
                "[ Up/Down move - Enter open - a approve - h hold - x dismiss - Esc close ]",
                classes="inbox-hint",
            )

    def on_mount(self) -> None:
        """Paint the initial highlight, then watch for a render-mode flip.

        Wires a ``render_mode`` watcher so a unicode <-> ASCII flip repaints
        the inbox glyphs (the title + per-row attention sigils) through the
        same row rebuild a dismiss drives.
        """
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        if not self._pauses:
            self.selected = -1
            return
        self._repaint_selection()

    def _on_render_mode(self, _mode: object) -> None:
        """Repaint the inbox glyphs when the App's render mode flips."""
        self.run_worker(self._rebuild_rows(), exclusive=True)

    def _render_mode(self) -> str:
        """Resolve the active render-mode label from the host app.

        Threads :attr:`~eawf.surfaces.tui.app.EaApp.render_mode` into the
        sigil helper so an ``ascii`` flip swaps the attention glyph to its
        ASCII column; falls back to the unicode column under a bare test
        harness whose host App carries no ``render_mode`` attribute.

        Returns:
            The render-mode label (``"ascii"`` or a unicode label).
        """
        return getattr(self.app, "render_mode", _DEFAULT_RENDER_MODE)

    def watch_selected(self) -> None:
        """Repaint the row highlight when the selection moves."""
        if self.is_mounted and self._pauses:
            self._repaint_selection()

    def _repaint_selection(self) -> None:
        """Toggle the ``-selected`` class onto the highlighted row."""
        for index in range(len(self._pauses)):
            row_widget = self.query_one(f"#inbox-row-{index}", Static)
            row_widget.set_class(index == self.selected, "-selected")

    def action_move(self, delta: int) -> None:
        """Move the highlight by *delta*, clamped to the row range.

        Args:
            delta: ``-1`` to move up, ``+1`` to move down.
        """
        if not self._pauses:
            return
        self.selected = max(0, min(self.selected + delta, len(self._pauses) - 1))

    def action_open_pause(self) -> None:
        """Open the highlighted pause's :class:`NeedsUserModal`.

        Closes the inbox first, then routes the highlighted pause's
        question through the App's needs_user open path (the same
        :func:`~eawf.surfaces.tui.screens.overlays.needs_user.open_needs_user`
        the auto-open uses) so the modal-stack cap is honoured and the host
        owns the pick callback. A no-op when the list is empty.
        """
        if not self._pauses or not (0 <= self.selected < len(self._pauses)):
            return
        target = self._pauses[self.selected]
        logger.info(f"needs_user_inbox open pause_urn={target.pause_urn!r}")
        app = self.app
        self.dismiss(None)
        open_pause = getattr(app, "open_needs_user_pause", None)
        if callable(open_pause):
            open_pause(target.pause_urn, target.question)
            return
        from eawf.surfaces.tui.screens.overlays.needs_user import open_needs_user

        open_needs_user(app, target.question)

    def action_approve_row(self) -> None:
        """Approve the highlighted pause (resolve with its first option, ``a``).

        Answers the highlighted pause through the App's shared
        :meth:`~eawf.surfaces.tui.app.EaApp.resolve_needs_user_pause` resume
        path -- the SAME daemon-RPC-then-local path the modal pick uses -- with
        the pause question's FIRST option label (the affirmative). On a
        successful resume the row drops out of the inbox live; a resume failure
        leaves the row so the operator can retry. A no-op when the list is empty
        or the host exposes no resume seam (a bare harness). Approve is the
        general-kind affirmative analog of ``h`` (hold) and ``x`` (dismiss).
        """
        self._resolve_row(option_index=0, verb="approve")

    def action_hold_row(self) -> None:
        """Hold the highlighted pause (resolve with its second option, ``h``).

        Answers the highlighted pause through the App's shared
        :meth:`~eawf.surfaces.tui.app.EaApp.resolve_needs_user_pause` resume
        path -- the SAME path ``a`` (approve) and the modal pick use -- with the
        pause question's SECOND option label (the deferral / negative). On a
        successful resume the row drops out of the inbox live; a resume failure
        leaves the row so the operator can retry. A no-op when the list is empty
        or the host exposes no resume seam.
        """
        self._resolve_row(option_index=1, verb="hold")

    def _resolve_row(self, *, option_index: int, verb: str) -> None:
        """Resolve the highlighted pause with its *option_index* option label.

        The shared body behind ``a`` (approve, option 0) and ``h`` (hold,
        option 1): looks up the highlighted pause, picks the option label at
        *option_index* (every pause question carries 2-4 options, so index 0/1
        always resolve), and routes it through the host's
        :meth:`~eawf.surfaces.tui.app.EaApp.resolve_needs_user_pause` -- the one
        resume seam the modal pick also uses. Drops the row only on a successful
        resume so a failed answer stays visible for a retry. A no-op when the
        list is empty or the host exposes no resume seam.

        Args:
            option_index: The pause question option to answer with (``0`` for
                approve, ``1`` for hold).
            verb: The action verb for the log line (``"approve"`` / ``"hold"``).
        """
        if not self._pauses or not (0 <= self.selected < len(self._pauses)):
            return
        target = self._pauses[self.selected]
        resolve = getattr(self.app, "resolve_needs_user_pause", None)
        if not callable(resolve):
            return
        choice = target.question.options[option_index].label
        logger.info(
            f"needs_user_inbox resolve_row verb={verb} "
            f"pause_urn={target.pause_urn!r} choice={choice!r}"
        )
        if not resolve(target.pause_urn, choice):
            return
        self._drop_row(target.pause_urn)

    def action_dismiss_row(self) -> None:
        """Acknowledge (hide) the highlighted pause for this session.

        Records the pause's session dismiss key on the App's dismissed-set
        (the same set the Home attention band filters against, so the pause
        also disappears from the band) and removes the row from this inbox
        live. The general-kind ``dismiss`` analog of ``Enter``'s open + the
        pause-specific ``defer``. Bound to ``x`` (and the legacy ``d`` alias).
        Named ``action_dismiss_row`` (not ``action_dismiss``) so it does not
        shadow the Textual screen-dismiss action a
        :class:`~textual.screen.ModalScreen` already owns. A no-op when the list
        is empty.
        """
        if not self._pauses or not (0 <= self.selected < len(self._pauses)):
            return
        target = self._pauses[self.selected]
        dismiss = getattr(self.app, "dismiss_attention", None)
        if callable(dismiss):
            logger.info(f"needs_user_inbox dismiss pause_urn={target.pause_urn!r}")
            dismiss(_pause_dismiss_key(target))
        self._drop_row(target.pause_urn)

    def _drop_row(self, pause_urn: str) -> None:
        """Remove the resolved / dismissed *pause_urn* row and repaint live.

        The shared row-removal tail behind ``a`` / ``h`` / ``x``: drops the
        pause from the in-memory set, re-clamps the highlight, and schedules the
        list rebuild so the row disappears immediately.

        Args:
            pause_urn: The pause whose row is dropped from the inbox.
        """
        self._pauses = tuple(p for p in self._pauses if p.pause_urn != pause_urn)
        self.selected = min(self.selected, len(self._pauses) - 1) if self._pauses else -1
        self.run_worker(self._rebuild_rows(), exclusive=True)

    async def _rebuild_rows(self) -> None:
        """Repaint the inbox list after a dismiss removed a row.

        Awaits the prior rows' removal before re-mounting so the fresh
        ``inbox-row-*`` ids cannot collide with the outgoing ones
        (``DuplicateIds``), mirroring the Home band's rebuild discipline.
        """
        mode = self._render_mode()
        sigil = chrome("attention", mode=mode)
        listing = self.query_one("#inbox-list", VerticalScroll)
        await listing.remove_children()
        self.query_one(".inbox-title", Static).update(
            f"[$warn]{sigil}[/] [$accent]needs_user inbox[/] - {len(self._pauses)}"
        )
        if not self._pauses:
            await listing.mount(Static(EMPTY_INBOX_TEXT, classes="inbox-empty"))
            return
        await listing.mount_all(
            [
                Static(
                    _render_row(pause, now=self._now, mode=mode),
                    classes="inbox-row",
                    id=f"inbox-row-{i}",
                )
                for i, pause in enumerate(self._pauses)
            ]
        )
        self._repaint_selection()

    def action_close(self) -> None:
        """Dismiss the inbox overlay (``Esc``)."""
        self.dismiss(None)


def open_needs_user_inbox(
    app: App[None], pauses: tuple[OpenPause, ...], *, now: datetime | None = None
) -> bool:
    """Push the needs_user inbox overlay onto *app* (modal-cap-aware).

    Routes through the App's ``push_modal`` helper so the modal-stack
    depth cap is enforced in one place; falls back to a plain
    ``push_screen`` under a bare harness that lacks the cap helper.

    Args:
        app: The running App.
        pauses: The pre-ranked open pauses (most-urgent first).
        now: Reference instant for the per-row ``time-ago`` labels; defaults
            to the wall clock.

    Returns:
        ``True`` when the modal was pushed, ``False`` when the cap
        rejected it.
    """
    modal = NeedsUserInbox(pauses, now=now)
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        return bool(push_modal(modal))
    app.push_screen(modal)
    return True


__all__ = [
    "EMPTY_INBOX_TEXT",
    "NeedsUserInbox",
    "open_needs_user_inbox",
    "rank_pauses_by_urgency",
]
