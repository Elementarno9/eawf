"""Textual ``EaApp`` — the operator-surface entry point (tui).

The v0.3+ TUI is **Textual** (reversing the prior ``rich`` pick);
``tui`` is the sole TUI surface. This module ships the App shell:

* :class:`EaApp` — a :class:`textual.app.App` subclass that resolves to
  one of three scope screens (``repo`` / ``workspace`` / ``user``) on
  launch, loads the Textual theme, declares the global key bindings
  (arrows primary, vim keys as aliases), and binds ``state.json`` into a
  reactive attribute via :class:`~eawf.surfaces.tui.state_binding.StateBinding`.
* The three scope screens are minimal placeholders here — the concrete
  2x2 quadrant / strip+zoom / attention-effort-portfolio compositions
  land in the follow-up waves of this band. The shell establishes the
  reactive plumbing, header branding, footer keymap, and scope-dispatch
  contract those waves build on.

Scope dispatch lives in the CLI bare-command handler
(:mod:`eawf.surfaces.cli.app`); this module receives the already-resolved
``scope`` + ``state_path`` and renders the matching screen.

Branding + keymap follow the operator conventions: the header brand is
the literal ``Eä`` (capital E + a-umlaut), bold accent, positioned
outside-left of the scope breadcrumb; navigation lists arrow keys first
(``↑↓←→`` + ``PageUp`` / ``PageDown`` / ``Home`` / ``End`` / ``Enter`` /
``Esc``) with vim keys (``hjkl``) as secondary aliases only.

Performance: first paint is dominated by the screen ``compose`` + the
single read-only :func:`~eawf.surfaces.tui.state_binding.load_state` call
issued from ``on_mount``; the mtime-poll task is created but its first
probe is deferred behind ``asyncio.sleep``, so it never blocks first
paint. The placeholder screens compose a constant number of widgets,
keeping the shell well inside the <150 ms p99 first-paint budget the
W22 Pilot harness enforces.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from textual.app import App
from textual.binding import Binding, BindingType
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widget import AwaitMount
from textual.widgets import Static

from eawf.kernel.state.enums import ScopeKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.surfaces.render.link_wrap import REFERENCE_KINDS
from eawf.surfaces.tui.modes import (
    DEFAULT_MODE,
    NavPosition,
    NavState,
    build_modes,
    mode_bindings,
    mode_title,
)
from eawf.surfaces.tui.poc_defects import poc_defects_enabled
from eawf.surfaces.tui.scopes import RepoScreen, UserScreen, WorkspaceScreen
from eawf.surfaces.tui.screens.overlays.reference import (
    ReferenceModal,
    ReferenceTarget,
    resolve_reference,
)
from eawf.surfaces.tui.state_binding import StateBinding, StateBindingCallbacks
from eawf.surfaces.tui.theme import (
    DEFAULT_THEME,
    EA_THEMES,
    THEME_POLL_INTERVAL_S,
    detect_auto_theme,
    detect_os_appearance,
    resolve_theme_name,
)
from eawf.surfaces.tui.toast_emitter import ToastEmitter
from eawf.surfaces.tui.widgets.eu_bar import EUBar, RenderMode
from eawf.surfaces.tui.widgets.header import (
    BRAND,
    DEFAULT_PROJECT_CODE,
    Header,
    build_breadcrumb,
)

if TYPE_CHECKING:
    from eawf.surfaces.tui.modes.feed import FeedListener

logger = logging.getLogger(__name__)

DEGRADED_BANNER_ID = "degraded-banner"
DEGRADED_BANNER_HIDDEN_CLASS = "degraded-banner--hidden"

#: Cap on the App-owned live event ring buffer. The Feed pane renders this
#: buffer newest-first; the bound mirrors the on-disk ``/events`` overlay
#: ring (50) with headroom so a burst of pushes between mode switches stays
#: visible. Oldest envelopes drop off the tail once the cap is reached.
LIVE_EVENT_BUFFER_MAX: int = 200

#: Literal scope kinds the App can launch into. ``repo`` / ``workspace``
#: mirror :class:`eawf.kernel.state.enums.ScopeKind`; ``user`` is the registry-
#: scoped portfolio view that has no ``state.json`` ``scope_kind`` of its
#: own (resolved from a populated ``~/.eawf/registry.json``).
ScopeName = Literal["repo", "workspace", "user"]

#: Backwards-compatible alias for the breadcrumb builder. The canonical
#: definition (with the ``BRAND`` / ``DEFAULT_PROJECT_CODE`` constants)
#: lives in :mod:`eawf.surfaces.tui.widgets.header` so the shared
#: :class:`~eawf.surfaces.tui.widgets.header.Header` and this module share one
#: source (DRY); this alias keeps the scaffold-test import path stable.
_breadcrumb = build_breadcrumb


def _persisted_theme() -> str:
    """Read the persisted ``ui.theme`` logical name from layered config.

    Reads through the same :func:`~eawf.kernel.config.layered.merge_config` path
    the config window writes through, so a value the operator saved via
    ``/config`` (or ``eawf config set ui.theme ...``) is honoured on the
    next launch. A missing key, an unreadable layer, or a value that is
    not a recognised logical name all degrade to :data:`DEFAULT_THEME` —
    the swap is a cosmetic preference, never a launch-blocking read.

    Returns:
        The persisted logical theme name, or :data:`DEFAULT_THEME` when
        none is persisted / the persisted value is unrecognised.
    """
    from eawf.kernel.config.layered import get_dotted, merge_config

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


#: Recognised values for the ``ui.glyphs`` leaf key. ``auto`` resolves
#: via the Braille coverage probe; ``ascii`` / ``unicode`` are explicit
#: operator overrides.
_GLYPHS_DEFAULT = "auto"
_GLYPHS_CHOICES = ("auto", "ascii", "unicode")


def _persisted_glyphs() -> str:
    """Read the persisted ``ui.glyphs`` policy from layered config.

    Reads through the same :func:`~eawf.kernel.config.layered.merge_config` path
    the config window writes through, so an operator-set value is honoured
    on launch. A missing key, an unreadable layer, or an unrecognised
    value degrades to ``"auto"`` — glyph selection is cosmetic, never a
    launch-blocking read.

    Returns:
        One of ``"auto"`` / ``"ascii"`` / ``"unicode"``; ``"auto"`` when
        none is persisted or the persisted value is unrecognised.
    """
    from eawf.kernel.config.layered import get_dotted, merge_config

    try:
        merged, _sources = merge_config()
        value = get_dotted(merged, "ui.glyphs")
    except (KeyError, OSError, ValueError) as exc:
        logger.debug(f"_persisted_glyphs fallback exc={exc!r}")
        return _GLYPHS_DEFAULT
    if isinstance(value, str) and value in _GLYPHS_CHOICES:
        return value
    logger.debug(f"_persisted_glyphs unrecognised value={value!r}")
    return _GLYPHS_DEFAULT


def probe_braille_coverage() -> bool:
    """Probe whether the active terminal/font covers Braille Patterns.

    There is no portable, side-effect-free way to query a terminal's
    glyph coverage, so this uses the conventional opt-out signal: the
    ``FONT_NO_BRAILLE`` environment variable. When it is set (to any
    non-empty value) the probe reports no coverage and the app falls back
    to ASCII; otherwise Braille is assumed available (the common case for
    modern terminals + Nerd Fonts). Operators on a Braille-less font set
    ``FONT_NO_BRAILLE=1`` (or ``ui.glyphs=ascii`` for a persisted
    override).

    Returns:
        ``True`` when Braille Patterns are assumed renderable, ``False``
        when the ``FONT_NO_BRAILLE`` opt-out is set.
    """
    return not os.environ.get("FONT_NO_BRAILLE")


def resolve_render_mode(glyphs: str, *, braille_ok: bool) -> RenderMode:
    """Resolve the bar render mode from the glyph policy + coverage probe.

    Args:
        glyphs: The ``ui.glyphs`` policy (``"auto"`` / ``"ascii"`` /
            ``"unicode"``).
        braille_ok: The :func:`probe_braille_coverage` verdict.

    Returns:
        ``"ascii"`` when ``glyphs == "ascii"`` or the coverage probe
        failed; ``"braille"`` when ``glyphs`` resolves to unicode and the
        probe passed.
    """
    if glyphs == "ascii":
        return "ascii"
    if glyphs == "unicode":
        return "braille" if braille_ok else "ascii"
    # auto: braille when the coverage probe passes, else ascii.
    return "braille" if braille_ok else "ascii"


class EaApp(App[None]):
    """Single Textual app; one of three scope screens chosen on launch.

    The scope is resolved by the CLI bare-command handler and handed in
    via :paramref:`scope`; this app pushes the matching screen in
    ``on_mount`` and binds ``state.json`` into the
    reactive :attr:`state` attribute through
    :class:`~eawf.surfaces.tui.state_binding.StateBinding`.
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
    #: compat. Digit keys ``1``..``6`` are the orthogonal mode axis
    #: (Home / Trust / Doctor / ...), appended from the mode registry via
    #: :func:`~eawf.surfaces.tui.modes.mode_bindings`. Arrow keys are primary
    #: navigation; vim ``hjkl`` are registered as hidden aliases
    #: (``show=False``) so the footer advertises arrows only, per the
    #: operator keymap convention.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("w", "switch_scope('workspace')", "workspace", show=False),
        Binding("r", "switch_scope('repo')", "repo", show=False),
        Binding("u", "switch_scope('user')", "user", show=False),
        Binding("ctrl+w", "switch_scope('workspace')", "workspace", show=False),
        Binding("ctrl+r", "switch_scope('repo')", "repo", show=False),
        Binding("ctrl+u", "switch_scope('user')", "user", show=False),
        Binding("i", "open_inbox", "inbox", show=False),
        Binding("q", "quit", "quit"),
        Binding("escape", "quit", "quit", show=False),
        # Vim-key aliases for navigation — secondary to the arrows the
        # individual screens bind; declared here so they resolve app-wide.
        Binding("h", "cursor_left", "left", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("l", "cursor_right", "right", show=False),
        Binding("alt+left", "reference_back", "ref back", show=False),
        Binding("alt+right", "reference_forward", "ref forward", show=False),
        *mode_bindings(),
    ]

    #: Native Textual mode map (mode name -> base-screen factory). Declared
    #: class-side (empty) so the type is valid and ``DEFAULT_MODE`` resolves;
    #: the instance replaces ``self._modes`` with app-bound factories in
    #: ``__init__`` (so the Home mode can read the resolved ``_scope``). The
    #: mode set lives in :mod:`eawf.surfaces.tui.modes.registry` -- the one
    #: seam the per-pane waves extend.
    MODES: ClassVar[dict[str, str | Callable[[], Screen[Any]]]] = {}

    #: The launch mode -- the chassis boots into Home (the scope-bearing
    #: mode). Overrides Textual's ``"_default"`` so ``on_mount`` -> run
    #: auto-initialises the Home mode's screen stack with no explicit
    #: ``push_screen``.
    DEFAULT_MODE: ClassVar[str] = DEFAULT_MODE

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

    #: Count of open needs_user pauses across **all** scopes (not just the
    #: active one). The footer needs_user badge watches this to flip
    #: attention-coloured when > 0. Recomputed on every state refresh off
    #: the same pause source the auto-open path reads. ``init=False`` so the
    #: watcher only fires on a real change.
    pending_pauses: reactive[int] = reactive(0, init=False)

    #: Active bar fill mode (Braille dot-matrix vs ASCII ``#``/``-``).
    #: Seeded ``braille`` per the operator pick; flipped to ``ascii`` in
    #: ``on_mount`` when the Braille coverage probe fails
    #: (``FONT_NO_BRAILLE``) or ``ui.glyphs=ascii`` is persisted. A flip
    #: is watched (:meth:`watch_render_mode`) so every mounted bar
    #: rerenders in the other glyph set in one pass.
    render_mode: reactive[RenderMode] = reactive[RenderMode]("braille", init=False)

    #: Focused repo root published by the workspace / user zoom lifecycle.
    #: ``r`` uses this while zoomed so repo-scope rebinding follows the
    #: selected row instead of falling back to the launch ``state.json``.
    _active_repo_path: reactive[Path | None] = reactive(None, init=False)

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
        # Replace Textual's working copy of the class-level (empty) MODES
        # with app-bound factories from the mode registry. The factories
        # close over ``self`` so the Home mode reads the resolved ``_scope``
        # to pick its scope screen; ``_scope`` is set above, before this.
        # ``DEFAULT_MODE`` ("home") was seeded into ``_current_mode`` +
        # ``_screen_stacks`` by ``super().__init__()``, so the launch mode is
        # already Home -- run() initialises its stack with no push_screen.
        # The cast bridges the registry's ``() -> Screen | str`` factory type
        # to Textual's ``str | (() -> Screen)`` MODES value type: both are
        # valid MODES values that ``_init_mode`` resolves identically (it
        # calls a callable then passes the Screen-or-name to ``_get_screen``),
        # but mypy cannot unify the invariant dict value types.
        self._modes = cast("dict[str, str | Callable[[], Screen[Any]]]", build_modes(self))
        # The bound SCOPE x MODE navigation state machine. The launch
        # position is (resolved scope, DEFAULT_MODE) -- always legal, since
        # the Home launch mode is scope-agnostic. Every scope / mode switch
        # consults this validator before swapping the screen, so an illegal
        # (scope, mode) corner (user x {trust, evidence, feed}) is rejected
        # at the boundary rather than landing in a sourceless view.
        self._nav: NavState = NavState.initial(scope, DEFAULT_MODE)
        self._state_path = state_path
        self._binding: StateBinding | None = None
        self._help_open = False
        self._needs_user_open = False
        self._init_wizard_open = False
        self._init_wizard_auto_opened = False
        self._toast_emitter = ToastEmitter()
        # Live event ring buffer fed by the daemon ``event.subscribe`` push
        # stream (via the read-only binding's ``on_event`` hook). The Feed
        # mode pane renders this newest-first; appends here run on the event
        # loop thread (the binding marshals pushes back via
        # ``run_coroutine_threadsafe``), so no extra lock is needed. Bounded
        # at LIVE_EVENT_BUFFER_MAX so an idle session never grows unbounded.
        self._live_event_buffer: deque[Envelope] = deque(maxlen=LIVE_EVENT_BUFFER_MAX)
        # Mounted panes that want each live envelope pushed to them as it
        # arrives -- the live Feed pane and the agent-watch zoom (which
        # filters the same stream to one session). Each registers on mount +
        # seeds from the buffer, and unregisters on unmount, so the fan-out
        # never targets a torn-down screen. Typed against the structural
        # FeedListener protocol so a second consumer registers through the
        # same seam without a concrete-class union. List (not set) because the
        # screens are not hashable-stable across Textual's lifecycle; the
        # membership churn is tiny.
        self._feed_listeners: list[FeedListener] = []
        self._last_state: State | None = None
        self._last_open_pause_count = 0
        # Session-level dismissed attention rows: the explicit acknowledge
        # set the live attention reducer is filtered against (the band +
        # inbox add a row's ``dismiss_key`` here on ``d``). Session-scoped on
        # purpose -- a dismiss clears on restart (persisted dismiss is YAGNI);
        # the live reducer already auto-clears a row when its source resolves.
        self._attention_dismissed: set[str] = set()
        self._reference_back_stack: list[ReferenceTarget] = []
        self._reference_forward_stack: list[ReferenceTarget] = []
        self._current_reference: ReferenceTarget | None = None
        # The persisted ``ui.glyphs`` policy (auto/ascii/unicode). Read
        # once here, off the same layered-config path /config writes; the
        # MOUNT-time coverage probe combines it with FONT_NO_BRAILLE to
        # resolve the initial render_mode. Cosmetic, never launch-blocking.
        self._glyphs_policy: str = _persisted_glyphs()
        # Detect the auto theme's terminal background ONCE here, before
        # .run() captures stdin: the OSC 11 query needs the live TTY, and
        # Textual's input parser owns stdin for the whole run — a mid-run
        # query would corrupt the screen. Cache the dark/light verdict so a
        # later /theme auto (or the persisted "auto" applied below) reuses it
        # with no further TTY access. Under run_test()/non-TTY this returns
        # the dark baseline, keeping the TUI tests deterministic.
        self._auto_logical: str = detect_auto_theme()
        # The operator-facing logical theme name currently in force (``dark``
        # / ``light`` / ``cb`` / ``auto``); the live appearance poll only acts
        # while this is ``auto``. Set authoritatively by apply_theme below.
        self._logical_theme: str = DEFAULT_THEME
        # Baseline for the live OS light/dark watch (:meth:`_poll_os_appearance`).
        # ``None`` until the first poll seeds it, so the startup OSC 11 verdict
        # in ``_auto_logical`` stands until the OS appearance actually flips.
        self._os_appearance: str | None = None
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
        """Bind state read-only; the default (Home) mode auto-mounts.

        The chassis launches into the Home mode (``DEFAULT_MODE``); Textual
        auto-initialises that mode's screen stack on run -- the Home
        factory builds the resolved scope screen -- so there is no explicit
        ``push_screen`` here. First paint fires as that scope screen
        mounts; the initial state load is a single synchronous file read
        inside :meth:`StateBinding.connect`, and the poll loop's first
        probe is deferred behind ``asyncio.sleep`` so it never blocks the
        paint. Themes are registered + the persisted one applied in
        ``__init__`` (the App stylesheet that resolves the semantic
        ``$var``\\ s is built before ``on_mount`` runs).
        """
        self._binding = StateBinding(
            state_path=self._state_path,
            callbacks=StateBindingCallbacks(
                on_state=self._on_state,
                on_degraded=self._on_degraded,
                on_event=self._on_event,
            ),
        )
        await self._binding.connect()
        # The user (portfolio) scope has no on-disk state.json to bind, so
        # the binder leaves self.state None. Synthesize a workspace-shaped
        # state from the global registry here (read-only) so the portfolio
        # table renders one row per registered repo before first paint.
        if self._scope == "user" and self.state is None:
            from eawf.surfaces.tui.scopes.user import synthesize_user_state

            self.state = synthesize_user_state()
        # Probe Braille coverage and resolve the bar fill mode before the
        # scope screen composes, so the first paint already carries the
        # right glyph set (no flicker from an after-mount flip). The
        # ``init=False`` reactive only fires its watcher on a real change,
        # so seeding ``ascii`` here when the probe fails rerenders cleanly.
        self.render_mode = resolve_render_mode(
            self._glyphs_policy, braille_ok=probe_braille_coverage()
        )
        self.call_after_refresh(self._sync_degraded_banner)
        self._maybe_open_init_wizard()
        # Follow a live system light/dark flip for /theme auto. The OSC 11
        # background probe can only run before .run() captured stdin, so the
        # running App tracks the system theme by polling the OS appearance
        # setting off-thread (no stdin contention). First probe is deferred a
        # full interval, so it never competes with first paint.
        self.set_interval(THEME_POLL_INTERVAL_S, self._poll_os_appearance)

    async def _on_state(self, new_state: State) -> None:
        """Receive a fresh state revision from the binder.

        Each refresh also drives the needs_user auto-open check: when the
        event store surfaces an unresolved pause for the active scope and
        no needs_user modal is already open, the modal is auto-opened so
        the operator can answer the paused question. Detection rides the
        :class:`~eawf.surfaces.tui.state_binding.StateBinding` refresh because
        there is no daemon push bus yet.
        """
        pauses = self._open_pauses_for_state(new_state)
        open_pause_count = len(pauses)
        self._toast_emitter.emit(
            cast(Any, self),
            self._last_state,
            new_state,
            prev_open_pause_count=self._last_open_pause_count,
            open_pause_count=open_pause_count,
        )
        self.state = new_state
        self._last_state = new_state
        self._last_open_pause_count = open_pause_count
        self.pending_pauses = len(self._all_open_pauses())
        self._maybe_open_needs_user(new_state, pauses=pauses)
        self._maybe_open_init_wizard(new_state)

    async def _on_event(self, envelope: Envelope) -> None:
        """Receive live daemon event envelopes from the binding.

        Two consumers ride this hook: the toast / needs_user logic still
        recomputes from the durable stores on the matching state refresh,
        and the live Feed pane subscribes off this seam. Each push is
        appended to the App-owned ring buffer (:attr:`_live_event_buffer`)
        and fanned out to every mounted Feed pane so the pane shows live
        events without opening a second daemon subscription. This runs on
        the event-loop thread (the binding marshals pushes back via
        ``run_coroutine_threadsafe``), so the deque append and the
        listener fan-out need no extra lock.

        Args:
            envelope: The live event envelope pushed by the daemon.
        """
        logger.debug(f"_on_event id={envelope.id!r} kind={envelope.kind.value!r}")
        self._live_event_buffer.append(envelope)
        for listener in list(self._feed_listeners):
            listener.append_event(envelope)

    @property
    def live_event_buffer(self) -> tuple[Envelope, ...]:
        """Return a snapshot of the live event buffer, oldest-first.

        The Feed pane seeds its scroll from this on mount so a mode switch
        into Feed mid-session shows the events that arrived before the pane
        existed. A tuple copy keeps the caller from mutating the deque.

        Returns:
            The buffered live envelopes in arrival order (oldest first).
        """
        return tuple(self._live_event_buffer)

    @property
    def nav_position(self) -> NavPosition:
        """Return the current bound SCOPE x MODE navigation position.

        The single source of truth for where the operator is in the
        ``(scope, mode)`` matrix. The header reads this so the breadcrumb
        renders the bound nav position rather than reconstructing it from the
        separate ``_scope`` / ``current_mode`` fields (which the nav wiring
        keeps in lockstep with this position on every accepted switch).

        Returns:
            The current :class:`~eawf.surfaces.tui.modes.NavPosition`.
        """
        return self._nav.position

    def register_feed_listener(self, listener: FeedListener) -> None:
        """Register *listener* to receive each live envelope on arrival.

        Idempotent: a double-register (e.g. a remount) does not duplicate
        the fan-out target. Called from the live Feed pane
        (:meth:`~eawf.surfaces.tui.modes.feed.FeedModeScreen.on_mount`) and the
        agent-watch zoom, both of which satisfy the structural
        :class:`~eawf.surfaces.tui.modes.feed.FeedListener` contract.

        Args:
            listener: The pane to push live envelopes to.
        """
        if listener not in self._feed_listeners:
            self._feed_listeners.append(listener)
            logger.debug(f"register_feed_listener count={len(self._feed_listeners)}")

    def unregister_feed_listener(self, listener: FeedListener) -> None:
        """Unregister *listener* so the fan-out skips a torn-down pane.

        A no-op when the listener was never registered (defensive against a
        double-unmount). Called from the live Feed pane
        (:meth:`~eawf.surfaces.tui.modes.feed.FeedModeScreen.on_unmount`) and the
        agent-watch zoom on unmount.

        Args:
            listener: The pane to stop pushing live envelopes to.
        """
        if listener in self._feed_listeners:
            self._feed_listeners.remove(listener)
            logger.debug(f"unregister_feed_listener count={len(self._feed_listeners)}")

    def _open_pauses_for_state(self, state: State) -> list[Any]:
        """Return open pauses for *state*; degrade to empty on read errors."""
        if self._state_path is None:
            return []
        from eawf.workflow.skills.needs_user import list_open_pauses

        try:
            return list(list_open_pauses(self._state_path, scope_id=state.urn))
        except OSError as exc:
            logger.debug(f"_open_pauses_for_state list failed cause={exc!r}")
            return []

    def _all_open_pauses(self) -> list[Any]:
        """Return every open pause across all scopes; empty on read errors.

        The cross-scope counterpart to :meth:`_open_pauses_for_state` (which
        filters to the active scope for the auto-open). Feeds both the
        footer :attr:`pending_pauses` badge count and the global inbox
        overlay, so the badge and the inbox always agree on the same set.
        """
        if self._state_path is None:
            return []
        from eawf.workflow.skills.needs_user import list_open_pauses

        try:
            return list(list_open_pauses(self._state_path, scope_id=None))
        except OSError as exc:
            logger.debug(f"_all_open_pauses list failed cause={exc!r}")
            return []

    def _attention_now(self) -> datetime:
        """Return the reference instant for attention-row time-ago labels.

        A single seam so the band + inbox measure relative time off one
        clock; a deterministic harness overrides it for stable goldens.
        """
        return datetime.now(UTC)

    def attention_dismissed(self) -> frozenset[str]:
        """Return the session-dismissed attention keys (read accessor)."""
        return frozenset(self._attention_dismissed)

    def dismiss_attention(self, dismiss_key: str) -> None:
        """Acknowledge an attention row this session so the reducer drops it.

        Adds *dismiss_key* (an :attr:`~eawf.surfaces.tui.attention.AttentionItem.dismiss_key`)
        to the session set, then rebuilds every mounted attention band so the
        row disappears immediately. Session-scoped -- the set clears on
        restart -- because the live reducer already auto-clears a row when
        its source resolves; an explicit dismiss only hides a still-live row.

        Args:
            dismiss_key: The stable per-row key to suppress this session.
        """
        if dismiss_key in self._attention_dismissed:
            return
        self._attention_dismissed.add(dismiss_key)
        logger.info(f"dismiss_attention key={dismiss_key!r} total={len(self._attention_dismissed)}")
        from eawf.surfaces.tui.widgets.attention_feed import AttentionFeed

        for band in self.query(AttentionFeed):
            band.rebuild()

    def _portfolio_attention_feed(self, dismissed: frozenset[str]) -> tuple[Any, ...]:
        """Aggregate open attentions across the registered repos, ranked.

        The user / portfolio scope has no single bound ``state.json``, so its
        band spans the explicitly registered repos: resolve them through the
        W24 registry boundary (:func:`~eawf.platform.registry.read_registry` --
        never a filesystem scan), load each repo's ``state.json`` read-only
        (degrading per-repo on an unreadable state), and merge the per-repo
        attention reductions into one ranked feed tagged by repo. A missing or
        corrupt registry yields an empty feed (honest-empty band).

        Args:
            dismissed: The session-dismissed keys to filter the merged feed
                against.

        Returns:
            The ranked cross-repo attention items, most-urgent first.
        """
        from eawf.platform.registry import RegistryReadError, read_registry
        from eawf.surfaces.tui.attention import build_portfolio_attention_feed
        from eawf.surfaces.tui.state_binding import load_state

        try:
            registry = read_registry()
        except RegistryReadError as exc:
            logger.debug(f"_portfolio_attention_feed registry unavailable cause={exc!r}")
            return ()

        def _repo_state(repo_root: Path) -> State | None:
            return load_state(repo_root / ".ea" / "state.json")

        return build_portfolio_attention_feed(
            registry.repos.values(),
            load_state=_repo_state,
            dismissed=dismissed,
        )

    def _maybe_open_needs_user(
        self,
        state: State,
        *,
        pauses: list[Any] | None = None,
    ) -> None:
        """Auto-open the needs_user modal for the active scope's oldest pause.

        Reads the event store (read-only) for unresolved pauses whose
        ``scope_id`` matches ``state.urn``. The single-instance guard
        (:attr:`_needs_user_open`) prevents a second auto-open while one is
        already on the stack; the guard clears when the modal dismisses.

        Args:
            state: The freshly loaded state — its ``urn`` is the canonical
                scope id pause records carry.
        """
        if self._needs_user_open or self._state_path is None:
            return
        if pauses is None:
            pauses = self._open_pauses_for_state(state)
        if not pauses:
            return
        pause = pauses[0]
        # Set the single-instance guard at schedule time so a second
        # refresh arriving before the deferred push runs does not queue a
        # duplicate. Defer the push to the next refresh so the scope screen
        # (pushed synchronously after the binder connects in on_mount) is
        # already on the stack — otherwise the initial-load modal would be
        # buried under the scope screen pushed right after it.
        self._needs_user_open = True
        self.call_after_refresh(self._open_needs_user_pause, pause.pause_urn, pause.question)

    def open_needs_user_pause(self, pause_urn: str, question: object) -> bool:
        """Push the needs_user modal for *pause_urn* with the pick handler.

        The single push path shared by the auto-open (via
        :meth:`_open_needs_user_pause`) and the global inbox overlay, so a
        pick from either surface routes through the same
        :meth:`_on_needs_user_picked` resume + single-instance guard. Sets
        the guard before the push and clears it when the modal-stack cap
        rejects the push so a later open can retry.

        Args:
            pause_urn: The pause the modal answers.
            question: The :class:`~eawf.workflow.skills.bodies.user_question.UserQuestion`
                to render (typed loosely to avoid an import cycle).

        Returns:
            ``True`` when the modal was pushed, ``False`` when the cap
            rejected it.
        """
        from eawf.surfaces.tui.screens.overlays.needs_user import NeedsUserModal

        self._needs_user_open = True
        modal = NeedsUserModal(question)  # type: ignore[arg-type]
        pushed = self.push_modal(
            modal,
            callback=lambda label: self._on_needs_user_picked(pause_urn, label),
        )
        if not pushed:
            self._needs_user_open = False
        return pushed

    def _open_needs_user_pause(self, pause_urn: str, question: object) -> None:
        """Push the auto-open needs_user modal for *pause_urn*.

        Thin ``call_after_refresh`` target for the auto-open path; delegates
        to the shared :meth:`open_needs_user_pause`. The guard is already
        set eagerly at schedule time in :meth:`_maybe_open_needs_user`;
        :meth:`open_needs_user_pause` re-sets it idempotently and clears it
        when the cap rejected the push so a later refresh can retry.

        Args:
            pause_urn: The pause the modal answers.
            question: The :class:`~eawf.workflow.skills.bodies.user_question.UserQuestion`
                to render (typed loosely to avoid an import cycle).
        """
        self.open_needs_user_pause(pause_urn, question)

    def _on_needs_user_picked(self, pause_urn: str, label: str | None) -> None:
        """Resolve *pause_urn* with the picked *label*, or clear on defer.

        ``label is None`` means the operator pressed ``Esc`` (defer): the
        pause stays open and the guard clears so a later refresh can
        re-open it. A non-None label routes through the shared resume
        library function; a resume failure surfaces an error toast and
        leaves the pause open for a retry.

        Args:
            pause_urn: The pause being answered.
            label: The chosen option label, or ``None`` on defer.
        """
        self._needs_user_open = False
        if label is None or self._state_path is None:
            return
        from eawf.workflow.skills.needs_user import PauseError

        try:
            self._resolve_needs_user_pause(pause_urn=pause_urn, choice=label)
        except (PauseError, OSError, ValueError) as exc:
            logger.info(f"_on_needs_user_picked resume_failed pause_urn={pause_urn!r} err={exc!r}")
            self.notify(f"resume failed: {exc}", severity="error")

    def _resolve_needs_user_pause(self, *, pause_urn: str, choice: str) -> None:
        """Resolve a pause through daemon RPC, falling back to local helper."""
        if self._daemon_socket_available():
            from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

            try:
                with DaemonClient(call_timeout_seconds=1.0) as client:
                    client.call(
                        "needs_user.resolve",
                        {"pause_urn": pause_urn, "choice": choice},
                    )
                return
            except DaemonRpcError as exc:
                logger.debug(f"_resolve_needs_user_pause daemon_rejected message={exc.message!r}")
            except (OSError, RuntimeError, TimeoutError) as exc:
                logger.debug(f"_resolve_needs_user_pause daemon_fallback cause={exc!r}")
        from eawf.workflow.skills.needs_user import resolve_pause

        if self._state_path is None:
            raise ValueError("state_path not configured")
        resolve_pause(self._state_path, pause_urn=pause_urn, choice=choice)

    def _daemon_socket_available(self) -> bool:
        """Return whether a daemon socket is present for RPC use."""
        if os.name == "nt":
            return False
        sock_path = runtime_dir() / "eawfd.sock"
        logger.debug(
            f"_daemon_socket_available probing path={sock_path!s}"
            f" EAWF_RUNTIME_DIR={os.environ.get('EAWF_RUNTIME_DIR')!r}"
            f" XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR')!r}"
        )
        if not sock_path.exists():
            logger.debug(f"_daemon_socket_available socket_missing path={sock_path!s}")
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.25)
                probe.connect(str(sock_path))
            return True
        except OSError as exc:
            logger.debug(f"_daemon_socket_available probe failed path={sock_path!s} cause={exc!r}")
            return False

    def _degraded_banner_message(self) -> str:
        """Render the transport-warning banner text with resolved diagnostics."""
        sock_path = runtime_dir() / "eawfd.sock"
        eawf_runtime_dir = os.environ.get("EAWF_RUNTIME_DIR")
        xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        return (
            f"daemon socket unavailable; polling state.json | socket={sock_path!s} "
            f"EAWF_RUNTIME_DIR={eawf_runtime_dir or '<unset>'} "
            f"XDG_RUNTIME_DIR={xdg_runtime_dir or '<unset>'} "
            "hint: ensure daemon and TUI share the same runtime environment "
            "values for EAWF_RUNTIME_DIR / XDG_RUNTIME_DIR"
        )

    def _maybe_open_init_wizard(self, state: State | None = None) -> None:
        """Auto-open the init wizard once for an empty user-scope registry."""
        if self._scope != "user" or self._init_wizard_open or self._init_wizard_auto_opened:
            return
        if state is None:
            state = self.state
        try:
            from eawf.surfaces.tui.scopes.user import user_scope_init_needed

            init_needed = user_scope_init_needed(state)
        except Exception as exc:  # pragma: no cover - defensive import guard
            logger.debug(f"_maybe_open_init_wizard flag_unavailable err={exc!r}")
            return
        if not init_needed:
            return
        self._init_wizard_auto_opened = True
        self.call_after_refresh(self._open_init_wizard)

    def _open_init_wizard(self) -> bool:
        """Push the init wizard with the app callback and single-instance guard."""
        if self._init_wizard_open:
            return False
        from eawf.surfaces.tui.screens.overlays.init_wizard import open_init_wizard

        pushed = open_init_wizard(self, callback=self._on_init_wizard_closed)
        if pushed:
            self._init_wizard_open = True
        return pushed

    def _on_init_wizard_closed(self, result: object | None) -> None:
        """Clear the init-wizard guard and surface the chosen command plan."""
        self._init_wizard_open = False
        if result is None:
            return
        from eawf.surfaces.tui.screens.overlays.init_wizard import InitWizardResult, format_command

        if not isinstance(result, InitWizardResult):
            return
        command = format_command(result.command)
        logger.info(f"_on_init_wizard_closed action={result.action!r} command={command!r}")
        self.notify(f"init path: {command}", severity="information")

    async def _on_degraded(self, degraded: bool) -> None:
        """Receive a degraded-mode flip from the binder.

        Drives the header degraded banner and, for any mounted Feed pane
        still showing its honest-empty notice, swaps that notice between the
        live-waiting and daemon-unreachable wording so the feed never
        implies a live stream while the binding is in poll fallback.
        """
        self.degraded = degraded
        self._sync_degraded_banner()
        for listener in list(self._feed_listeners):
            listener.refresh_empty_notice()

    def _sync_degraded_banner(self) -> None:
        """Toggle banner visibility and text without remounting the widget."""
        if not self.screen_stack:
            return
        matches = self.screen.query(f"#{DEGRADED_BANNER_ID}")
        if not matches:
            self.screen.mount(
                Static(
                    "",
                    id=DEGRADED_BANNER_ID,
                    classes=f"degraded-banner {DEGRADED_BANNER_HIDDEN_CLASS}",
                )
            )
            matches = self.screen.query(f"#{DEGRADED_BANNER_ID}")
        matched = list(matches)
        if not matched:
            return
        banner = cast(Static, matched[0])
        if self.degraded:
            banner.set_class(False, DEGRADED_BANNER_HIDDEN_CLASS)
            banner.update(self._degraded_banner_message())
        else:
            banner.set_class(True, DEGRADED_BANNER_HIDDEN_CLASS)
            banner.update("")

    def watch_render_mode(self, mode: RenderMode) -> None:
        """Propagate a bar-fill-mode flip to every mounted bar.

        A single flip (Braille ↔ ASCII) rerenders the whole surface: every
        :class:`~eawf.surfaces.tui.widgets.eu_bar.EUBar` repaints in the new glyph
        set. Bars rendered as plain strings inside other widgets (the
        roadmap tree, status pane, tables) read the mode off this reactive
        on their own repaint, which the same flip schedules.

        Args:
            mode: The newly-active render mode.
        """
        logger.info(f"watch_render_mode mode={mode!r}")
        for bar in self.query(EUBar):
            bar.render_mode = mode

    def switch_mode(self, mode: str) -> AwaitMount:
        """Switch the active content mode, gated by the nav state machine.

        Overrides Textual's native ``switch_mode`` so the digit-key bindings
        **and** the ``/<mode>`` palette verbs (both route here) consult the
        bound :class:`~eawf.surfaces.tui.modes.NavState` before the mode
        flips. When the target ``(current_scope, mode)`` pair is illegal --
        the user portfolio scope crossed with a single-scope data mode
        (``trust`` / ``evidence`` / ``feed`` / ``research_board``) -- the
        switch is rejected: the app toasts the reason, logs it, and no-ops
        (returns a no-op
        :class:`~textual.widget.AwaitMount` for the current screen) rather
        than landing the operator in a sourceless view. An accepted switch
        advances the nav position, then delegates to the native
        ``switch_mode`` (which itself no-ops when already in *mode*).

        Args:
            mode: The requested target mode name.

        Returns:
            The native ``switch_mode`` await object on an accepted switch, or
            a no-op :class:`~textual.widget.AwaitMount` for the current
            screen when the nav bound rejected the target.
        """
        transition = self._nav.resolve_mode(mode)
        if not transition.accepted:
            logger.info(f"switch_mode rejected mode={mode!r} reason={transition.reason!r}")
            self.notify(f"{mode} is unavailable here: {transition.reason}", severity="warning")
            return AwaitMount(self.screen, [])
        self._nav = replace(self._nav, position=transition.position)
        return super().switch_mode(mode)

    async def action_switch_mode(self, mode: str) -> None:
        """Switch the active content mode (breadcrumb / palette click target).

        Overrides Textual's native ``action_switch_mode`` (kept ``async`` to
        match the supertype signature) so an unknown mode name resolves to a
        no-op rather than raising ``UnknownModeError`` out of the action
        handler. The native action delegates straight to ``switch_mode``, and
        this app's overridden ``switch_mode`` accepts any nav-legal pair before
        handing the name to the native ``switch_mode``, which raises on a name
        absent from the mode registry. A breadcrumb
        ``[@click=app.switch_mode('<name>')]`` link can carry a stale name
        (e.g. after a mode rename), so this guard drops the switch when the
        target is not a registered mode -- the legality gate (portfolio scope
        x single-scope mode) still lives in ``switch_mode``.

        Args:
            mode: The requested target mode name.
        """
        if mode not in self._modes:
            logger.warning(f"action_switch_mode unknown mode={mode!r}")
            return
        self.switch_mode(mode)

    def action_switch_scope(self, scope: str) -> None:
        """Switch the active scope screen (raw ``w`` / ``r`` / ``u``).

        The ``ctrl+w`` / ``ctrl+r`` / ``ctrl+u`` chords route here too as
        hidden muscle-memory aliases.

        The switch is gated by the bound :class:`~eawf.surfaces.tui.modes.NavState`:
        when the target ``(scope, current_mode)`` pair is illegal -- the
        user portfolio scope crossed with a single-scope data mode
        (``trust`` / ``evidence`` / ``feed`` / ``research_board``) -- the
        switch is rejected (the app toasts the reason, logs it, and no-ops)
        rather than landing in a sourceless view. An accepted switch advances
        the nav position.

        Every scope rebinds :attr:`state` before the screen swap so the
        target screen never renders against a stale binding. Switching to
        the user scope rebinds to a freshly synthesized portfolio state
        (read from the global registry), mirroring the ``on_mount`` launch
        path; switching to ``repo`` / ``workspace`` re-reads the launch
        ``state.json`` (read-only) so the quadrant repopulates after a
        detour through the user scope — without it the user scope's
        synthesized portfolio (``workspace`` set, no ``phases``) stayed
        bound and the repo roadmap rendered empty. Reassigning
        :attr:`state` fires the watchers that rebuild the new screen's rows.

        Args:
            scope: Target scope name; must be a key of :attr:`SCREENS`.
        """
        if scope not in self.SCREENS:
            logger.warning(f"action_switch_scope unknown scope={scope!r}")
            return
        transition = self._nav.resolve_scope(scope)
        if not transition.accepted:
            logger.info(
                f"action_switch_scope rejected scope={scope!r} mode={self.current_mode!r} "
                f"reason={transition.reason!r}"
            )
            self.notify(
                f"{mode_title(self.current_mode)} is unavailable at the {scope} scope",
                severity="warning",
            )
            return
        self._nav = replace(self._nav, position=transition.position)
        if scope == "user":
            from eawf.surfaces.tui.scopes.user import synthesize_user_state

            self.state = synthesize_user_state()
        elif scope == "repo" and self._active_repo_path is not None:
            from eawf.surfaces.tui.state_binding import load_state

            self.state = load_state(self._active_repo_path / ".ea" / "state.json")
        elif self._state_path is not None:
            from eawf.surfaces.tui.state_binding import load_state

            self.state = load_state(self._state_path)
        self._scope = scope  # type: ignore[assignment]
        self._ensure_switchable_base_screen()
        self.switch_screen(scope)
        self.call_after_refresh(self._sync_degraded_banner)
        self._maybe_open_init_wizard()

    def _ensure_switchable_base_screen(self) -> None:
        """Seed a result callback on a mode-initialised base screen.

        ``switch_screen`` pops a result callback off the outgoing screen,
        but a mode's base screen is mounted by Textual's ``_init_mode``
        (the auto-init of the active mode's stack), which appends the
        screen without the result-callback push a normal ``push_screen``
        performs. So the first in-mode scope switch would underflow the
        empty callback stack. Seeding a ``None`` callback here restores the
        invariant ``switch_screen`` relies on; it re-pushes one onto the
        incoming screen, so subsequent switches stay balanced. A no-op when
        the base screen already carries a callback (a normally-pushed
        screen, or a second switch).
        """
        screen = self.screen
        if not screen._result_callbacks:
            screen._push_result_callback(self, None)

    async def _poll_os_appearance(self) -> None:
        """Re-apply ``/theme auto`` when the OS light/dark appearance flips.

        Runs on the :data:`THEME_POLL_INTERVAL_S` interval. The OS appearance
        is read off-thread (a subprocess that never touches the App's stdin),
        so it is safe while Textual owns the terminal — unlike the OSC 11
        background probe, which can only run before ``.run()`` captured stdin.

        Acts only when the freshly-read appearance both changed since the last
        poll **and** differs from the theme currently shown, and only while
        ``auto`` is the active logical theme (an explicit ``dark`` / ``light``
        / ``cb`` pick is left untouched). When OSC 11 and the OS agree the
        first poll is a no-op; when they disagree (e.g. the startup probe fell
        back to dark on a non-answering terminal) the first poll corrects
        ``auto`` to the real appearance within one interval. An undetermined
        read (``None``) is a no-op.
        """
        appearance = await asyncio.to_thread(detect_os_appearance)
        if appearance is None or appearance == self._os_appearance:
            return
        self._os_appearance = appearance
        if self._logical_theme == "auto" and self._auto_logical != appearance:
            self._auto_logical = appearance
            self.apply_theme("auto")
            logger.info(f"_poll_os_appearance reapplied auto appearance={appearance!r}")

    def apply_theme(self, logical: str) -> bool:
        """Apply an operator-facing logical theme name to the live App.

        Maps *logical* (one of ``dark`` / ``light`` / ``cb`` / ``auto``)
        onto the registered Textual theme name and assigns it to the
        reactive :attr:`theme`, which re-resolves every semantic ``$var``
        the structural CSS references. ``auto`` resolves through the
        terminal-background verdict cached at construction
        (:attr:`_auto_logical`), so the dark/light choice reflects the real
        terminal without a mid-run TTY query; every other name resolves via
        the pure :func:`~eawf.surfaces.tui.theme.resolve_theme_name`. An
        unrecognised name leaves the theme unchanged and returns ``False`` so
        callers can surface a rejection.

        Args:
            logical: The operator-facing logical theme name.

        Returns:
            ``True`` when a known logical name was applied, ``False`` when
            *logical* was unrecognised (no theme change).
        """
        effective = self._auto_logical if logical == "auto" else logical
        registered = resolve_theme_name(effective)
        if registered is None:
            logger.info(f"apply_theme rejected logical={logical!r}")
            return False
        self.theme = registered
        self._logical_theme = logical
        logger.info(f"apply_theme logical={logical!r} effective={effective!r} theme={registered!r}")
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

    def _top_modal(self) -> ModalScreen[Any] | None:
        """Return the top-most :class:`ModalScreen` overlay, or ``None``.

        Scans from the top of the screen stack down to the first modal so a
        non-modal screen on top (none in practice -- overlays always sit
        above the scope screen) does not mask the open overlay. ``None`` when
        no modal is open.

        Returns:
            The top-most open modal overlay, or ``None``.
        """
        for screen in reversed(self.screen_stack):
            if isinstance(screen, ModalScreen):
                return screen
        return None

    def push_modal(
        self,
        modal: ModalScreen[Any],
        *,
        callback: Callable[[Any], None] | None = None,
    ) -> bool:
        """Push *modal* unless the cap is hit or it duplicates the top overlay.

        The single modal-stack gate: every overlay-opening path (the ``/``
        palette, the ``?`` help, the row-drill DetailModal, the destructive
        ConfirmModal, the needs_user picker, and the later-wave overlays)
        routes through here so both the depth limit and the singleton dedup
        are enforced in exactly one place.

        Two rejections, both leaving the stack unmutated:

        * **Depth cap** -- a push beyond :attr:`MAX_MODAL_DEPTH` toasts and
          returns ``False``.
        * **Singleton dedup** -- a modal whose class sets
          ``dedupe_singleton = True`` is rejected when the current
          top-of-stack overlay is the same class, so a re-fired open key /
          palette verb (``c`` config, ``/`` palette, ``?`` help, the inbox,
          the init wizard) cannot stack a second identical overlay. The
          dedup is **top-only**: a singleton over a *different* singleton
          still stacks, and non-singleton drill-ins (DetailModal /
          ConfirmModal) stack freely. A dedup rejection is a benign no-op
          (logged, no toast) -- the overlay the operator wanted is already
          open.

        Args:
            modal: The overlay screen to push.
            callback: Optional callback invoked with the modal's dismiss
                value when it closes (e.g. the needs_user picked label).

        Returns:
            ``True`` when the modal was pushed, ``False`` when the cap or the
            singleton dedup rejected it.
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
        if getattr(modal, "dedupe_singleton", False):
            top = self._top_modal()
            if top is not None and type(top) is type(modal):
                logger.info(f"push_modal dedup_skipped modal={type(modal).__name__!r}")
                return False
        if callback is not None:
            self.push_screen(modal, callback=callback)
        else:
            self.push_screen(modal)
        return True

    def action_open_palette(self) -> None:
        """Open the ``/`` command palette (cap-checked).

        Exposed on the App as well as the scope screen so the palette can
        be opened from any focus context; routes through
        :meth:`push_modal` for the depth cap.
        """
        from eawf.surfaces.tui.palette.command_palette import CommandPalette

        self.push_modal(CommandPalette())

    def _navigate_reference(
        self,
        ref: ReferenceTarget,
        *,
        record_history: bool,
        replace_current: bool = False,
    ) -> bool:
        """Open *ref* in a reference modal and update nav state on success."""
        card = resolve_reference(self.state, ref.kind, ref.target)
        modal = ReferenceModal(card, state=self.state)
        if replace_current and isinstance(self.screen, ReferenceModal):
            self.pop_screen()
        if not self.push_modal(modal):
            return False
        if (
            record_history
            and self._current_reference is not None
            and self._current_reference != ref
        ):
            self._reference_back_stack.append(self._current_reference)
            self._reference_forward_stack.clear()
        self._current_reference = ref
        return True

    def action_open_ref(self, kind: str, target: str) -> None:
        """Open a typed reference target from palette or action markup."""
        if kind not in REFERENCE_KINDS:
            logger.info(f"action_open_ref rejected kind={kind!r} target={target!r}")
            self.notify(f"unknown reference kind: {kind}", severity="warning")
            return
        ref = ReferenceTarget(kind, target)
        self._navigate_reference(ref, record_history=True)

    def action_open_repo_ref(self, target: str) -> None:
        """Open a repo reference."""
        self.action_open_ref("repo", target)

    def action_open_project_ref(self, target: str) -> None:
        """Open a project reference."""
        self.action_open_ref("project", target)

    def action_open_phase_ref(self, target: str) -> None:
        """Open a phase reference."""
        self.action_open_ref("phase", target)

    def action_open_iter_ref(self, target: str) -> None:
        """Open an iter reference."""
        self.action_open_ref("iter", target)

    def action_open_wave_ref(self, target: str) -> None:
        """Open a wave reference."""
        self.action_open_ref("wave", target)

    def action_open_hypothesis_ref(self, target: str) -> None:
        """Open a hypothesis reference."""
        self.action_open_ref("hypothesis", target)

    def action_open_decision_ref(self, target: str) -> None:
        """Open a decision reference."""
        self.action_open_ref("decision", target)

    def action_open_audit_ref(self, target: str) -> None:
        """Open an audit reference."""
        self.action_open_ref("audit", target)

    def action_open_artifact_ref(self, target: str) -> None:
        """Open an artifact reference."""
        self.action_open_ref("artifact", target)

    def action_open_memory_ref(self, target: str) -> None:
        """Open a memory reference."""
        self.action_open_ref("memory", target)

    def action_open_report_ref(self, target: str) -> None:
        """Open a report reference."""
        self.action_open_ref("report", target)

    def action_open_event_ref(self, target: str) -> None:
        """Open an event reference."""
        self.action_open_ref("event", target)

    def action_open_profile_ref(self, target: str) -> None:
        """Open a profile reference."""
        self.action_open_ref("profile", target)

    def action_open_spec_ref(self, target: str) -> None:
        """Open a spec reference."""
        self.action_open_ref("spec", target)

    def action_reference_back(self) -> None:
        """Navigate back through clicked reference targets."""
        if not self._reference_back_stack:
            self.notify("no reference back history", severity="information")
            return
        current = self._current_reference
        previous = self._reference_back_stack[-1]
        if self._navigate_reference(previous, record_history=False, replace_current=True):
            self._reference_back_stack.pop()
            if current is not None:
                self._reference_forward_stack.append(current)

    def action_reference_forward(self) -> None:
        """Navigate forward through clicked reference targets."""
        if not self._reference_forward_stack:
            self.notify("no reference forward history", severity="information")
            return
        current = self._current_reference
        nxt = self._reference_forward_stack[-1]
        if self._navigate_reference(nxt, record_history=False, replace_current=True):
            self._reference_forward_stack.pop()
            if current is not None:
                self._reference_back_stack.append(current)

    def action_open_config(self) -> None:
        """Open the ``c`` registry-driven config window (cap-checked).

        Exposed on the App so the ``c`` binding (declared on the repo
        scope screen) and the ``/config`` palette verb open the same
        window through the modal-cap-aware
        :func:`~eawf.surfaces.tui.screens.overlays.config_modal.open_config`.
        """
        from eawf.surfaces.tui.screens.overlays.config_modal import open_config

        open_config(self)

    def action_open_init_wizard(self) -> None:
        """Open the TUI init wizard (cap-checked, single-instance)."""
        self._open_init_wizard()

    def action_open_inbox(self) -> None:
        """Open the ``i`` global needs_user inbox overlay (cap-checked).

        Lists every open needs_user pause across all scopes ranked by
        urgency (most-immediate first); selecting a row opens that pause's
        :class:`~eawf.surfaces.tui.screens.overlays.needs_user.NeedsUserModal`
        through the shared :meth:`open_needs_user_pause`. Reads the same
        pause source the footer badge counts, so the two never disagree.
        Pauses the operator has dismissed this session are excluded (parity
        with the Home attention band). Renders honest-empty when no pause is
        open.
        """
        from eawf.surfaces.tui.screens.overlays.needs_user_inbox import (
            _pause_dismiss_key,
            open_needs_user_inbox,
            rank_pauses_by_urgency,
        )

        live = [
            pause
            for pause in self._all_open_pauses()
            if _pause_dismiss_key(pause) not in self._attention_dismissed
        ]
        ranked = rank_pauses_by_urgency(tuple(live))
        open_needs_user_inbox(self, ranked, now=self._attention_now())

    def action_open_help(self) -> None:
        """Open the ``?`` help overlay (cap-checked, single-instance).

        A second ``?`` (or ``/help``) while the help overlay is already
        open is a no-op — the :attr:`_help_open` guard suppresses
        the duplicate push so the operator cannot exhaust the stack cap by
        holding ``?``. The guard clears when the overlay dismisses.
        """
        from eawf.surfaces.tui.screens.help import HelpScreen

        if self._help_open:
            return
        if self.push_modal(HelpScreen()):
            self._help_open = True

    def _on_help_closed(self) -> None:
        """Clear the help-open guard when the help overlay dismisses."""
        self._help_open = False

    def action_poc_dead_click(self) -> None:
        """Planted dead-click defect: resolves but does nothing observable.

        Armed only behind the :data:`~eawf.surfaces.tui.poc_defects.POC_DEFECTS_ENV`
        build flag (a W10 PoC fixture for the W11 jury). When the flag is
        set this handler resolves -- ``run_action`` returns ``True`` -- yet
        mutates no observable signal, the resolved-but-inert dead-click the
        behaviour probe classifies ``no_op``. When the flag is unset it
        raises :class:`~textual.actions.SkipAction` so the action never
        resolves (``run_action`` returns ``False``) -- the honest "no live
        handler here" shape, identical to a stale action string.
        """
        from textual.actions import SkipAction

        if not poc_defects_enabled():
            raise SkipAction
        logger.info("action_poc_dead_click resolved outcome=no_op")

    async def on_unmount(self) -> None:
        """Tear the read-only binder down on app exit."""
        if self._binding is not None:
            await self._binding.disconnect()


def _swap_root_logging_to_textual() -> list[logging.Handler]:
    """Detach terminal-bound root handlers, install a :class:`TextualHandler`.

    The CLI installs a :class:`logging.StreamHandler` on the root logger
    (:func:`eawf.surfaces.cli.app._configure_logging`) so non-TUI commands get
    scrubbed stderr logs. That handler keeps writing to the terminal after
    Textual owns the screen, corrupting the live TUI. This swaps the root
    logger for the duration of the Textual run: every root handler whose
    ``stream`` is :data:`sys.stderr` / :data:`sys.stdout` is removed, and a
    :class:`textual.logging.TextualHandler` (which routes to Textual's
    devtools console, never the screen) is installed in its place. The
    :class:`~eawf.observability.logging.scrub.SensitiveScrubber` is not needed on this
    path because the TextualHandler never reaches a terminal.

    Returns:
        The root logger's handler list as it was before the swap, so
        :func:`_restore_root_logging` can reinstate it on app exit.
    """
    import sys

    from textual.logging import TextualHandler

    root = logging.getLogger()  # noqa: EAWF003 (root-logger handler config, not library acquisition)
    saved = list(root.handlers)
    terminal_streams = (sys.stderr, sys.stdout)
    for handler in saved:
        if isinstance(handler, logging.StreamHandler) and handler.stream in terminal_streams:
            root.removeHandler(handler)
    root.addHandler(TextualHandler())
    return saved


def _restore_root_logging(saved: list[logging.Handler]) -> None:
    """Restore the root logger handler list captured before the TUI swap.

    Removes every handler currently on the root logger (the
    :class:`TextualHandler` installed by :func:`_swap_root_logging_to_textual`
    plus any survivors) and reinstates *saved* so the non-TUI CLI path keeps
    its scrubbed stderr sink once the Textual app has exited.

    Args:
        saved: The handler list returned by
            :func:`_swap_root_logging_to_textual`.
    """
    root = logging.getLogger()  # noqa: EAWF003 (root-logger handler config, not library acquisition)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved:
        root.addHandler(handler)


def run_app(scope: ScopeName, state_path: Path | None) -> int:
    """Launch the interactive :class:`EaApp` for *scope*.

    Blocks until the operator quits. Intended for the TTY-interactive
    branch of the CLI bare-command / ``tui`` dispatch; the non-TTY /
    ``--plain`` / ``--no-input`` branch uses the deterministic status
    fallback instead.

    Root logging is swapped to a :class:`TextualHandler` for the duration of
    the run (:func:`_swap_root_logging_to_textual`) so library log lines no
    longer bleed onto the live screen, and restored on exit
    (:func:`_restore_root_logging`) via ``try``/``finally`` so the non-TUI
    CLI path keeps its scrubbed stderr sink.

    Args:
        scope: Resolved scope name.
        state_path: Path to the scope's ``state.json`` (read-only).

    Returns:
        Process exit code (``0`` on a clean quit).
    """
    saved = _swap_root_logging_to_textual()
    try:
        EaApp(scope=scope, state_path=state_path).run()
    finally:
        _restore_root_logging(saved)
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
    "_restore_root_logging",
    "_swap_root_logging_to_textual",
    "build_breadcrumb",
    "probe_braille_coverage",
    "resolve_render_mode",
    "resolve_scope",
    "run_app",
]
