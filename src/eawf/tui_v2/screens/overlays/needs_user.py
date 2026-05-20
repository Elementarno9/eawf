"""``NeedsUserModal`` — needs_user AskUserQuestion overlay.

The modal AUQ surface: auto-opened when the TUI receives a
``needs_user_pause`` envelope for the active scope/session, it renders
the envelope's
:class:`~eawf.skills.bodies.user_question.UserQuestion` — the prompt plus
its 2-4 enumerated options — and lets the operator pick one with ``↑`` /
``↓`` + ``Enter``. The chosen option's label routes back to the paused
skill via ``eawf skill resume <pause-urn> --choice <label>``. ``Esc``
defers (the daemon keeps the pause open; the next queued envelope opens
when this one dismisses).

This wave lands the **overlay**: the question + option list rendered from
the typed ``UserQuestion``, the ``↑`` / ``↓`` highlight, and the chosen
label returned through the ``ModalScreen`` dismiss value (``None`` on
defer). Wiring the pick to the ``eawf skill resume`` CLI verb + the
``needs_user_pause`` daemon-push auto-open rides the wave that lands
those seams — the resume CLI verb does not exist yet — so the host runs
the resume on the returned label.

The overlay holds no domain logic beyond presenting a validated
``UserQuestion`` and returning the picked label; the question is built by
the host from the envelope ``body.user_question``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from eawf.skills.bodies.user_question import UserQuestion

logger = logging.getLogger(__name__)


class NeedsUserModal(ModalScreen[str]):
    """needs_user AUQ overlay (returns the picked option label on dismiss).

    ``↑`` / ``↓`` move the highlight across the question's 2-4 options,
    ``Enter`` confirms the highlighted option (its label is the dismiss
    value, consumed by the host's ``eawf skill resume`` call), and ``Esc``
    defers (dismiss ``None`` — the daemon keeps the pause open). The
    initial highlight is the first option.
    """

    DEFAULT_CSS: ClassVar[str] = """
    NeedsUserModal {
        align: center middle;
    }
    NeedsUserModal > #needs-user-box {
        width: 70%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    NeedsUserModal .needs-user-question {
        text-style: bold;
        color: $accent;
        height: auto;
        margin-bottom: 1;
    }
    NeedsUserModal .needs-user-option {
        height: auto;
        color: $text-muted;
        padding: 0 1;
    }
    NeedsUserModal .needs-user-option.-selected {
        color: $accent;
        text-style: bold reverse;
    }
    NeedsUserModal .needs-user-desc {
        height: auto;
        color: $text-muted;
        padding: 0 3;
    }
    NeedsUserModal .needs-user-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``↑`` / ``↓`` move the highlight, ``Enter`` confirms, ``Esc``
    #: defers. Vim ``j`` / ``k`` ride the arrows per the operator keymap.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("k", "move(-1)", "up", show=False),
        Binding("j", "move(1)", "down", show=False),
        Binding("enter", "confirm", "confirm", show=False),
        Binding("escape", "defer", "defer", show=False),
    ]

    #: Index of the highlighted option (``0`` = first option).
    selected: reactive[int] = reactive(0)

    def __init__(self, question: UserQuestion) -> None:
        """Construct the overlay for a validated *question*.

        Args:
            question: The ``body.user_question`` payload from the
                ``needs_user_pause`` envelope (2-4 options, validated by
                the :class:`~eawf.skills.bodies.user_question.UserQuestion`
                model).
        """
        super().__init__()
        self._question = question
        self._labels: tuple[str, ...] = tuple(opt.label for opt in question.options)

    def compose(self) -> ComposeResult:
        """Yield the question, the option list, and the keymap hint."""
        with Vertical(id="needs-user-box"):
            yield Static(self._question.question, classes="needs-user-question")
            with VerticalScroll():
                for index, option in enumerate(self._question.options):
                    yield Static(option.label, classes="needs-user-option", id=f"option-{index}")
                    if option.description:
                        yield Static(option.description, classes="needs-user-desc")
            yield Static("[ ↑/↓ select · Enter choose · Esc defer ]", classes="needs-user-hint")

    def on_mount(self) -> None:
        """Paint the initial highlight on the first option."""
        self._repaint_options()

    def watch_selected(self) -> None:
        """Repaint the option highlight when the selection moves."""
        if self.is_mounted:
            self._repaint_options()

    def _repaint_options(self) -> None:
        """Toggle the ``-selected`` class onto the highlighted option."""
        for index in range(len(self._labels)):
            cell = self.query_one(f"#option-{index}", Static)
            cell.set_class(index == self.selected, "-selected")

    def action_move(self, delta: int) -> None:
        """Move the highlight by *delta*, wrapping at the ends.

        Args:
            delta: ``-1`` for the previous option, ``+1`` for the next.
        """
        count = len(self._labels)
        self.selected = (self.selected + delta) % count

    def action_confirm(self) -> None:
        """Dismiss with the highlighted option's label."""
        label = self._labels[self.selected]
        logger.info(f"needs_user choice={label!r}")
        self.dismiss(label)

    def action_defer(self) -> None:
        """Dismiss with ``None`` (``Esc`` = defer; pause stays open)."""
        logger.info("needs_user action='defer'")
        self.dismiss(None)


def open_needs_user(app: object, question: UserQuestion) -> None:
    """Push the needs_user overlay onto *app*'s screen stack (cap-checked).

    Routes through the App's modal-cap-aware ``push_modal`` helper when
    present (so the modal-stack depth limit is enforced), falling back to a
    plain ``push_screen`` under a bare harness — mirroring the
    :func:`~eawf.tui_v2.screens.help.open_help` pattern. The future
    ``needs_user_pause`` daemon-push handler calls this for the active
    scope/session; there is no palette verb for it (it is daemon-push
    only).

    Args:
        app: The running App (typed loosely to avoid an import cycle with
            :mod:`eawf.tui_v2.app`).
        question: The validated ``body.user_question`` payload.
    """
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        push_modal(NeedsUserModal(question))
        return
    push_screen = getattr(app, "push_screen", None)
    if callable(push_screen):
        push_screen(NeedsUserModal(question))


__all__ = ["NeedsUserModal", "open_needs_user"]
