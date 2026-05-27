"""``CommandPalette`` — the ``/``-triggered command palette.

A :class:`~textual.screen.ModalScreen` overlaying any scope screen: the
operator presses ``/`` (wired on the shared
:class:`~eawf.surfaces.tui.scopes.ScopeScreen`), an :class:`~textual.widgets.Input`
opens pre-filled with ``/``, and an :class:`~textual.widgets.OptionList`
below lists the verbs from the static registry
(:mod:`eawf.surfaces.tui.palette.verbs`) filtered + ranked by fuzzy match as the
operator types.

Palette UX:

* The verbs shown are :func:`~eawf.surfaces.tui.palette.verbs.visible_verbs`
  for the App's resolved scope (so profile-gated verbs hide off their
  profile, etc.), then
  :func:`~eawf.surfaces.tui.palette.verbs.rank_verbs` re-orders by the typed
  filter.
* ``Tab`` autocompletes the input to the highlighted option's verb name.
* ``Enter`` parses ``(verb, args)`` via
  :func:`~eawf.surfaces.tui.palette.verbs.split_verb_args`, dismisses the
  palette, and runs the matched verb's handler.
* ``Esc`` dismisses without executing.

The legacy ``:`` alias is gone in v0.3 — only ``/`` opens the palette;
this module never binds ``:``.

The fuzzy filter + the verb resolution live in pure functions on the
registry module so the palette widget stays a thin view: it owns the
Input / OptionList wiring and the keystroke routing, not the matching
logic. That keeps the matcher unit-testable without mounting Textual and
keeps the palette Pilot-testable through ``run_test``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from eawf.surfaces.tui.palette.verbs import (
    PaletteVerb,
    ScopeName,
    rank_verbs,
    split_verb_args,
    visible_verbs,
)

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)

#: The leading token the palette input is seeded with — pressing ``/``
#: opens the palette already primed so the operator types the verb tail
#: directly.
PALETTE_PREFIX: str = "/"


def _option_label(verb: PaletteVerb) -> str:
    """Render a verb's palette row: ``name  grammar — hint``.

    Args:
        verb: The verb to label.

    Returns:
        A single-line label combining the verb name, its (optional)
        argument grammar, and the one-line hint.
    """
    head = verb.name if not verb.args_grammar else f"{verb.name} {verb.args_grammar}"
    return f"{head}  —  {verb.hint}"


class CommandPalette(ModalScreen[None]):
    """The ``/`` command palette overlay (fuzzy-filterable verb list).

    Opened by the shared :class:`~eawf.surfaces.tui.scopes.ScopeScreen`
    ``open_palette`` action (and the ``/`` keypress that triggers it). The
    palette reads the App's resolved scope to decide which verbs to offer,
    re-ranks them on every keystroke, and runs the selected verb's handler
    on ``Enter``.
    """

    DEFAULT_CSS: ClassVar[str] = """
    CommandPalette {
        align: center top;
    }
    CommandPalette > #palette-box {
        width: 80%;
        max-width: 100;
        height: auto;
        margin-top: 2;
        border: solid $accent;
        background: $surface;
        padding: 0 1;
    }
    CommandPalette #palette-input {
        border: none;
        height: 1;
    }
    CommandPalette #palette-options {
        height: auto;
        max-height: 16;
        border: none;
    }
    """

    #: Palette-local bindings. ``Esc`` closes; ``Tab`` autocompletes the
    #: input to the highlighted option; arrows move the option cursor
    #: while the Input keeps focus (handled in ``on_key``).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
        Binding("tab", "autocomplete", "autocomplete", show=False),
    ]

    def __init__(self) -> None:
        """Construct the palette resolving the host App's scope on mount."""
        super().__init__()
        self._scope: ScopeName = "repo"

    def compose(self) -> ComposeResult:
        """Yield the input + option-list box."""
        with Vertical(id="palette-box"):
            yield Input(value=PALETTE_PREFIX, id="palette-input")
            yield OptionList(id="palette-options")

    def on_mount(self) -> None:
        """Resolve the active scope, seed the option list, focus the input.

        The cursor is parked at the end of the seeded ``/`` so the
        operator types the verb tail without first clearing the prefix.
        """
        self._scope = self._resolve_scope()
        self._refresh_options(PALETTE_PREFIX)
        palette_input = self.query_one("#palette-input", Input)
        palette_input.focus()
        palette_input.cursor_position = len(palette_input.value)

    def _resolve_scope(self) -> ScopeName:
        """Read the host App's resolved scope name (defaults to ``repo``).

        Returns:
            The App's ``_scope`` when it is a known scope name, else
            ``"repo"`` so the palette degrades gracefully under a bare
            test harness.
        """
        scope = getattr(self.app, "_scope", "repo")
        if scope in ("repo", "workspace", "user"):
            return scope  # type: ignore[return-value]
        return "repo"

    def current_verbs(self, query: str) -> list[PaletteVerb]:
        """Return the scope-visible verbs ranked by *query*.

        Args:
            query: The current palette input text (including the leading
                ``/``).

        Returns:
            The visible verbs for the active scope, fuzzy-ranked by
            *query* (best match first).
        """
        candidates = visible_verbs(self._scope)
        return rank_verbs(candidates, query)

    def _refresh_options(self, query: str) -> None:
        """Repopulate the option list from the ranked verbs for *query*.

        Each option's id is the verb name so ``Enter`` / ``Tab`` resolve
        the selection without re-ranking. Clearing then re-adding keeps
        the highlight on the first (best) match.

        Args:
            query: The current palette input text.
        """
        option_list = self.query_one("#palette-options", OptionList)
        option_list.clear_options()
        verbs = self.current_verbs(query)
        for verb in verbs:
            option_list.add_option(Option(_option_label(verb), id=verb.name))
        if verbs:
            option_list.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-rank the option list as the operator types.

        Args:
            event: The Textual input-changed event carrying the new value.
        """
        self._refresh_options(event.value)

    def _highlighted_verb_name(self) -> str | None:
        """Return the verb name of the highlighted option, or ``None``.

        Returns:
            The highlighted option's id (the verb name), or ``None`` when
            the list is empty / nothing is highlighted.
        """
        option_list = self.query_one("#palette-options", OptionList)
        index = option_list.highlighted
        if index is None:
            return None
        option = option_list.get_option_at_index(index)
        return option.id

    def action_autocomplete(self) -> None:
        """Complete the input to the highlighted verb name (``Tab``).

        Sets the input to ``"<verb> "`` (trailing space) so the operator
        types args directly, then re-ranks. A no-op when nothing is
        highlighted.
        """
        name = self._highlighted_verb_name()
        if name is None:
            return
        palette_input = self.query_one("#palette-input", Input)
        palette_input.value = f"{name} "
        palette_input.cursor_position = len(palette_input.value)
        self._refresh_options(palette_input.value)

    def on_key(self, event: object) -> None:
        """Route ``up`` / ``down`` to the option list while Input is focused.

        The Input widget would otherwise swallow the arrow keys for its
        own cursor; intercepting them here lets the operator move the
        option highlight without leaving the input. Other keys fall
        through to the bindings / Input.

        Args:
            event: The Textual key event (typed loosely so the bare-key
                attribute access stays import-light).
        """
        key = getattr(event, "key", None)
        if key not in ("up", "down"):
            return
        option_list = self.query_one("#palette-options", OptionList)
        if option_list.option_count == 0:
            return
        if key == "down":
            option_list.action_cursor_down()
        else:
            option_list.action_cursor_up()
        stop = getattr(event, "stop", None)
        if callable(stop):
            stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run the resolved verb on ``Enter`` and dismiss the palette.

        An empty input (blank, or just the seeded ``/`` prefix) on
        ``Enter`` dismisses the palette — there is no verb to run and the
        operator is signalling "never mind", so closing is the least
        surprising response. Otherwise resolves ``(verb, args)`` from the
        current input via
        :func:`~eawf.surfaces.tui.palette.verbs.split_verb_args`, then matches
        the verb name against the visible registry for the active scope. A
        match dismisses the palette and runs the handler; an unknown verb
        toasts and leaves the palette open so the operator can correct it.

        Args:
            event: The Textual input-submitted event carrying the value.
        """
        if event.value.strip() in ("", PALETTE_PREFIX):
            logger.info("palette_submit_empty dismiss=1")
            self.dismiss(None)
            return
        verb_name, args = split_verb_args(event.value)
        verb = self._match_verb(verb_name)
        if verb is None:
            logger.info(f"palette_submit_unknown verb={verb_name!r}")
            self.app.notify(f"unknown verb: {verb_name}", severity="warning")
            return
        app = self.app
        self.dismiss(None)
        logger.info(f"palette_run verb={verb.name!r} args={args!r}")
        verb.handler(app, args)

    def _match_verb(self, verb_name: str) -> PaletteVerb | None:
        """Resolve *verb_name* to a verb visible on the active scope.

        Args:
            verb_name: The verb name parsed from the input.

        Returns:
            The matching :class:`~eawf.surfaces.tui.palette.verbs.PaletteVerb`,
            or ``None`` when no visible verb has that exact name.
        """
        for verb in visible_verbs(self._scope):
            if verb.name == verb_name:
                return verb
        return None

    def action_close(self) -> None:
        """Dismiss the palette without executing (``Esc``)."""
        self.dismiss(None)


def open_palette(app: App[None]) -> None:
    """Push the command palette onto *app*'s screen stack (cap-checked).

    The shared :class:`~eawf.surfaces.tui.scopes.ScopeScreen` ``open_palette``
    action calls this. It routes through the App's modal-cap-aware
    ``push_modal`` helper when present (so the modal-stack depth limit is
    enforced), falling back to a plain ``push_screen`` under a bare
    harness that has no such helper.

    Args:
        app: The running App.
    """
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        push_modal(CommandPalette())
        return
    app.push_screen(CommandPalette())


__all__ = [
    "PALETTE_PREFIX",
    "CommandPalette",
    "open_palette",
]
