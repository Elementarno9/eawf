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
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.kernel.state.enums import Urgency

if TYPE_CHECKING:
    from textual.app import App

    from eawf.workflow.skills.needs_user import OpenPause

logger = logging.getLogger(__name__)

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


def _render_row(pause: OpenPause) -> str:
    """Render one pause as a single content-markup line.

    The urgency token is tinted to draw the eye to the more-immediate
    rows: ``URGENT`` / ``HIGH`` render in the ``$warn`` attention colour,
    the calmer tiers in ``$text-muted``. The colours resolve against the
    active theme at render time via Textual content markup.

    Args:
        pause: The pause row to render.

    Returns:
        A content-markup string for one :class:`~textual.widgets.Static`.
    """
    cell = f"{pause.urgency.value:<8}"
    tier = f"[$warn]{cell}[/]" if _is_attention(pause.urgency) else cell
    label = _row_label(pause)
    return f"{tier} [$accent]{label}[/]  {pause.question.question}"


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

    DEFAULT_CSS: ClassVar[str] = """
    NeedsUserInbox {
        align: center middle;
    }
    NeedsUserInbox > #inbox-card {
        width: 80%;
        max-width: 120;
        height: 70%;
        border: solid $accent;
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
    #: pause, ``Esc`` closes. Vim ``j`` / ``k`` ride the arrows.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("k", "move(-1)", "up", show=False),
        Binding("j", "move(1)", "down", show=False),
        Binding("enter", "open_pause", "open", show=False),
        Binding("escape", "close", "close", show=False),
    ]

    #: Index of the highlighted pause row (``-1`` when the inbox is empty).
    selected: reactive[int] = reactive(0)

    def __init__(self, pauses: tuple[OpenPause, ...]) -> None:
        """Construct the inbox for a pre-ranked pause set.

        Args:
            pauses: The open pauses, already ranked most-urgent first by
                :func:`rank_pauses_by_urgency` (the host resolves them from
                :func:`~eawf.workflow.skills.needs_user.list_open_pauses`).
        """
        super().__init__()
        self._pauses = pauses

    def compose(self) -> ComposeResult:
        """Yield the titled card, the ranked pause list (or note), and hint."""
        with VerticalScroll(id="inbox-card"):
            yield Static(f"needs_user inbox - {len(self._pauses)}", classes="inbox-title")
            with VerticalScroll(id="inbox-list"):
                if self._pauses:
                    for index, pause in enumerate(self._pauses):
                        yield Static(
                            _render_row(pause),
                            classes="inbox-row",
                            id=f"inbox-row-{index}",
                        )
                else:
                    yield Static(EMPTY_INBOX_TEXT, classes="inbox-empty")
            yield Static("[ Up/Down move - Enter open - Esc close ]", classes="inbox-hint")

    def on_mount(self) -> None:
        """Paint the initial highlight on the first row (when any)."""
        if not self._pauses:
            self.selected = -1
            return
        self._repaint_selection()

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

    def action_close(self) -> None:
        """Dismiss the inbox overlay (``Esc``)."""
        self.dismiss(None)


def open_needs_user_inbox(app: App[None], pauses: tuple[OpenPause, ...]) -> bool:
    """Push the needs_user inbox overlay onto *app* (modal-cap-aware).

    Routes through the App's ``push_modal`` helper so the modal-stack
    depth cap is enforced in one place; falls back to a plain
    ``push_screen`` under a bare harness that lacks the cap helper.

    Args:
        app: The running App.
        pauses: The pre-ranked open pauses (most-urgent first).

    Returns:
        ``True`` when the modal was pushed, ``False`` when the cap
        rejected it.
    """
    modal = NeedsUserInbox(pauses)
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
