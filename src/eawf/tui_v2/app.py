"""Textual ``EaApp`` — the C06 operator-surface entry point (tui_v2).

This is the START of the C06 TUI rebuild. Per the operator decision of
2026-05-18 the v0.3+ TUI is **Textual** (reversing the prior P14 ``rich``
pick); the legacy ``src/eawf/tui/`` tree stays untouched until its
cutover is ratified in a later wave. This module ships the App shell:

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

Scope dispatch (per C06 §5.2 / Decision D10) lives in the CLI bare-
command handler (:mod:`eawf.cli.app`); this module receives the already-
resolved ``scope`` + ``state_path`` and renders the matching screen.

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

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Footer, Static

from eawf.state.enums import ScopeKind
from eawf.state.models import State
from eawf.tui_v2.state_binding import StateBinding, StateBindingCallbacks

logger = logging.getLogger(__name__)

#: Literal scope kinds the App can launch into. ``repo`` / ``workspace``
#: mirror :class:`eawf.state.enums.ScopeKind`; ``user`` is the registry-
#: scoped portfolio view that has no ``state.json`` ``scope_kind`` of its
#: own (resolved from a populated ``~/.eawf/registry.json`` per D10).
ScopeName = Literal["repo", "workspace", "user"]

#: The brand string rendered outside-left of the scope breadcrumb.
#: Literal ``Eä`` (capital E + a-umlaut) per the operator branding
#: convention; bold-accent styling is applied via ``theme.tcss``.
BRAND: str = "Eä"

#: Fallback project code shown in the breadcrumb when no state is loaded
#: yet (fresh workspace / daemon cold-spawn placeholder).
DEFAULT_PROJECT_CODE: str = "EAWF"


def _breadcrumb(state: State | None) -> str:
    """Build the ``scope / code / phase`` breadcrumb from typed state.

    Falls back to :data:`DEFAULT_PROJECT_CODE` when no state is loaded so
    the header stays informative during the daemon cold-spawn window. The
    separator matches the legacy surface (:func:`eawf.tui.layout.build_breadcrumb`)
    so the brand-left placement is the only branding the rebuild changes.

    Args:
        state: The currently bound state, or ``None`` before first load.

    Returns:
        The breadcrumb string (without the brand prefix).
    """
    if state is None:
        return DEFAULT_PROJECT_CODE
    code = state.project.code if state.project is not None else DEFAULT_PROJECT_CODE
    parts: list[str] = [state.scope_kind.value, code]
    if state.current.phase_id is not None:
        parts.append(state.current.phase_id)
    return " / ".join(parts)


class _BrandHeader(Static):
    """Single-line header: ``Eä`` brand outside-left of the breadcrumb.

    Reads the App's reactive ``state`` so the breadcrumb tracks state
    changes pushed by :class:`~eawf.tui_v2.state_binding.StateBinding`.
    """

    def render(self) -> str:
        app = self.app
        state = app.state if isinstance(app, EaApp) else None
        return f"[b]{BRAND}[/b]  {_breadcrumb(state)}"


class _ScopePlaceholderScreen(Screen[None]):
    """Base scope screen — header + a labelled placeholder body + footer.

    The concrete per-scope compositions (repo 2x2 quadrant, workspace
    strip+zoom, user attention/effort/portfolio) land in later waves of
    this band. This base establishes the header / footer chrome and the
    reactive-state plumbing those waves extend.
    """

    #: Human label for the placeholder body; overridden per subclass.
    SCOPE_LABEL: ClassVar[str] = "scope"

    def compose(self) -> ComposeResult:
        yield _BrandHeader()
        yield Static(
            f"{self.SCOPE_LABEL} screen — composition lands in a follow-up wave",
            classes="scope-placeholder",
        )
        yield Footer()


class RepoScreen(_ScopePlaceholderScreen):
    """Repo-scope screen placeholder (2x2 quadrant lands later)."""

    SCOPE_LABEL: ClassVar[str] = "repo"


class WorkspaceScreen(_ScopePlaceholderScreen):
    """Workspace-scope screen placeholder (strip + zoom lands later)."""

    SCOPE_LABEL: ClassVar[str] = "workspace"


class UserScreen(_ScopePlaceholderScreen):
    """User-scope screen placeholder (portfolio sections land later)."""

    SCOPE_LABEL: ClassVar[str] = "user"


class EaApp(App[None]):
    """Single Textual app; one of three scope screens chosen on launch.

    The scope is resolved by the CLI bare-command handler (per Decision
    D10) and handed in via :paramref:`scope`; this app pushes the
    matching screen in ``on_mount`` and binds ``state.json`` into the
    reactive :attr:`state` attribute through
    :class:`~eawf.tui_v2.state_binding.StateBinding`.
    """

    CSS_PATH: ClassVar[str] = "theme.tcss"

    #: Global key bindings shared across every scope screen. Arrow keys
    #: are primary navigation; vim ``hjkl`` are registered as hidden
    #: aliases (``show=False``) so the footer advertises arrows only, per
    #: the operator keymap convention.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+r", "switch_scope('repo')", "repo", show=False),
        Binding("ctrl+w", "switch_scope('workspace')", "workspace", show=False),
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

    async def on_mount(self) -> None:
        """Bind state read-only, then push the resolved scope screen.

        First paint fires as the scope screen mounts; the initial state
        load is a single synchronous file read inside
        :meth:`StateBinding.connect`, and the poll loop's first probe is
        deferred behind ``asyncio.sleep`` so it never blocks the paint.
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
        """Switch the active scope screen (Ctrl-R / Ctrl-W / Ctrl-U).

        Args:
            scope: Target scope name; must be a key of :attr:`SCREENS`.
        """
        if scope not in self.SCREENS:
            logger.warning(f"action_switch_scope unknown scope={scope!r}")
            return
        self._scope = scope  # type: ignore[assignment]
        self.switch_screen(scope)

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
    "RepoScreen",
    "ScopeName",
    "UserScreen",
    "WorkspaceScreen",
    "resolve_scope",
    "run_app",
]
