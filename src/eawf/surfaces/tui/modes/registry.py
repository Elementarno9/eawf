"""Mode registry -- the single extensibility seam for the MODES chassis.

The Textual ``EaApp`` runs on native :attr:`textual.app.App.MODES` +
``switch_mode`` (a mode owns an independent screen stack; a digit key
flips between them, preserving each mode's state). This module is the
**one place** the mode set is declared, so the nine per-pane waves
(Home / Trust / Doctor / Evidence / live-feed / config-modal / nav /
multi-repo / QoL) can build in parallel without colliding on a central
dict in ``app.py``.

Mode vs scope are orthogonal axes. A **mode** is a content surface
(Home / Trust / Doctor / ...), switched with digit keys ``1``..``6``. The
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
2. Replace that mode's ``factory`` in :data:`MODE_REGISTRY` below with a
   one-line lambda returning its screen, e.g.::

       ModeSpec("trust", "2", "Trust", lambda app: TrustModeScreen()),

   (drop the ``PlaceholderModeScreen`` the seed shipped). Nothing else in
   ``app.py`` changes -- :func:`build_modes`, :func:`mode_bindings`, and
   the ``/trust`` palette verb all derive from the registry, so the mode
   keeps its digit key, its breadcrumb segment, and its palette verb for
   free.

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
#: build the right base screen; placeholder modes ignore the argument. A
#: ``str`` return is a name in ``App.SCREENS`` -- Textual resolves it to
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
        digit: The digit key (``"1"``..``"6"``) that switches to this
            mode. Digits are the mode axis only; arrows stay primary
            intra-pane per the keymap convention.
        title: The human-readable mode title shown in the breadcrumb and
            the help / placeholder surfaces (e.g. ``"Home"``).
        factory: Builds the mode's base screen from the live app. A pane
            wave swaps the seed ``PlaceholderModeScreen`` factory for its
            real screen (the one-line registration recipe in the module
            docstring).
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


def _placeholder_factory(title: str) -> ModeFactory:
    """Return a factory that builds a titled :class:`PlaceholderModeScreen`.

    The seed factory for every mode whose pane wave has not landed yet:
    it renders the shared chassis around a ``<title> - coming soon``
    notice so the mode's digit key works the day the chassis ships.

    Args:
        title: The mode title rendered in the coming-soon notice.

    Returns:
        A :data:`ModeFactory` that ignores its app argument and builds the
        placeholder screen for *title*.
    """

    def factory(_app: EaApp) -> Screen[None]:
        from eawf.surfaces.tui.modes.placeholder import PlaceholderModeScreen

        return PlaceholderModeScreen(title)

    return factory


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
    """Build the live-feed pane for the Feed mode (digit 5).

    Lazy-imports :class:`~eawf.surfaces.tui.modes.feed.FeedModeScreen` (which
    imports the scope chassis) so the registry stays import-cycle-free, the
    same deferral :func:`_placeholder_factory` uses. The pane subscribes to
    the App's live-event seam on mount, so it needs no per-instance launch
    state and ignores the app argument.

    Args:
        _app: The live app (unused; the screen subscribes on mount).

    Returns:
        A fresh :class:`FeedModeScreen` for the Feed mode's screen stack.
    """
    from eawf.surfaces.tui.modes.feed import FeedModeScreen

    return FeedModeScreen()


def _doctor_factory(app: EaApp) -> Screen[None]:
    """Build the Doctor-mode health screen (lazy import to avoid a cycle).

    The Doctor pane imports :class:`~eawf.surfaces.tui.scopes.ScopeScreen`,
    which pulls the scope-screen graph; importing it at module top would
    cycle with ``app.py`` (which imports this registry early). Defer the
    import into the factory body -- the same shape the placeholder factory
    uses -- so the registry stays screen-free at import time.

    Args:
        app: The live app (forwarded to the pane factory, which ignores it
            -- the Doctor view is scope-independent).

    Returns:
        A fresh :class:`~eawf.surfaces.tui.modes.doctor.DoctorModeScreen`.
    """
    from eawf.surfaces.tui.modes.doctor import doctor_mode_factory

    return doctor_mode_factory(app)


#: The default six-mode layout seeded on the chassis. Digit order is the
#: switch order (``1``..``6``). ``home`` (the launch default,
#: :data:`DEFAULT_MODE`) renders the resolved scope screen; ``trust`` renders
#: the estimation trust scorecard; ``doctor`` folds the install / state /
#: drift health view; ``evidence`` renders the agent-report rollup
#: (honest-empty until reports exist); ``feed`` renders the live event feed.
#: The remaining modes ship as honest-empty placeholders that their per-pane
#: waves replace via the one-line registration recipe (module docstring).
MODE_REGISTRY: tuple[ModeSpec, ...] = (
    ModeSpec("home", "1", "Home", _home_screen),
    ModeSpec("trust", "2", "Trust", _trust_factory),
    ModeSpec("doctor", "3", "Doctor", _doctor_factory),
    ModeSpec("evidence", "4", "Evidence", _evidence_factory),
    ModeSpec("feed", "5", "Feed", _feed_factory),
    ModeSpec("config", "6", "Config", _placeholder_factory("Config")),
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

    One ``Binding(digit, "switch_mode('<name>')", "<title>")`` per mode,
    in registry (digit) order. ``switch_mode`` no-ops when already in the
    target mode, so a repeated digit press is harmless. Appended onto
    ``EaApp.BINDINGS`` so the digits resolve app-wide regardless of focus.

    Returns:
        The list of digit-key mode-switch bindings.
    """
    return [
        Binding(spec.digit, f"switch_mode({spec.name!r})", spec.title, show=False)
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
