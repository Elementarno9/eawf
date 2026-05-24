"""``MultichoiceChecklist`` — inline ``[X]`` / ``[ ]`` editor for a multichoice key.

Extracted from :mod:`eawf.surfaces.tui.screens.overlays.config_modal` so the config
overlay module stays under the EAWF010 module-length budget: the checklist
is a self-contained Textual widget with its own keymap and message
contract, so it reads cleanly as a sibling widget the modal mounts rather
than a class buried in the modal file. The modal imports it and handles its
:class:`~MultichoiceChecklist.Committed` / :class:`~MultichoiceChecklist.Cancelled`
/ :class:`~MultichoiceChecklist.Toggled` messages; the message classes keep
their ``MultichoiceChecklist`` qualname so the modal's
``on_multichoice_checklist_*`` handler names resolve unchanged.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.binding import Binding, BindingType
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static


class MultichoiceChecklist(Static):
    """Inline ``[X]`` / ``[ ]`` checklist for a ``multichoice`` config key.

    Mounted in place of the focused field row when the operator presses
    ``Enter`` on a ``multichoice`` field. Each declared choice renders as a
    ``[X]`` (selected) or ``[ ]`` (cleared) line; a ``>`` caret marks the
    focused line. The widget owns the keyboard while open:

    * ``↑`` / ``↓`` move the line focus (clamped, no wrap).
    * ``Space`` toggles the focused line's membership.
    * ``Enter`` posts :class:`Committed` (the host validates + stages the
      selected list and tears the editor down).
    * ``Esc`` posts :class:`Cancelled` (the host tears down without staging).

    The two-``Enter`` semantics — first ``Enter`` (on the field row) opens
    this widget, the second ``Enter`` (here) commits — hold because this
    widget binds ``enter`` explicitly to :meth:`action_commit`, not to a
    toggle: ``Space`` is the sole toggle key, so the focused line is never
    both toggled and committed by one keystroke.
    """

    DEFAULT_CSS: ClassVar[str] = """
    MultichoiceChecklist {
        height: auto;
        max-height: 12;
        overflow-y: auto;
        background: $surface;
        color: $text;
    }
    MultichoiceChecklist:focus {
        background: $surface;
    }
    """

    can_focus = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("space", "toggle_item", "toggle", show=False),
        Binding("enter", "commit", "commit", show=False),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    class Toggled(Message):
        """Posted when ``Space`` toggles a checklist line.

        Attributes:
            item: The choice whose membership the operator flipped.
        """

        def __init__(self, item: str) -> None:
            self.item = item
            super().__init__()

    class Committed(Message):
        """Posted when the operator presses ``Enter`` to commit the selection.

        Attributes:
            selected: The selected choices in declaration order.
        """

        def __init__(self, selected: list[str]) -> None:
            self.selected = selected
            super().__init__()

    class Cancelled(Message):
        """Posted when the operator presses ``Esc`` to abort the edit."""

    #: The line under the checklist cursor. ``↑`` / ``↓`` clamp it to the
    #: first / last line (no wrap), mirroring the field-row cursor.
    line_index: reactive[int] = reactive(0)

    def __init__(
        self,
        *,
        choices: tuple[str, ...],
        selected: list[str],
        prefix: str,
        **kwargs: Any,
    ) -> None:
        """Construct the checklist for *choices*, pre-checking *selected*.

        Args:
            choices: The key's declared choices, in render order.
            selected: The currently-selected subset (seeds the ``[X]`` marks).
            prefix: Leading pad reused from :meth:`ConfigModal._meta_line`'s
                key column so each line aligns under the static value cell.
            **kwargs: Forwarded to :class:`~textual.widgets.Static` (e.g.
                ``id=`` / ``classes=``).
        """
        super().__init__("", markup=False, **kwargs)
        self._choices = choices
        self._selected: set[str] = {item for item in selected if item in choices}
        self._prefix = prefix

    def on_mount(self) -> None:
        """Paint the initial checklist and focus the widget."""
        self._repaint()
        self.focus()

    def selected_items(self) -> list[str]:
        """Return the selected choices in declaration order."""
        return [choice for choice in self._choices if choice in self._selected]

    def _repaint(self) -> None:
        """Repaint every checklist line (cursor caret + ``[X]`` / ``[ ]`` mark)."""
        lines: list[str] = []
        for index, choice in enumerate(self._choices):
            caret = ">" if index == self.line_index else " "
            mark = "X" if choice in self._selected else " "
            lines.append(f"{self._prefix}{caret} [{mark}] {choice}")
        self.update("\n".join(lines))

    def watch_line_index(self) -> None:
        """Repaint when the line cursor moves."""
        if self.is_mounted:
            self._repaint()

    def action_cursor_up(self) -> None:
        """Move the line cursor up (``↑``), clamped to the first line."""
        if self.line_index > 0:
            self.line_index -= 1

    def action_cursor_down(self) -> None:
        """Move the line cursor down (``↓``), clamped to the last line."""
        if self.line_index < len(self._choices) - 1:
            self.line_index += 1

    def action_toggle_item(self) -> None:
        """Toggle the focused line's membership (``Space``)."""
        if not self._choices:
            return
        item = self._choices[self.line_index]
        if item in self._selected:
            self._selected.discard(item)
        else:
            self._selected.add(item)
        self._repaint()
        self.post_message(self.Toggled(item))

    def action_commit(self) -> None:
        """Commit the current selection (``Enter`` — the second one)."""
        self.post_message(self.Committed(self.selected_items()))

    def action_cancel(self) -> None:
        """Abort the edit without staging (``Esc``)."""
        self.post_message(self.Cancelled())


__all__ = ["MultichoiceChecklist"]
