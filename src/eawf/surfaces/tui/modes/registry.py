"""Mode registry -- the single extensibility seam for the MODES chassis.

The Textual ``EaApp`` runs on native :attr:`textual.app.App.MODES` +
``switch_mode`` (a mode owns an independent screen stack; a digit key
flips between them, preserving each mode's state). This module is the
**one place** the mode set is declared, so the per-pane waves
(Home / Trust / Doctor / Evidence / live-feed / config-modal / nav /
multi-repo / QoL) can build in parallel without colliding on a central
dict in ``app.py``.

Mode vs scope are orthogonal axes. A **mode** is a content surface
(Home / Trust / Doctor / ...), switched with digit keys ``1``..``9``. The
**scope** (repo / workspace / user, switched with ``w`` / ``r`` / ``u``)
stays an in-mode operation: the Home mode renders the resolved scope
screen, and the scope switch swaps that screen within the Home mode's own
stack. Building the bound SCOPE x MODE navigation on top of this seam is a
later wave's job; this module only establishes the mode axis.

One-line registration recipe (for a per-pane wave)
--------------------------------------------------
A pane wave that builds, say, the Trust pane does exactly two things:

1. Add its pane screen module (e.g. ``modes/trust.py`` exporting a
   ``TrustModeScreen(ScopeScreen)``).
2. Add a :class:`ModeSpec` row in :data:`MODE_REGISTRY` below carrying its
   next free digit and a one-line factory returning its screen, e.g.::

       ModeSpec("trust", "4", "Trust", lambda app: TrustModeScreen()),

   Nothing else in ``app.py`` changes -- :func:`build_modes`,
   :func:`mode_bindings`, and the ``/trust`` palette verb all derive from
   the registry, so the mode keeps its digit key, its breadcrumb segment,
   and its palette verb for free.

The factory takes the live :class:`~eawf.surfaces.tui.app.EaApp` so a mode
that depends on per-instance launch state (the Home mode reads the
resolved ``_scope`` to pick its scope screen) can build the right body;
modes that ignore it simply discard the argument.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.binding import Binding

if TYPE_CHECKING:
    from textual.screen import Screen

    from eawf.surfaces.tui.app import EaApp

logger = logging.getLogger(__name__)

#: A mode factory: ``(app) -> Screen | str``. Takes the live app so a mode
#: that needs per-instance launch state (Home reads ``app._scope``) can
#: build the right base screen; modes that ignore it discard the argument.
#: A ``str`` return is a name in ``App.SCREENS`` -- Textual resolves it to
#: the **cached** named instance, which the Home mode returns so its base
#: screen is the same instance ``switch_screen`` reuses across scope
#: switches (no duplicate scope-screen instance per mode init).
ModeFactory = Callable[["EaApp"], "Screen[None] | str"]


@dataclass(frozen=True)
class ModeSpec:
    """One row in the mode registry -- a single content surface.

    Attributes:
        name: The mode name; the key under
            :attr:`textual.app.App.MODES` and the value
            ``switch_mode`` is called with. Also the breadcrumb segment
            and the ``/<name>`` palette-verb stem.
        digit: The digit key (``"1"``..``"9"``) that switches to this
            mode. Digits are the mode axis only; arrows stay primary
            intra-pane per the keymap convention.
        title: The human-readable mode title shown in the breadcrumb and
            the help / placeholder surfaces (e.g. ``"Home"``).
        factory: Builds the mode's base screen from the live app (the
            one-line registration recipe in the module docstring).
    """

    name: str
    digit: str
    title: str
    factory: ModeFactory


def _home_screen(app: EaApp) -> str:
    """Resolve the Home mode's base screen to the resolved scope-screen name.

    Home is the scope-bearing mode -- it renders whichever scope screen
    (``repo`` / ``workspace`` / ``user``) the launch resolved into
    ``app._scope``. Returns the **name** (a key of ``App.SCREENS``) rather
    than a fresh instance so Textual resolves it to the cached named scope
    screen -- the same instance ``action_switch_scope`` ->
    ``switch_screen`` reuses. That keeps one scope-screen instance per
    scope (preserving the cached-screen zoom/reset invariants), and the
    ``w`` / ``r`` / ``u`` scope switch swaps it within the Home mode's own
    stack (the orthogonal scope axis), so a mode flip away and back
    preserves the active scope.

    The Home pane (the attention-feed overview band ranking what needs the
    operator) is delivered as the band each scope screen **leads its body
    with** (:func:`~eawf.surfaces.tui.scopes.attention_band`), NOT a separate
    ``home``-only screen. That is the deliberate design choice for the Home
    mode: returning a dedicated ``HomeModeScreen`` here would break the
    orthogonal scope axis (the first ``w`` / ``r`` / ``u`` would
    ``switch_screen`` to a scope screen and never return to a feed screen),
    so the feed lives as a sibling band **above** the scope body that every
    scope switch keeps intact. Home therefore stays the scope-screen name,
    and the scope screens carry the band -- the seam is honoured by what the
    Home factory resolves into.

    Args:
        app: The live app, read for its resolved ``_scope``.

    Returns:
        The scope-screen name for ``app._scope`` (a key of ``App.SCREENS``).
    """
    scope = getattr(app, "_scope", "repo")
    if scope in ("repo", "workspace", "user"):
        return scope
    return "repo"


def _evidence_factory(_app: EaApp) -> Screen[None]:
    """Build the Evidence mode's base screen (the agent-report rollup pane).

    The :class:`~eawf.surfaces.tui.modes.evidence.EvidenceModeScreen` self-
    binds to ``app.state`` + ``app._state_path`` on mount, so the factory
    discards the app argument here and lets the screen resolve the rollup.

    Args:
        _app: The live app (unused -- the screen reads ``app`` on mount).

    Returns:
        A fresh Evidence mode screen.
    """
    from eawf.surfaces.tui.modes.evidence import EvidenceModeScreen

    return EvidenceModeScreen()


def _trust_factory(_app: EaApp) -> Screen[None]:
    """Build the Trust mode's pane over ``compute_trust_scorecard``.

    The Trust pane reads the host app's read-only state + the append-only
    stores under the resolved ``state.json`` and renders the trust
    scorecard honestly (residuals + sample sizes, an honest-negative
    banner when data-starved). It ignores its app argument -- the screen
    reads ``self.app`` directly once mounted -- so the factory takes the
    standard :data:`ModeFactory` shape and discards the argument.

    Args:
        _app: The live app (unused; the screen reads ``self.app``).

    Returns:
        A fresh :class:`~eawf.surfaces.tui.modes.trust.TrustModeScreen`.
    """
    from eawf.surfaces.tui.modes.trust import TrustModeScreen

    return TrustModeScreen()


def _feed_factory(_app: EaApp) -> Screen[None]:
    """Build the live-feed pane for the Feed mode (digit 7).

    Lazy-imports :class:`~eawf.surfaces.tui.modes.feed.FeedModeScreen` (which
    imports the scope chassis) so the registry stays import-cycle-free, the
    same deferral :func:`_trust_factory` uses. The pane subscribes to
    the App's live-event seam on mount, so it needs no per-instance launch
    state and ignores the app argument.

    Args:
        _app: The live app (unused; the screen subscribes on mount).

    Returns:
        A fresh :class:`FeedModeScreen` for the Feed mode's screen stack.
    """
    from eawf.surfaces.tui.modes.feed import FeedModeScreen

    return FeedModeScreen()


def _research_board_factory(_app: EaApp) -> Screen[None]:
    """Build the Research mode pane over the ResearchCampaign store (digit 3).

    Lazy-imports
    :class:`~eawf.surfaces.tui.modes.research_board.ResearchBoardModeScreen`
    (which imports the scope chassis) so the registry stays import-cycle-free,
    the same deferral :func:`_trust_factory` / :func:`_feed_factory` use. The
    pane self-binds to ``app.state`` + ``app._state_path`` on mount, so it
    needs no per-instance launch state and ignores the app argument.

    Args:
        _app: The live app (unused; the screen reads ``self.app`` on mount).

    Returns:
        A fresh :class:`ResearchBoardModeScreen` for the Research mode stack.
    """
    from eawf.surfaces.tui.modes.research_board import ResearchBoardModeScreen

    return ResearchBoardModeScreen()


def _agent_watch_factory(_app: EaApp) -> Screen[None]:
    """Build the agent-watch zoom pane for the Watch mode (digit 8).

    Lazy-imports
    :class:`~eawf.surfaces.tui.modes.agent_watch.AgentWatchModeScreen` (which
    imports the scope chassis) so the registry stays import-cycle-free, the
    same deferral :func:`_feed_factory` / :func:`_research_board_factory` use.
    The pane registers on the App's live-event seam + resolves its watch
    target from ``app.state`` on mount, so it needs no per-instance launch
    state and ignores the app argument.

    Args:
        _app: The live app (unused; the screen subscribes + reads ``self.app``
            on mount).

    Returns:
        A fresh :class:`AgentWatchModeScreen` for the Watch mode's screen stack.
    """
    from eawf.surfaces.tui.modes.agent_watch import AgentWatchModeScreen

    return AgentWatchModeScreen()


def _autopilot_factory(_app: EaApp) -> Screen[None]:
    """Build the ready-wave frontier + dispatch pane for the Autopilot mode (digit 2).

    Lazy-imports
    :class:`~eawf.surfaces.tui.modes.autopilot.AutopilotModeScreen` (which
    imports the scope chassis) so the registry stays import-cycle-free, the
    same deferral :func:`_research_board_factory` / :func:`_agent_watch_factory`
    use. The pane self-binds to ``app.state`` on mount + reaches the daemon
    through the App's daemon-client seam for dispatch, so it needs no per-
    instance launch state and ignores the app argument.

    Args:
        _app: The live app (unused; the screen reads ``self.app`` on mount).

    Returns:
        A fresh :class:`AutopilotModeScreen` for the Autopilot mode's stack.
    """
    from eawf.surfaces.tui.modes.autopilot import AutopilotModeScreen

    return AutopilotModeScreen()


def _doctor_factory(app: EaApp) -> Screen[None]:
    """Build the Doctor-mode health screen (lazy import to avoid a cycle).

    The Doctor pane imports :class:`~eawf.surfaces.tui.scopes.ScopeScreen`,
    which pulls the scope-screen graph; importing it at module top would
    cycle with ``app.py`` (which imports this registry early). Defer the
    import into the factory body -- the same shape the sibling factories
    use -- so the registry stays screen-free at import time.

    Args:
        app: The live app (forwarded to the pane factory, which ignores it
            -- the Doctor view is scope-independent).

    Returns:
        A fresh :class:`~eawf.surfaces.tui.modes.doctor.DoctorModeScreen`.
    """
    from eawf.surfaces.tui.modes.doctor import doctor_mode_factory

    return doctor_mode_factory(app)


def _sandbox_events_factory(_app: EaApp) -> Screen[None]:
    """Build the sandbox-enforcement timeline pane (digit 9).

    Lazy-imports
    :class:`~eawf.surfaces.tui.modes.sandbox_events.SandboxEventsModeScreen`
    (which imports the scope chassis) so the registry stays import-cycle-free,
    the same deferral the sibling factories use. The pane reads the on-disk
    event store for the floor's persisted enforcement rows on mount, so it
    needs no per-instance launch state and ignores the app argument.

    Args:
        _app: The live app (unused; the screen reads ``self.app`` on mount).

    Returns:
        A fresh :class:`SandboxEventsModeScreen` for the Sandbox-events stack.
    """
    from eawf.surfaces.tui.modes.sandbox_events import SandboxEventsModeScreen

    return SandboxEventsModeScreen()


#: The default mode layout seeded on the chassis. Digit order is the switch
#: order (``1``..``9``), matching the ratified mode-order brief. ``home``
#: (the launch default, :data:`DEFAULT_MODE`) renders the resolved scope
#: screen; ``autopilot`` renders the ready-wave dependency frontier with
#: dispatch controls (honest-empty until a wave is claim-ready);
#: ``research_board`` renders the research-campaign overview (campaigns /
#: claims / open questions, honest-empty until a campaign is staged);
#: ``trust`` renders the estimation trust scorecard; ``doctor`` folds the
#: install / state / drift health view; ``evidence`` renders the agent-report
#: rollup (honest-empty until reports exist); ``feed`` renders the live event
#: feed; ``agent_watch`` zooms one dispatched session's live stream with a
#: cancel control (honest-empty until a session is dispatched);
#: ``sandbox_events`` renders the spawn-safety floor's denial timeline (the
#: argv-deny / egress-block / env-scrub / cwd-guard rows the floor persisted,
#: honest-empty until the floor refuses something). Config is not a mode -- it
#: is reachable from every scope via the ``c`` key (and the ``/config`` palette
#: verb), so it owns no digit here.
MODE_REGISTRY: tuple[ModeSpec, ...] = (
    ModeSpec("home", "1", "Home", _home_screen),
    ModeSpec("autopilot", "2", "Autopilot", _autopilot_factory),
    ModeSpec("research_board", "3", "Research", _research_board_factory),
    ModeSpec("trust", "4", "Trust", _trust_factory),
    ModeSpec("doctor", "5", "Doctor", _doctor_factory),
    ModeSpec("evidence", "6", "Evidence", _evidence_factory),
    ModeSpec("feed", "7", "Feed", _feed_factory),
    ModeSpec("agent_watch", "8", "Watch", _agent_watch_factory),
    ModeSpec("sandbox_events", "9", "Sandbox", _sandbox_events_factory),
)

#: The launch mode -- the chassis boots into Home (the scope-bearing
#: mode). Wired onto ``EaApp.DEFAULT_MODE`` so Textual auto-initialises the
#: Home mode's screen stack on run (no explicit ``push_screen``).
DEFAULT_MODE: str = MODE_REGISTRY[0].name


def build_modes(app: EaApp) -> dict[str, Callable[[], Screen[None] | str]]:
    """Build the ``App.MODES`` map (mode name -> screen factory) for *app*.

    Each entry is a zero-arg factory (the shape Textual's ``_init_mode``
    calls) closed over *app*, so a mode that needs per-instance launch
    state -- Home reads ``app._scope`` -- resolves the right base screen
    while the registry stays declarative. A factory may return a
    ``Screen`` or a ``str`` name in ``App.SCREENS`` (Home returns the
    cached scope-screen name). Assigned into the app's per-instance
    ``_modes`` working copy in ``EaApp.__init__``.

    Args:
        app: The live app the factories close over.

    Returns:
        A ``{mode_name: () -> Screen | str}`` dict ready to assign onto
        ``app._modes``.
    """

    def _bind(spec: ModeSpec) -> Callable[[], Screen[None] | str]:
        return lambda: spec.factory(app)

    return {spec.name: _bind(spec) for spec in MODE_REGISTRY}


def mode_bindings() -> list[Binding]:
    """Build the digit-key mode-switch bindings from the registry.

    One ``Binding(digit, "switch_mode('<name>')", "<title>", priority=True)``
    per mode, in registry (digit) order. ``switch_mode`` no-ops when already
    in the target mode, so a repeated digit press is harmless. Appended onto
    ``EaApp.BINDINGS`` so the digits resolve app-wide regardless of focus.

    The ``priority=True`` flag is load-bearing, not cosmetic: Textual's key
    dispatch runs the priority pass FIRST -- from the App down through the
    binding chain (``App._check_bindings(key, priority=True)``) -- BEFORE the
    raw key event is ever forwarded to the focused widget / active screen
    (``App.on_event``). A non-priority digit binding only resolves on that
    later focused-up pass, so any widget that grabs focus and carries a
    same-digit binding (or whose binding chain resolves the digit to another
    action) intercepts the digit first. That is the RB-2 misroute the operator
    hit: a focus-capturing widget swallowed ``3`` and the fall-through reached
    the scope screen's ``question_mark`` -> ``open_help`` neighbour, so ``3``
    opened help instead of switching to Research. Marking the mode switch
    priority makes the digit -> mode switch win at App priority regardless of
    what is focused, so the accelerator is unconditionally the mode axis.

    Returns:
        The list of priority digit-key mode-switch bindings.
    """
    return [
        Binding(spec.digit, f"switch_mode({spec.name!r})", spec.title, show=False, priority=True)
        for spec in MODE_REGISTRY
    ]


def mode_for_name(name: str) -> ModeSpec | None:
    """Resolve a mode name to its :class:`ModeSpec`, or ``None``.

    Args:
        name: The mode name to look up.

    Returns:
        The matching :class:`ModeSpec`, or ``None`` when no mode has that
        name.
    """
    for spec in MODE_REGISTRY:
        if spec.name == name:
            return spec
    return None


def mode_title(name: str) -> str:
    """Return the title for mode *name*, falling back to *name* itself.

    Used by the breadcrumb so an unknown / future mode name still renders
    a readable segment rather than raising.

    Args:
        name: The mode name (typically ``app.current_mode``).

    Returns:
        The mode's registered title, or *name* when it is unregistered.
    """
    spec = mode_for_name(name)
    return spec.title if spec is not None else name


__all__ = [
    "DEFAULT_MODE",
    "MODE_REGISTRY",
    "ModeFactory",
    "ModeSpec",
    "build_modes",
    "mode_bindings",
    "mode_for_name",
    "mode_title",
]
