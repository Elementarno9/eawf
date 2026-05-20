"""``ConfirmModal`` — yes/no confirmation overlay (C06 §5.7).

The destructive-op approval overlay from the C06 brief §5.7 modal-stack
inventory: a small centred :class:`~textual.screen.ModalScreen` with a
prompt and two arrow-toggle choices (``Yes`` / ``No``). ``←`` / ``→``
move the selection, ``Enter`` confirms the highlighted choice, ``Esc``
cancels (equivalent to ``No``). The modal returns its boolean result
through Textual's ``ModalScreen`` dismiss value, so a caller awaits
``push_screen_wait`` (or passes a callback) to gate the destructive
action — cherry-pick, ``roadmap drop``, abandon, etc.

The overlay holds no domain logic: it presents a prompt string and yields
a boolean. The default highlighted choice is ``No`` so an accidental
``Enter`` on a destructive prompt is the safe answer.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

logger = logging.getLogger(__name__)

#: The two confirmation choices, ordered left-to-right for the arrow
#: toggle. Index ``0`` (``No``) is the safe default highlight.
_CHOICES: tuple[str, ...] = ("No", "Yes")


class ConfirmModal(ModalScreen[bool]):
    """Arrow-toggle yes/no confirmation (returns ``bool`` on dismiss).

    ``←`` / ``→`` move the highlight, ``Enter`` confirms the highlighted
    choice (``True`` for ``Yes``), and ``Esc`` cancels (``False``). The
    default highlight is ``No`` so a destructive prompt is safe to
    ``Enter`` through.
    """

    DEFAULT_CSS: ClassVar[str] = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > #confirm-box {
        width: auto;
        min-width: 40;
        max-width: 80;
        height: auto;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    ConfirmModal .confirm-prompt {
        height: auto;
        margin-bottom: 1;
    }
    ConfirmModal #confirm-choices {
        height: 1;
        align-horizontal: center;
    }
    ConfirmModal .confirm-choice {
        width: auto;
        margin: 0 2;
        color: $text-muted;
    }
    ConfirmModal .confirm-choice.-selected {
        color: $accent;
        text-style: bold reverse;
    }
    """

    #: ``←`` / ``→`` toggle, ``Enter`` confirms, ``Esc`` cancels. Vim
    #: aliases ``h`` / ``l`` ride the arrows per the operator keymap.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "move(0)", "no", show=False),
        Binding("right", "move(1)", "yes", show=False),
        Binding("h", "move(0)", "no", show=False),
        Binding("l", "move(1)", "yes", show=False),
        Binding("enter", "confirm", "confirm", show=False),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    #: Index into :data:`_CHOICES` of the highlighted choice (``0`` = No).
    selected: reactive[int] = reactive(0)

    def __init__(self, prompt: str) -> None:
        """Construct the confirmation for *prompt*.

        Args:
            prompt: The question shown above the Yes/No toggle.
        """
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        """Yield the prompt + the two arrow-toggle choice cells."""
        with Vertical(id="confirm-box"):
            yield Static(self._prompt, classes="confirm-prompt")
            with Horizontal(id="confirm-choices"):
                for index, label in enumerate(_CHOICES):
                    yield Static(label, classes="confirm-choice", id=f"choice-{index}")

    def on_mount(self) -> None:
        """Paint the initial highlight on the safe default (``No``)."""
        self._repaint_choices()

    def watch_selected(self) -> None:
        """Repaint the choice highlight when the selection moves."""
        if self.is_mounted:
            self._repaint_choices()

    def _repaint_choices(self) -> None:
        """Toggle the ``-selected`` class onto the highlighted choice."""
        for index in range(len(_CHOICES)):
            cell = self.query_one(f"#choice-{index}", Static)
            cell.set_class(index == self.selected, "-selected")

    def action_move(self, index: int) -> None:
        """Highlight the choice at *index* (``←`` = 0 / ``→`` = 1).

        Args:
            index: The choice index to highlight.
        """
        if 0 <= index < len(_CHOICES):
            self.selected = index

    def action_confirm(self) -> None:
        """Dismiss with the highlighted choice (``Yes`` → ``True``)."""
        result = _CHOICES[self.selected] == "Yes"
        logger.info(f"confirm_modal result={result}")
        self.dismiss(result)

    def action_cancel(self) -> None:
        """Dismiss with ``False`` (``Esc`` = cancel = ``No``)."""
        self.dismiss(False)


__all__ = ["ConfirmModal"]
