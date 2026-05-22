"""Textual ``EaApp`` — the operator-surface entry point (tui_v2).

The v0.3+ TUI is **Textual** (reversing the prior ``rich`` pick); the
legacy ``src/eawf/tui/`` tree has been removed and ``tui_v2`` is now the
sole TUI surface. This module ships the App shell:

* :class:`EaApp` — a :class:`textual.app.App` subclass that resolves to
  one of three scope screens (``repo`` / ``workspace`` / ``user``) on
  launch, loads the Textual theme, declares the global key bindings
  (arrows primary, vim keys as aliases), and binds ``state.json`` into a
  reactive attribute via :class:`~eawf.tui_v2.state_binding.StateBinding`.
* The three scope screens are minimal placeholders here — the concrete
  2x2 quadrant / strip+zoom / attention-effort-portfolio compositions
  land in the follow-up waves of this band. The shell establishes the
  reactive plumbing, header branding, footer keymap, and scope-dispatch
  contract those waves build on.

Scope dispatch lives in the CLI bare-command handler
(:mod:`eawf.cli.app`); this module receives the already-resolved
``scope`` + ``state_path`` and renders the matching screen.

Branding + keymap follow the operator conventions: the header brand is
the literal ``Eä`` (capital E + a-umlaut), bold accent, positioned
outside-left of the scope breadcrumb; navigation lists arrow keys first
(``↑↓←→`` + ``PageUp`` / ``PageDown`` / ``Home`` / ``End`` / ``Enter`` /
``Esc``) with vim keys (``hjkl``) as secondary aliases only.

Performance: first paint is dominated by the screen ``compose`` + the
single read-only :func:`~eawf.tui_v2.state_binding.load_state` call
issued from ``on_mount``; the mtime-poll task is created but its first
probe is deferred behind ``asyncio.sleep``, so it never blocks first
paint. The placeholder screens compose a constant number of widgets,
keeping the shell well inside the <150 ms p99 first-paint budget the
W22 Pilot harness enforces.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Literal

from textual.app import App
from textual.binding import Binding, BindingType
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen

from eawf.state.enums import ScopeKind
from eawf.state.models import State
from eawf.tui_v2.scopes import RepoScreen, UserScreen, WorkspaceScreen
from eawf.tui_v2.state_binding import StateBinding, StateBindingCallbacks
from eawf.tui_v2.theme import (
    DEFAULT_THEME,
    EA_THEMES,
    resolve_theme_name,
)
from eawf.tui_v2.widgets.header import (
    BRAND,
    DEFAULT_PROJECT_CODE,
    Header,
    build_breadcrumb,
)

logger = logging.getLogger(__name__)

#: Literal scope kinds the App can launch into. ``repo`` / ``workspace``
#: mirror :class:`eawf.state.enums.ScopeKind`; ``user`` is the registry-
#: scoped portfolio view that has no ``state.json`` ``scope_kind`` of its
#: own (resolved from a populated ``~/.eawf/registry.json``).
ScopeName = Literal["repo", "workspace", "user"]

#: Backwards-compatible alias for the breadcrumb builder. The canonical
#: definition (with the ``BRAND`` / ``DEFAULT_PROJECT_CODE`` constants)
#: lives in :mod:`eawf.tui_v2.widgets.header` so the shared
#: :class:`~eawf.tui_v2.widgets.header.Header` and this module share one
#: source (DRY); this alias keeps the scaffold-test import path stable.
_breadcrumb = build_breadcrumb


def _persisted_theme() -> str:
    """Read the persisted ``ui.theme`` logical name from layered config.

    Reads through the same :func:`~eawf.config.layered.merge_config` path
    the config window writes through, so a value the operator saved via
    ``/config`` (or ``eawf config set ui.theme ...``) is honoured on the
    next launch. A missing key, an unreadable layer, or a value that is
    not a recognised logical name all degrade to :data:`DEFAULT_THEME` —
    the swap is a cosmetic preference, never a launch-blocking read.

    Returns:
        The persisted logical theme name, or :data:`DEFAULT_THEME` when
        none is persisted / the persisted value is unrecognised.
    """
    from eawf.config.layered import get_dotted, merge_config

    try:
        merged, _sources = merge_config()
        value = get_dotted(merged, "ui.theme")
    except (KeyError, OSError, ValueError) as exc:
        logger.debug(f"_persisted_theme fallback exc={exc!r}")
        return DEFAULT_THEME
    if isinstance(value, str) and resolve_theme_name(value) is not None:
        return value
    logger.debug(f"_persisted_theme unrecognised value={value!r}")
    return DEFAULT_THEME


class EaApp(App[None]):
    """Single Textual app; one of three scope screens chosen on launch.

    The scope is resolved by the CLI bare-command handler and handed in
    via :paramref:`scope`; this app pushes the matching screen in
    ``on_mount`` and binds ``state.json`` into the
    reactive :attr:`state` attribute through
    :class:`~eawf.tui_v2.state_binding.StateBinding`.
    """

    CSS_PATH: ClassVar[str] = "theme.tcss"

    #: Maximum number of stacked :class:`~textual.screen.ModalScreen`
    #: overlays (command palette, detail card, confirm, help, and the
    #: audit / plan-preview overlays of later waves). The cap is **3** —
    #: deep enough for the plan-mode → edit → confirm flow, shallow
    #: enough to keep the stack legible. A fourth push is rejected by
    #: :meth:`push_modal` with a toast.
    MAX_MODAL_DEPTH: ClassVar[int] = 3

    #: Global key bindings shared across every scope screen. Scope switch
    #: is the raw ``w`` / ``r`` / ``u`` keys (workspace / repo / user); the
    #: ``ctrl+`` chords stay as hidden aliases for muscle-memory back-
    #: compat. Arrow keys are primary navigation; vim ``hjkl`` are
    #: registered as hidden aliases (``show=False``) so the footer
    #: advertises arrows only, per the operator keymap convention.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("w", "switch_scope('workspace')", "workspace", show=False),
        Binding("r", "switch_scope('repo')", "repo", show=False),
        Binding("u", "switch_scope('user')", "user", show=False),
        Binding("ctrl+w", "switch_scope('workspace')", "workspace", show=False),
        Binding("ctrl+r", "switch_scope('repo')", "repo", show=False),
        Binding("ctrl+u", "switch_scope('user')", "user", show=False),
        Binding("q", "quit", "quit"),
        Binding("escape", "quit", "quit", show=False),
        # Vim-key aliases for navigation — secondary to the arrows the
        # individual screens bind; declared here so they resolve app-wide.
        Binding("h", "cursor_left", "left", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("l", "cursor_right", "right", show=False),
    ]

    SCREENS: ClassVar[dict[str, Callable[[], Screen[Any]]]] = {
        "repo": RepoScreen,
        "workspace": WorkspaceScreen,
        "user": UserScreen,
    }

    #: Reactive bound state — widgets ``watch`` this to repaint on
    #: change. ``None`` until the first read-only load completes (fresh
    #: workspace / daemon cold-spawn window).
    state: reactive[State | None] = reactive(None, init=False)

    #: ``True`` while the mtime-poll fallback is active (daemon push not
    #: yet wired / daemon unreachable). The header surfaces a degraded
    #: banner off this flag.
    degraded: reactive[bool] = reactive(False, init=False)

    def __init__(
        self,
        scope: ScopeName,
        state_path: Path | None,
    ) -> None:
        """Construct the app for a resolved scope.

        Args:
            scope: One of ``"repo"`` / ``"workspace"`` / ``"user"`` —
                resolved by the CLI bare-command dispatch ladder.
            state_path: Path to the scope's ``state.json`` (read-only),
                or ``None`` for the user scope / no resolved state.
        """
        super().__init__()
        self._scope: ScopeName = scope
        self._state_path = state_path
        self._binding: StateBinding | None = None
        self._help_open = False
        # Register the custom themes and apply the persisted one here, in
        # __init__, NOT in on_mount: Textual builds the App stylesheet
        # (which resolves every $var the structural CSS references) from
        # get_css_variables() before on_mount runs, and a theme's
        # variables only enter that namespace once the theme is the active
        # one. Setting the theme any later leaves the initial parse with
        # the migrated $accent/$ok/$status-* vars undefined.
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.apply_theme(_persisted_theme())

    async def on_mount(self) -> None:
        """Bind state read-only, then push the resolved scope screen.

        First paint fires as the scope screen mounts; the initial state
        load is a single synchronous file read inside
        :meth:`StateBinding.connect`, and the poll loop's first probe is
        deferred behind ``asyncio.sleep`` so it never blocks the paint.
        Themes are registered + the persisted one applied in ``__init__``
        (the App stylesheet that resolves the semantic ``$var``\\ s is
        built before ``on_mount`` runs).
        """
        self._binding = StateBinding(
            state_path=self._state_path,
            callbacks=StateBindingCallbacks(
                on_state=self._on_state,
                on_degraded=self._on_degraded,
            ),
        )
        await self._binding.connect()
        self.push_screen(self._scope)

    async def _on_state(self, new_state: State) -> None:
        """Receive a fresh state revision from the binder."""
        self.state = new_state

    async def _on_degraded(self, degraded: bool) -> None:
        """Receive a degraded-mode flip from the binder."""
        self.degraded = degraded

    def action_switch_scope(self, scope: str) -> None:
        """Switch the active scope screen (raw ``w`` / ``r`` / ``u``).

        The ``ctrl+w`` / ``ctrl+r`` / ``ctrl+u`` chords route here too as
        hidden muscle-memory aliases.

        Args:
            scope: Target scope name; must be a key of :attr:`SCREENS`.
        """
        if scope not in self.SCREENS:
            logger.warning(f"action_switch_scope unknown scope={scope!r}")
            return
        self._scope = scope  # type: ignore[assignment]
        self.switch_screen(scope)

    def apply_theme(self, logical: str) -> bool:
        """Apply an operator-facing logical theme name to the live App.

        Maps *logical* (one of ``dark`` / ``light`` / ``cb`` / ``auto``)
        onto the registered Textual theme name via
        :func:`~eawf.tui_v2.theme.resolve_theme_name` and assigns it to the
        reactive :attr:`theme`, which re-resolves every semantic ``$var``
        the structural CSS references. An unrecognised name leaves the
        theme unchanged and returns ``False`` so callers can surface a
        rejection.

        Args:
            logical: The operator-facing logical theme name.

        Returns:
            ``True`` when a known logical name was applied, ``False`` when
            *logical* was unrecognised (no theme change).
        """
        registered = resolve_theme_name(logical)
        if registered is None:
            logger.info(f"apply_theme rejected logical={logical!r}")
            return False
        self.theme = registered
        logger.info(f"apply_theme logical={logical!r} theme={registered!r}")
        return True

    def modal_depth(self) -> int:
        """Return the number of :class:`ModalScreen` overlays on the stack.

        Scope screens are plain :class:`~textual.screen.Screen` subclasses,
        so only the stacked overlays (palette / detail / confirm / help /
        later-wave overlays) count toward the cap.

        Returns:
            The current modal-overlay depth.
        """
        return sum(1 for screen in self.screen_stack if isinstance(screen, ModalScreen))

    def push_modal(self, modal: ModalScreen[Any]) -> bool:
        """Push *modal* unless the stack is already at :attr:`MAX_MODAL_DEPTH`.

        The single modal-stack-cap gate: every overlay-opening path (the
        ``/`` palette, the ``?`` help, the row-drill DetailModal, the
        destructive ConfirmModal, and the later-wave overlays) routes
        through here so the depth limit is enforced in exactly one place.
        A rejected push toasts and mutates nothing.

        Args:
            modal: The overlay screen to push.

        Returns:
            ``True`` when the modal was pushed, ``False`` when the cap
            rejected it.
        """
        if self.modal_depth() >= self.MAX_MODAL_DEPTH:
            logger.info(
                f"push_modal rejected depth={self.modal_depth()} cap={self.MAX_MODAL_DEPTH}"
            )
            self.notify(
                f"modal stack depth limit ({self.MAX_MODAL_DEPTH}) reached — close one first",
                severity="warning",
            )
            return False
        self.push_screen(modal)
        return True

    def action_open_palette(self) -> None:
        """Open the ``/`` command palette (cap-checked).

        Exposed on the App as well as the scope screen so the palette can
        be opened from any focus context; routes through
        :meth:`push_modal` for the depth cap.
        """
        from eawf.tui_v2.palette.command_palette import CommandPalette

        self.push_modal(CommandPalette())

    def action_open_config(self) -> None:
        """Open the ``c`` registry-driven config window (cap-checked).

        Exposed on the App so the ``c`` binding (declared on the repo
        scope screen) and the ``/config`` palette verb open the same
        window through the modal-cap-aware
        :func:`~eawf.tui_v2.screens.overlays.config_modal.open_config`.
        """
        from eawf.tui_v2.screens.overlays.config_modal import open_config

        open_config(self)

    def action_open_help(self) -> None:
        """Open the ``?`` help overlay (cap-checked, single-instance).

        A second ``?`` (or ``/help``) while the help overlay is already
        open is a no-op — the :attr:`_help_open` guard suppresses
        the duplicate push so the operator cannot exhaust the stack cap by
        holding ``?``. The guard clears when the overlay dismisses.
        """
        from eawf.tui_v2.screens.help import HelpScreen

        if self._help_open:
            return
        if self.push_modal(HelpScreen()):
            self._help_open = True

    def _on_help_closed(self) -> None:
        """Clear the help-open guard when the help overlay dismisses."""
        self._help_open = False

    async def on_unmount(self) -> None:
        """Tear the read-only binder down on app exit."""
        if self._binding is not None:
            await self._binding.disconnect()


def run_app(scope: ScopeName, state_path: Path | None) -> int:
    """Launch the interactive :class:`EaApp` for *scope*.

    Blocks until the operator quits. Intended for the TTY-interactive
    branch of the CLI bare-command / ``tui`` dispatch; the non-TTY /
    ``--plain`` / ``--no-input`` branch uses the deterministic status
    fallback instead.

    Args:
        scope: Resolved scope name.
        state_path: Path to the scope's ``state.json`` (read-only).

    Returns:
        Process exit code (``0`` on a clean quit).
    """
    EaApp(scope=scope, state_path=state_path).run()
    return 0


def resolve_scope(scope_kind: ScopeKind) -> ScopeName:
    """Map a state ``scope_kind`` onto an :class:`EaApp` scope name.

    Args:
        scope_kind: The ``scope_kind`` read from a resolved ``state.json``.

    Returns:
        ``"repo"`` or ``"workspace"`` — the matching App scope name.
    """
    if scope_kind is ScopeKind.WORKSPACE:
        return "workspace"
    return "repo"


__all__ = [
    "BRAND",
    "DEFAULT_PROJECT_CODE",
    "EaApp",
    "Header",
    "RepoScreen",
    "ScopeName",
    "UserScreen",
    "WorkspaceScreen",
    "build_breadcrumb",
    "resolve_scope",
    "run_app",
]
