"""``EditFieldModal`` — per-type single-field editor overlay.

The larger scalar-edit surface the
:class:`~eawf.tui.screens.overlays.config_modal.ConfigModal` opens for
a ``str`` field whose value is multi-line or wider than its row (a
``bool`` toggles in place on ``Enter``, a ``choice`` forward-cycles on
``Enter``, and a short ``str`` / ``int`` / ``float`` edits inline in its
row — none of those reach this overlay). The modal presents a single
:class:`~textual.widgets.Input` seeded with the field's current value;
``Enter`` validates the buffer against the field's declared type / range
and dismisses with the typed value, ``Esc`` cancels (dismisses ``None``).

Validation reports **inline below the input** rather than via a toast:
the field's :func:`~eawf.kernel.config.registry.coerce_and_validate` runs on the
raw buffer; on failure the error string renders in a dim-error row under
the input and the overlay stays open so the operator can correct the
value without retyping from scratch. On success the coerced (typed)
value is the dismiss payload, so the parent folds an already-validated
value into its dirty map.

The overlay holds no save logic — it returns a typed value (or ``None``);
persisting it through the layered-config writer is the parent
:class:`ConfigModal`'s job.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from eawf.cli.errors import UserError
from eawf.kernel.config.registry import ConfigKey, coerce_and_validate

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)


def seed_input_text(entry: ConfigKey, current: Any) -> str:
    """Return the initial input buffer text for *entry* given *current*.

    The buffer is always a string — the :class:`Input` edits text and
    :func:`coerce_and_validate` re-types it on accept. ``None`` (an unset
    value) seeds an empty buffer so the operator types from scratch.

    Args:
        entry: The registry entry describing the field being edited.
        current: The field's currently-resolved value (dirty buffer wins,
            then merged config, then registry default).

    Returns:
        The seed string for the input widget.
    """
    if current is None:
        return ""
    return str(current)


class EditFieldModal(ModalScreen[Any]):
    """Single-field per-type editor (returns the typed value on dismiss).

    ``Enter`` validates the input buffer against the field's declared
    type / range and dismisses with the coerced value; a validation
    failure renders inline below the input and keeps the overlay open.
    ``Esc`` cancels (dismisses ``None``). The dismiss value is therefore
    either an already-coerced value of the field's declared type or
    ``None`` when the operator cancelled.
    """

    DEFAULT_CSS: ClassVar[str] = """
    EditFieldModal {
        align: center middle;
    }
    EditFieldModal > #edit-field-box {
        width: 70%;
        max-width: 90;
        height: auto;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    EditFieldModal .edit-field-label {
        text-style: bold;
        color: $accent;
        height: auto;
        margin-bottom: 1;
    }
    EditFieldModal .edit-field-meta {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }
    EditFieldModal #edit-field-input {
        margin-bottom: 1;
    }
    EditFieldModal .edit-field-error {
        color: $error;
        height: auto;
    }
    EditFieldModal .edit-field-error.-ok {
        color: $text-muted;
    }
    EditFieldModal .edit-field-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``Esc`` cancels. ``Enter`` is handled via the focused
    #: :class:`Input`'s ``Submitted`` message (the Input owns the ``enter``
    #: binding), not a screen binding — see :meth:`on_input_submitted`.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "cancel", show=False),
    ]

    def __init__(self, entry: ConfigKey, current: Any) -> None:
        """Construct the editor for *entry* seeded from *current*.

        Args:
            entry: The registry entry describing the field. Its declared
                type / range / choices drive the accept-time validation.
            current: The field's currently-resolved value used to seed
                the input buffer.
        """
        super().__init__()
        self._entry = entry
        self._seed = seed_input_text(entry, current)

    def compose(self) -> ComposeResult:
        """Yield the field label, type/range meta, input, and inline error row."""
        with Vertical(id="edit-field-box"):
            yield Static(self._entry.label, classes="edit-field-label")
            # markup=False — the ``[int]`` type cell is literal, not a tag.
            yield Static(self._meta_line(), classes="edit-field-meta", markup=False)
            yield Input(value=self._seed, id="edit-field-input")
            # markup=False — validation messages may contain ``[...]`` (e.g.
            # a choices list) that must render literally, not as a tag.
            yield Static("", classes="edit-field-error -ok", id="edit-field-error", markup=False)
            yield Static("[ Enter accept · Esc cancel ]", classes="edit-field-hint")

    def _meta_line(self) -> str:
        """Render the dotted key + type + range hint shown above the input."""
        parts = [self._entry.key, f"[{self._entry.type}]"]
        if self._entry.min_value is not None or self._entry.max_value is not None:
            low = "" if self._entry.min_value is None else f"{self._entry.min_value:g}"
            high = "" if self._entry.max_value is None else f"{self._entry.max_value:g}"
            parts.append(f"range {low}..{high}")
        return "  ".join(parts)

    def on_mount(self) -> None:
        """Focus the input so the operator types immediately."""
        self.query_one("#edit-field-input", Input).focus()

    def on_input_submitted(self, message: Input.Submitted) -> None:
        """Accept on ``Enter`` (the focused Input's ``Submitted`` message)."""
        message.stop()
        self.action_accept()

    def action_accept(self) -> None:
        """Validate the buffer; dismiss with the typed value or report inline.

        Runs :func:`coerce_and_validate` against the raw input buffer. On
        success the coerced (typed) value is the dismiss payload. On
        failure the error string renders in the inline error row and the
        overlay stays open.
        """
        raw = self.query_one("#edit-field-input", Input).value
        try:
            coerced = coerce_and_validate(self._entry, raw)
        except UserError as exc:
            self._report_error(str(exc))
            return
        logger.info(f"edit_field accept key={self._entry.key!r} type={self._entry.type}")
        self.dismiss(coerced)

    def action_cancel(self) -> None:
        """Dismiss with ``None`` (``Esc`` = cancel, no change)."""
        logger.info(f"edit_field cancel key={self._entry.key!r}")
        self.dismiss(None)

    def _report_error(self, message: str) -> None:
        """Render *message* in the inline error row below the input.

        Args:
            message: The validation error to surface.
        """
        row = self.query_one("#edit-field-error", Static)
        row.set_class(False, "-ok")
        row.update(message)


def open_edit_field(app: App[None], entry: ConfigKey, current: Any) -> bool:
    """Push an :class:`EditFieldModal` onto *app* (modal-cap-aware).

    Routes through the App's ``push_modal`` helper so the modal-stack
    depth cap is enforced in one place; falls back to a plain
    ``push_screen`` under a bare harness that lacks the cap helper.

    Args:
        app: The running App.
        entry: The registry entry for the field being edited.
        current: The field's currently-resolved value (seeds the input).

    Returns:
        ``True`` when the modal was pushed, ``False`` when the cap
        rejected it.
    """
    modal = EditFieldModal(entry, current)
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        return bool(push_modal(modal))
    app.push_screen(modal)
    return True


__all__ = ["EditFieldModal", "open_edit_field", "seed_input_text"]
