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
(:func:`global_key_rows` / :func:`mode_action_key_rows` /
:func:`reference_nav_rows` / :func:`pane_nav_rows` / :func:`scope_key_rows`)
so the table is unit-testable without mounting Textual; the screen is a
thin scrollable view over them. The per-mode action-key section
(:func:`mode_action_key_rows`) and the reference-nav section
(:func:`reference_nav_rows`) are auto-derived from each mode screen's own
``BINDINGS`` and the App's reference-nav bindings respectively, so a
mode rebinding flows into the help without a manual edit here.
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
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE
from eawf.surfaces.tui.widgets.sigils import chrome

logger = logging.getLogger(__name__)

#: Global keybinding rows (key, action) — the "Global" table. Full
#: key names. Scope switch is the raw ``w`` / ``r`` / ``u`` keys; the
#: ``Ctrl-`` chords are listed as muscle-memory aliases.
_GLOBAL_KEYS: tuple[tuple[str, str], ...] = (
    ("q", "quit"),
    ("Esc", "close overlay / drop palette / leave zoom"),
    ("w", "switch to workspace scope"),
    ("r", "switch to repo scope"),
    ("u", "switch to user scope"),
    ("Ctrl-W / Ctrl-R / Ctrl-U", "scope switch (aliases)"),
    ("c", "open config (any scope)"),
    ("i", "open needs_user inbox (urgent first)"),
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
#: scope-local keys (workspace ``z`` zoom) belong here.
_SCOPE_KEYS: dict[ScopeName, tuple[tuple[str, str], ...]] = {
    "repo": (),
    "workspace": (("z", "zoom focused repo to repo screen"),),
    "user": (),
}

#: Backlog-pane keys (key, action) — the "Backlog pane" table. These ride
#: the focused :class:`~eawf.surfaces.tui.widgets.backlog_table.BacklogTable`;
#: the substring filter is *set* via the ``/filter backlog`` palette verb
#: and *cleared* in-pane with ``x``.
_BACKLOG_KEYS: tuple[tuple[str, str], ...] = (
    ("Enter", "drill into row (detail modal)"),
    ("c", "toggle closed rows"),
    ("x", "clear active filter"),
)

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


def mode_key_rows() -> tuple[tuple[str, str], ...]:
    """Return the mode digit-key rows (digit, action) for the help table.

    Derived from the mode registry so the help reflects exactly the
    digit-key mode axis the chassis binds (one ``<digit> switch to <Title>
    mode`` row per mode). A new pane wave that adds a mode gets its help
    row for free. Imported lazily to keep the help module's import graph
    light.

    Returns:
        One ``(digit, action)`` row per registered mode, in digit order.
    """
    from eawf.surfaces.tui.modes.registry import MODE_REGISTRY

    return tuple((spec.digit, f"switch to {spec.title} mode") for spec in MODE_REGISTRY)


def mode_key_rows_active(current_mode: str) -> tuple[tuple[str, str, bool], ...]:
    """Return the mode digit-key rows tagged with the active-mode flag.

    Mirrors :func:`mode_key_rows` but appends an ``is_active`` flag per row,
    ``True`` only for the row whose mode name equals *current_mode*, so the
    help overlay can mark which mode the operator is currently in. A
    *current_mode* that matches no registered mode tags no row active (the
    honest path under a bare harness with no resolved mode).

    Args:
        current_mode: The App's active mode name (``app.current_mode``).

    Returns:
        One ``(digit, action, is_active)`` triple per registered mode, in
        digit order.
    """
    from eawf.surfaces.tui.modes.registry import MODE_REGISTRY

    return tuple(
        (spec.digit, f"switch to {spec.title} mode", spec.name == current_mode)
        for spec in MODE_REGISTRY
    )


def _mode_screen_classes() -> dict[str, type]:
    """Resolve the ``{mode_name: screen_class}`` map for the action-key help.

    Lazily imports each non-Home mode screen class (mirroring the deferred
    import :func:`mode_key_rows` uses for the registry) so the help
    module's import graph stays light and free of the scope-screen cycle.
    Home is intentionally absent: it reuses the resolved scope screen and
    owns no mode-specific :attr:`~textual.screen.Screen.BINDINGS`, so it
    contributes no action-key subsection.

    Returns:
        A ``{mode_name: screen_class}`` map keyed by the registry mode
        name, covering every mode that declares its own BINDINGS class.
    """
    from eawf.surfaces.tui.modes.agent_watch import AgentWatchModeScreen
    from eawf.surfaces.tui.modes.autopilot import AutopilotModeScreen
    from eawf.surfaces.tui.modes.doctor import DoctorModeScreen
    from eawf.surfaces.tui.modes.evidence import EvidenceModeScreen
    from eawf.surfaces.tui.modes.feed import FeedModeScreen
    from eawf.surfaces.tui.modes.research_board import ResearchBoardModeScreen
    from eawf.surfaces.tui.modes.sandbox_events import SandboxEventsModeScreen
    from eawf.surfaces.tui.modes.trust import TrustModeScreen

    return {
        "autopilot": AutopilotModeScreen,
        "research_board": ResearchBoardModeScreen,
        "trust": TrustModeScreen,
        "doctor": DoctorModeScreen,
        "evidence": EvidenceModeScreen,
        "feed": FeedModeScreen,
        "agent_watch": AgentWatchModeScreen,
        "sandbox_events": SandboxEventsModeScreen,
    }


def mode_action_key_rows() -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Return the per-mode action-key subsections for the help table.

    For each non-Home mode, in registry (digit) order, derive the mode's
    own action keys straight from the mode screen class so the help cannot
    silently diverge from the live bindings. A mode's *own* bindings are
    read off the class ``__dict__`` (not the inherited :attr:`BINDINGS`
    attribute) so the shared :class:`~eawf.surfaces.tui.scopes.ScopeScreen`
    chrome (palette / help / quit -- already documented under the global
    section) is not re-listed, and a mode that declares no action keys of
    its own (e.g. Feed / Doctor / Evidence) yields an empty row tuple rather
    than the inherited chrome.

    Each yielded :class:`~textual.binding.Binding` becomes a
    ``(key, description)`` row; no binding the mode declares is dropped, so
    the coverage test can assert every own-binding has a help row.

    Returns:
        One ``(mode_title, ((key, description), ...))`` pair per non-Home
        mode, in digit order. Modes that declare no own bindings carry an
        empty row tuple.
    """
    from eawf.surfaces.tui.modes.registry import MODE_REGISTRY

    classes = _mode_screen_classes()
    sections: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for spec in MODE_REGISTRY:
        cls = classes.get(spec.name)
        if cls is None:
            continue
        own_bindings = cls.__dict__.get("BINDINGS", ())
        rows = tuple(
            (binding.key, binding.description)
            for binding in own_bindings
            if isinstance(binding, Binding)
        )
        sections.append((spec.title, rows))
    return tuple(sections)


def reference_nav_rows() -> tuple[tuple[str, str], ...]:
    """Return the reference-nav key rows (key, action) for the help table.

    Surfaces the ``alt+left`` / ``alt+right`` history-style reference
    navigation that the App binds app-wide. Derived from
    :attr:`~eawf.surfaces.tui.app.EaApp.BINDINGS` filtered to the two
    ``reference_*`` actions (lazy import so the help module stays free of
    the App import cycle), so a rebinding of either key flows straight into
    the help.

    Returns:
        One ``(key, description)`` row per reference-nav binding, in the
        order the App declares them (``alt+left`` then ``alt+right``).
    """
    from eawf.surfaces.tui.app import EaApp

    return tuple(
        (binding.key, binding.description)
        for binding in EaApp.BINDINGS
        if isinstance(binding, Binding) and binding.action.startswith("reference_")
    )


def pane_nav_rows() -> tuple[tuple[str, str, str], ...]:
    """Return the pane-navigation rows (key, action, vim-alias)."""
    return _PANE_NAV


def backlog_key_rows() -> tuple[tuple[str, str], ...]:
    """Return the backlog-pane key rows (key, action) for the help table."""
    return _BACKLOG_KEYS


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

    #: One help overlay at a time -- a re-fired ``?`` / ``/help`` over an
    #: already-open help screen is a no-op (deduped by
    #: :meth:`~eawf.surfaces.tui.app.EaApp.push_modal`, in addition to the
    #: App-level ``_help_open`` guard) rather than stacking a duplicate.
    dedupe_singleton: ClassVar[bool] = True

    DEFAULT_CSS: ClassVar[str] = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > #help-container {
        width: 90%;
        height: 90%;
        border: round $accent;
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
    HelpScreen .help-row-active {
        text-style: bold;
        color: $accent;
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
        overview = chrome("overview", mode=getattr(self.app, "render_mode", DEFAULT_RENDER_MODE))
        with VerticalScroll(id="help-container"):
            yield Static(f"[$accent]{overview}[/] Eä TUI — Help", classes="help-title")
            yield Static("Global keys", classes="help-section")
            for key, action in global_key_rows():
                yield Static(f"  {key:<10} {action}", classes="help-row")
            yield Static("Modes (digit keys)", classes="help-section")
            yield from self._mode_key_widgets()
            yield Static("Pane navigation (vim alias)", classes="help-section")
            for key, action, alias in pane_nav_rows():
                yield Static(f"  {key:<10} {action}  ({alias})", classes="help-row")
            yield Static("Mode action keys", classes="help-section")
            yield from self._mode_action_widgets()
            yield Static("Reference navigation", classes="help-section")
            for key, action in reference_nav_rows():
                yield Static(f"  {key:<10} {action}", classes="help-row")
            yield Static(f"{self._scope.capitalize()} screen keys", classes="help-section")
            scope_rows = scope_key_rows(self._scope)
            if scope_rows:
                for key, action in scope_rows:
                    yield Static(f"  {key:<10} {action}", classes="help-row")
            else:
                yield Static("  (none)", classes="help-row")
            yield Static("Backlog pane (set filter via /filter)", classes="help-section")
            for key, action in backlog_key_rows():
                yield Static(f"  {key:<10} {action}", classes="help-row")
            yield Static("Config overlay (arrows nav, Enter edits)", classes="help-section")
            for key, action in config_overlay_rows():
                yield Static(f"  {key:<10} {action}", classes="help-row")
            yield Static("Palette verbs", classes="help-section")
            for verb in visible_verbs(self._scope):
                yield Static(f"  {verb.name:<16} {verb.hint}", classes="help-row")
            yield Static("[ Esc to close ]", classes="help-hint")

    def _mode_key_widgets(self) -> ComposeResult:
        """Yield the mode digit-key rows, highlighting the active mode.

        The row whose mode is the App's :attr:`current_mode` is marked with
        the shared dispatch sigil
        (:func:`~eawf.surfaces.tui.widgets.sigils.chrome`) cursor + an
        ``(active)`` tag and carries the ``help-row-active`` style class, so
        the operator reads at a glance which mode they are in. The marker is
        plain text so it survives the text-only snapshot capture (the CSS
        style would not appear in the captured frame on its own).
        """
        current_mode = self._resolve_current_mode()
        cursor = chrome("dispatch", mode=getattr(self.app, "render_mode", DEFAULT_RENDER_MODE))
        for key, action, is_active in mode_key_rows_active(current_mode):
            if is_active:
                yield Static(
                    f"{cursor} {key:<10} {action} (active)",
                    classes="help-row help-row-active",
                )
            else:
                yield Static(f"  {key:<10} {action}", classes="help-row")

    def _mode_action_widgets(self) -> ComposeResult:
        """Yield the per-mode action-key subsection widgets.

        One ``Static`` mode-title row per non-Home mode followed by a
        ``Static`` per ``(key, action)`` binding row (or a muted
        ``(navigation only)`` note when the mode declares no own action
        keys). Split out of :meth:`compose` so the section's nested loop
        stays out of the compose method's complexity budget.
        """
        for mode_title, rows in mode_action_key_rows():
            yield Static(f"  {mode_title}", classes="help-row")
            if rows:
                for key, action in rows:
                    yield Static(f"    {key:<10} {action}", classes="help-row")
            else:
                yield Static("    (navigation only)", classes="help-row")

    def _resolve_scope(self) -> ScopeName:
        """Read the host App's resolved scope (defaults to ``repo``).

        Returns:
            The App's ``_scope`` when known, else ``"repo"`` so the
            overlay degrades under a bare harness.
        """
        scope = getattr(self.app, "_scope", "repo")
        if scope in ("repo", "workspace", "user"):
            return scope  # type: ignore[return-value]
        return "repo"

    def _resolve_current_mode(self) -> str:
        """Read the host App's active mode name (empty when unknown).

        Mirrors the ``current_mode`` read the footer + header use. Returns
        the empty string under a bare harness with no resolved mode, which
        tags no row active rather than guessing.

        Returns:
            The App's ``current_mode`` when a string, else ``""``.
        """
        current_mode = getattr(self.app, "current_mode", None)
        if isinstance(current_mode, str):
            return current_mode
        return ""

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
    "backlog_key_rows",
    "config_overlay_rows",
    "global_key_rows",
    "mode_action_key_rows",
    "mode_key_rows",
    "mode_key_rows_active",
    "open_help",
    "pane_nav_rows",
    "reference_nav_rows",
    "scope_key_rows",
]
