"""``HelpScreen`` — full keymap + palette-verb overlay.

Reachable from the ``?`` keypress or the palette ``/help`` verb (both
route through the shared :class:`~eawf.surfaces.tui.scopes.ScopeScreen` /
App helpers so the modal-stack cap is honoured). The overlay renders the
master keybinding catalog — global keys, pane navigation (with vim
aliases), the active scope's extra keys, and the palette verb table —
in a scrollable card. ``Esc`` closes.

Every key is shown with its **full name** (``PageUp`` not
``PgUp``, ``PageDown`` not ``PgDn``) and arrows are listed as primary with
the vim ``hjkl`` aliases beside them, matching the operator keymap
convention. The verb table is built from the static registry
(:mod:`eawf.surfaces.tui.palette.verbs`) filtered to the active scope so the
help reflects exactly what the palette would offer.

The keymap content is assembled by pure helpers
(:func:`global_key_rows` / :func:`pane_nav_rows` / :func:`scope_key_rows`)
so the table is unit-testable without mounting Textual; the screen is a
thin scrollable view over them.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.palette.verbs import ScopeName, visible_verbs

logger = logging.getLogger(__name__)

#: Global keybinding rows (key, action) — the "Global" table. Full
#: key names. Scope switch is the raw ``w`` / ``r`` / ``u`` keys; the
#: ``Ctrl-`` chords are listed as muscle-memory aliases.
_GLOBAL_KEYS: tuple[tuple[str, str], ...] = (
    ("q", "quit"),
    ("Esc", "close overlay / clear filter / drop palette"),
    ("w", "switch to workspace scope"),
    ("r", "switch to repo scope"),
    ("u", "switch to user scope"),
    ("Ctrl-W / Ctrl-R / Ctrl-U", "scope switch (aliases)"),
    ("c", "open config (any scope)"),
    ("F5", "force refresh"),
    ("?", "open help"),
    ("/", "open palette"),
    ("Tab", "next pane"),
    ("Shift-Tab", "previous pane"),
)

#: Pane-navigation rows (key, action, vim-alias) — the "Pane
#: navigation" table. Arrows are primary; the vim column is the alias.
_PANE_NAV: tuple[tuple[str, str, str], ...] = (
    ("↑", "line up", "k"),
    ("↓", "line down", "j"),
    ("←", "collapse row / scroll left", "h"),
    ("→", "expand row / scroll right", "l"),
    ("PageUp", "half-page up", "Ctrl-u"),
    ("PageDown", "half-page down", "Ctrl-d"),
    ("Home", "top of pane", "gg"),
    ("End", "bottom of pane", "G"),
    ("Enter", "drill into row (modal)", "—"),
)

#: Per-scope extra keys — the "Per-screen extras". ``c`` (open config) is
#: scope-agnostic and lives in :data:`_GLOBAL_KEYS`; only genuinely
#: scope-local keys (workspace ``z`` zoom, wave-board ``f`` filter) belong
#: here.
_SCOPE_KEYS: dict[ScopeName, tuple[tuple[str, str], ...]] = {
    "repo": (),
    "workspace": (("z", "zoom focused repo to repo screen"),),
    "user": (),
    "wave_board": (("f", "cycle filter (all / active-only)"),),
}

#: Config-overlay keys (key, action) — the "Config overlay" table. Arrows
#: navigate (``↑`` / ``↓`` fields, ``←`` / ``→`` tabs) and ``Enter`` is
#: the sole mutator; vim ``j`` / ``k`` ride ``↓`` / ``↑`` as aliases.
_CONFIG_OVERLAY_KEYS: tuple[tuple[str, str], ...] = (
    ("↑ / ↓", "move field cursor (vim: k / j)"),
    ("← / →", "switch tab"),
    ("Enter", "toggle bool / cycle choice / edit value"),
    ("s", "save staged edits"),
    ("r", "reset staged edits"),
    ("L", "cycle writable layer"),
    ("Esc", "cancel inline edit / close"),
)


def global_key_rows() -> tuple[tuple[str, str], ...]:
    """Return the global key rows (key, action) for the help table."""
    return _GLOBAL_KEYS


def pane_nav_rows() -> tuple[tuple[str, str, str], ...]:
    """Return the pane-navigation rows (key, action, vim-alias)."""
    return _PANE_NAV


def config_overlay_rows() -> tuple[tuple[str, str], ...]:
    """Return the config-overlay key rows (key, action) for the help table."""
    return _CONFIG_OVERLAY_KEYS


def scope_key_rows(scope: ScopeName) -> tuple[tuple[str, str], ...]:
    """Return the per-scope extra key rows for *scope*.

    Args:
        scope: The active scope name.

    Returns:
        The scope's extra key rows (empty when the scope adds none).
    """
    return _SCOPE_KEYS.get(scope, ())


class HelpScreen(ModalScreen[None]):
    """Scrollable full-keymap + palette-verb help overlay (Esc to close).

    Renders the global keys, pane navigation (with vim aliases), the
    active scope's extras, and the palette verb table. Built thin over the
    pure row helpers so the content is testable without Textual.
    """

    DEFAULT_CSS: ClassVar[str] = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > #help-container {
        width: 90%;
        height: 90%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    HelpScreen .help-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    HelpScreen .help-section {
        text-style: bold;
        color: $accent;
        margin-top: 1;
        height: 1;
    }
    HelpScreen .help-row {
        height: auto;
    }
    HelpScreen .help-hint {
        color: $text-muted;
        margin-top: 1;
        height: 1;
    }
    """

    #: ``Esc`` closes the help overlay; the only binding it owns.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(self) -> None:
        """Construct the help overlay resolving the App scope on mount."""
        super().__init__()
        self._scope: ScopeName = "repo"

    def compose(self) -> ComposeResult:
        """Yield the scrollable help card with each keymap section."""
        self._scope = self._resolve_scope()
        with VerticalScroll(id="help-container"):
            yield Static("Eä TUI — Help", classes="help-title")
            yield Static("Global keys", classes="help-section")
            for key, action in global_key_rows():
                yield Static(f"  {key:<10} {action}", classes="help-row")
            yield Static("Pane navigation (vim alias)", classes="help-section")
            for key, action, alias in pane_nav_rows():
                yield Static(f"  {key:<10} {action}  ({alias})", classes="help-row")
            yield Static(f"{self._scope.capitalize()} screen keys", classes="help-section")
            scope_rows = scope_key_rows(self._scope)
            if scope_rows:
                for key, action in scope_rows:
                    yield Static(f"  {key:<10} {action}", classes="help-row")
            else:
                yield Static("  (none)", classes="help-row")
            yield Static("Config overlay (arrows nav, Enter edits)", classes="help-section")
            for key, action in config_overlay_rows():
                yield Static(f"  {key:<10} {action}", classes="help-row")
            yield Static("Palette verbs", classes="help-section")
            for verb in visible_verbs(self._scope):
                yield Static(f"  {verb.name:<16} {verb.hint}", classes="help-row")
            yield Static("[ Esc to close ]", classes="help-hint")

    def _resolve_scope(self) -> ScopeName:
        """Read the host App's resolved scope (defaults to ``repo``).

        Returns:
            The App's ``_scope`` when known, else ``"repo"`` so the
            overlay degrades under a bare harness.
        """
        scope = getattr(self.app, "_scope", "repo")
        if scope in ("repo", "workspace", "user", "wave_board"):
            return scope  # type: ignore[return-value]
        return "repo"

    def action_close(self) -> None:
        """Dismiss the help overlay (``Esc``)."""
        self.dismiss(None)

    def on_unmount(self) -> None:
        """Clear the App's help-open guard when this overlay closes."""
        clear = getattr(self.app, "_on_help_closed", None)
        if callable(clear):
            clear()


def open_help(app: object) -> None:
    """Push the help overlay onto *app*'s screen stack (cap-checked).

    Routes through the App's modal-cap-aware ``push_modal`` helper when
    present so the modal-stack depth limit is enforced; falls back to a
    plain ``push_screen`` under a bare harness. A second ``?`` while
    help is already open is suppressed by the App's help-already-open
    guard before this is called.

    Args:
        app: The running App (typed loosely to avoid an import cycle with
            :mod:`eawf.surfaces.tui.app`).
    """
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        push_modal(HelpScreen())
        return
    push_screen = getattr(app, "push_screen", None)
    if callable(push_screen):
        push_screen(HelpScreen())


__all__ = [
    "HelpScreen",
    "config_overlay_rows",
    "global_key_rows",
    "open_help",
    "pane_nav_rows",
    "scope_key_rows",
]
