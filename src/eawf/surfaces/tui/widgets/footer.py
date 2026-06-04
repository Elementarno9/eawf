"""``Footer`` + ``Heartbeat`` — shared chassis footer (widget catalog).

A single footer composite reused by every per-scope screen
(``RepoScreen`` / ``WorkspaceScreen`` / ``UserScreen``) with **no
per-scope duplication**. The footer is **two rows** tall:

* **Row 1** merges the context-aware key-hint strip (left) with the
  status cells (right): the weekly-burn cell, the needs_user attention
  badge, and a live :class:`Heartbeat` dot. Hints use **full key names**
  only (``PageUp`` / ``PageDown`` / ``Enter`` / ``Esc`` — never ``PgUp``)
  per the operator keymap convention.
* **Row 2** is the always-visible **mode row**: every registered mode
  rendered as ``<digit> <title>`` (derived from
  :data:`~eawf.surfaces.tui.modes.registry.MODE_REGISTRY`), so the operator
  sees and can reach all modes at a glance. The **active** mode's token is
  highlighted (bold accent); the rest render muted.

The heartbeat is a ``•`` pulse that proves the TUI is alive,
``accent``-coloured by default and ``err``-coloured when any pane is
degraded, with a 0.5 s double-pulse ack on the ``F5`` force-refresh
keypress.

Bundling the heartbeat inside the footer is the chassis trim: the
three scope screens reuse one :class:`Footer` (which mounts the shared
:class:`Heartbeat`) rather than each re-declaring the chrome — the
``~5300 → ~2500`` salvageable-LOC target. Colours resolve
against the ``theme.tcss`` palette vars (``$muted`` for the hints,
``$accent`` / ``$err`` for the heartbeat) — never hardcoded hex.

The heartbeat pulse runs off a Textual ``set_interval`` timer started on
mount; the host screen flips :attr:`Heartbeat.degraded` (wired to the
App's degraded reactive in a later wave) to swap the dot colour. The
pulse cadence + the visible/hidden toggle are pure-ish state on the
widget so a Pilot test can drive a tick and assert the dot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static

from eawf.surfaces.tui.widgets.eu_bar import EMPTY_STATE
from eawf.surfaces.tui.widgets.heartbeat import HEARTBEAT_GLYPH, HEARTBEAT_INTERVAL_S, Heartbeat
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.workflow.estimation.metrics import compute_weekly_burn

if TYPE_CHECKING:
    from datetime import datetime

    from eawf.kernel.state.models import State


#: The frozen canonical key-token vocabulary every footer hint label must
#: draw from. The drift this guards against is real and recurring -- per-mode
#: tuples authored ``up/down`` instead of the arrow glyph, lowercase ``enter``
#: instead of ``Enter``, the ``w/u scope`` typo dropping the repo letter, and
#: a stale ``1-6``/``1-8 mode`` fragment that duplicates the always-visible
#: mode row. Freezing the token set turns each of those into a hard
#: :class:`ValueError` at authoring time rather than a silent visual
#: inconsistency. The members are:
#:
#: * **Arrow glyphs** -- ``↑↓`` / ``←→`` (never the spelled-out ``up/down``),
#:   matching the operator keymap convention that arrows are the primary
#:   navigation affordance.
#: * **Capitalized full key names** -- ``Enter`` / ``Esc`` / ``F5`` (never
#:   ``enter`` / ``PgUp``); the same full-key-name rule the mode row + the
#:   global bindings follow.
#: * **The three-letter scope switch** -- ``w/r/u`` (workspace / repo / user);
#:   all three letters, never the truncated ``w/u``.
#: * **Single-key action tokens** -- the literal letter / word a per-mode key
#:   binds (``a`` / ``c`` / ``d`` / ``H`` / ``k`` / ``K`` / ``p`` / ``q`` /
#:   ``r`` / ``s`` / ``S`` / ``space``) plus the palette / help glyphs
#:   (``/`` / ``?``). These are the concrete keys a pane advertises.
#:
#: A digit-range token (``1-6`` / ``1-8``) is deliberately ABSENT: the
#: always-visible mode row (row 2) already lists every mode with its digit, so
#: a hint-strip digit fragment is stale duplication.
CANONICAL_HINT_TOKENS: Final[frozenset[str]] = frozenset(
    {
        # Arrow glyphs (primary navigation).
        "↑↓",
        "←→",
        # Capitalized full key names.
        "Enter",
        "Esc",
        "F5",
        # Three-letter scope switch (all three letters).
        "w/r/u",
        # Palette / help glyphs.
        "/",
        "?",
        # Single-key action tokens advertised by the per-mode panes.
        "a",
        "c",
        "d",
        "H",
        "k",
        "K",
        "p",
        "q",
        "r",
        "s",
        "S",
        "space",
    }
)


#: The frozen canonical *action* phrase each cross-surface (shared) key token
#: must carry. Token freezing (above) stopped the key half from drifting, but
#: the action half still varied across the ten footer surfaces -- the same
#: ``↑↓`` key read ``move`` on one screen, ``row`` on another, ``scroll`` on a
#: third, and ``tree`` / ``select`` elsewhere; ``Enter`` split between ``open``
#: / ``zoom`` / ``peek``. A key that does the same thing everywhere must read
#: the same everywhere, so the shared tokens are pinned to ONE action word and
#: a drifted action becomes a hard :class:`ValueError` at authoring time (the
#: same regression-guard shape :data:`CANONICAL_HINT_TOKENS` gives the key
#: half). The members are exactly the tokens that appear on more than one
#: surface:
#:
#: * **Navigation** -- ``↑↓`` -> ``select`` (the one cross-surface row-cursor
#:   verb), ``←→`` -> ``collapse``, ``Enter`` -> ``open``, ``Esc`` -> ``back``.
#: * **Global affordances** -- ``w/r/u`` -> ``scope``, ``F5`` -> ``refresh``,
#:   ``/`` -> ``palette``, ``?`` -> ``help``, ``q`` -> ``quit``,
#:   ``c`` -> ``config``.
#:
#: Mode-specific tokens (``a`` / ``d`` / ``H`` / ``k`` / ``K`` / ``p`` / ``s``
#: / ``S`` / ``space`` and ``r`` where it binds a per-mode verb) are DELIBERATELY
#: absent: a key that means something different per mode keeps its own free
#: action text, so this map only governs the genuinely shared vocabulary.
CANONICAL_HINT_ACTIONS: Final[dict[str, str]] = {
    # Navigation.
    "↑↓": "select",
    "←→": "collapse",
    "Enter": "open",
    "Esc": "back",
    # Global affordances.
    "w/r/u": "scope",
    "F5": "refresh",
    "/": "palette",
    "?": "help",
    "q": "quit",
    "c": "config",
}


def render_hint_label(token: str, action: str) -> str:
    """Render one footer hint label from the frozen canonical vocabulary.

    The single chokepoint every footer hint fragment is authored through, so
    neither half of a label can drift. A hint label is a *key token* paired
    with a short *action* phrase -- e.g. ``render_hint_label("↑↓", "select")``
    -> ``"↑↓ select"``. Two guards apply:

    * The *token* MUST be a member of :data:`CANONICAL_HINT_TOKENS` (catches a
      drifted key token: ``up/down`` vs ``↑↓``, ``enter`` vs ``Enter``,
      ``w/u`` vs ``w/r/u``).
    * When the token is a cross-surface *shared* one (a key of
      :data:`CANONICAL_HINT_ACTIONS`), the *action* MUST equal that token's
      one canonical phrase (catches a drifted action: the same ``↑↓`` reading
      ``move`` on one surface and ``scroll`` on another). A mode-specific
      token -- one absent from :data:`CANONICAL_HINT_ACTIONS` -- keeps free
      action text (the verb the key performs in that pane).

    Args:
        token: The key token; MUST be a member of
            :data:`CANONICAL_HINT_TOKENS`.
        action: The short action phrase the key performs. Free text for a
            mode-specific token; for a shared token (a key of
            :data:`CANONICAL_HINT_ACTIONS`) it MUST equal the canonical phrase.

    Returns:
        The joined ``"<token> <action>"`` hint label.

    Raises:
        ValueError: When *token* is not in :data:`CANONICAL_HINT_TOKENS` (a
            drifted key token), or when *token* is a shared token whose
            *action* does not equal :data:`CANONICAL_HINT_ACTIONS`\\ ``[token]``
            (a drifted shared-token action). Both are authoring-time
            regression guards.
    """
    if token not in CANONICAL_HINT_TOKENS:
        raise ValueError(f"non-canonical hint token: {token!r}")
    canonical = CANONICAL_HINT_ACTIONS.get(token)
    if canonical is not None and action != canonical:
        raise ValueError(
            f"non-canonical action for {token!r}: {action!r} (canonical: {canonical!r})"
        )
    return f"{token} {action}"


#: Default footer key hints (full key names). Screens may pass a
#: scope-specific override via :meth:`Footer.set_hints`; this is the base
#: chrome shared by every scope. ``w/r/u`` scope-switch + ``F5`` refresh
#: are surfaced so the operator sees the global affordances. Each label is
#: produced through :func:`render_hint_label` so the key tokens stay pinned
#: to :data:`CANONICAL_HINT_TOKENS`.
DEFAULT_HINTS: tuple[str, ...] = (
    render_hint_label("↑↓", "select"),
    render_hint_label("Enter", "open"),
    render_hint_label("w/r/u", "scope"),
    render_hint_label("F5", "refresh"),
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
)


#: The frozen canonical left-to-right ORDER the footer hint strip lays its
#: fragments out in. Token freezing (:data:`CANONICAL_HINT_TOKENS`) and action
#: freezing (:data:`CANONICAL_HINT_ACTIONS`) stopped each label from drifting,
#: but the *order* the labels appear in still varied across the ten footer
#: surfaces -- one screen led with ``↑↓`` and trailed ``q``, another put ``c``
#: before ``w/r/u``, a third dropped ``F5`` entirely. An operator who learns
#: the strip on one surface must read it the same way on every other, so the
#: position of each key is pinned here and every surface is sorted through the
#: :func:`order_hints` chokepoint. The order is three bands, left to right:
#:
#: * **Primary navigation** -- ``↑↓`` / ``←→`` / ``Enter`` / ``Esc`` first, so
#:   the row-cursor + drill affordances lead (the operator's most-used keys,
#:   matching the arrows-are-primary keymap convention).
#: * **Per-mode action keys** -- the mode-specific verbs (``a`` / ``d`` / ``H``
#:   / ``k`` / ``K`` / ``p`` / ``r`` / ``s`` / ``S`` / ``space``) in the middle
#:   band, after navigation and before the globals, so a pane's own keys sit
#:   together regardless of which subset that pane advertises.
#: * **Global affordances** -- the cross-surface globals last in a fixed order:
#:   ``w/r/u`` scope-switch, then ``c`` config, then ``F5`` refresh, then the
#:   ``/`` palette / ``?`` help glyphs, then ``q`` quit at the tail. These read
#:   identically on every surface (a key always lands at the same offset from
#:   the right), so the operator's eye finds quit / help / config in one place.
#:
#: A token absent from this tuple (a future per-mode key not yet listed) sorts
#: AFTER every known token in stable original order -- :func:`order_hints`
#: never drops a fragment, so an unlisted key still renders, just at the tail
#: of its surface's strip until it is added here.
HINT_KEY_PRIORITY: Final[tuple[str, ...]] = (
    # Primary navigation.
    "↑↓",
    "←→",
    "Enter",
    "Esc",
    # Per-mode action keys.
    "a",
    "d",
    "H",
    "k",
    "K",
    "p",
    "r",
    "s",
    "S",
    "space",
    # Global affordances (fixed tail order).
    "w/r/u",
    "c",
    "F5",
    "/",
    "?",
    "q",
)

#: The two global-affordance fragments :func:`order_hints` guarantees on every
#: surface. ``c config`` (the registry-driven config window) and ``F5 refresh``
#: (the force-refresh + heartbeat ack) are reachable from every scope and mode,
#: so the strip must advertise them everywhere even on a surface whose authored
#: tuple omitted one. Both are rendered through :func:`render_hint_label` so the
#: injected fragment is byte-identical to a hand-authored one (same token + same
#: canonical action), which keeps the de-dup in :func:`order_hints` a plain
#: membership test.
_REQUIRED_HINTS: Final[tuple[str, ...]] = (
    render_hint_label("c", "config"),
    render_hint_label("F5", "refresh"),
)


def order_hints(hints: tuple[str, ...]) -> tuple[str, ...]:
    """Canonicalise a footer hint strip: inject the globals, sort to canon.

    The single chokepoint every footer surface's hints flow through (via
    :meth:`Footer.set_hints`), so the strip reads the same everywhere. Two
    transforms, in order:

    * **Inject the required globals.** ``c config`` and ``F5 refresh`` are
      reachable from every scope and mode, so they are appended (each as the
      canonical :func:`render_hint_label` fragment) when absent -- a surface
      whose authored tuple omitted one still advertises it. A fragment already
      present (byte-equal to the canonical one) is NOT re-added, so the
      function is idempotent.
    * **Sort to the canonical order.** Each fragment is keyed by its leading
      token (the text before the first space, e.g. ``"↑↓ select"`` ->
      ``"↑↓"``) and sorted by that token's index in :data:`HINT_KEY_PRIORITY`.
      A token absent from the priority tuple sorts AFTER every known token, in
      stable original order, so an unlisted key is never dropped -- it renders
      at the tail until it is added to the canon.

    The sort is stable, so two fragments sharing a leading token (an unusual
    but legal authoring choice) keep their relative order. Because the injected
    globals are the canonical fragments and the sort key is the leading token,
    ``order_hints(order_hints(x)) == order_hints(x)`` for every input.

    Args:
        hints: The authored hint fragments for one surface (each a
            ``"<token> <action>"`` label, typically from
            :func:`render_hint_label`).

    Returns:
        The hints with ``c config`` + ``F5 refresh`` guaranteed present and the
        whole strip ordered by :data:`HINT_KEY_PRIORITY`.
    """
    complete = list(hints)
    for required in _REQUIRED_HINTS:
        if required not in complete:
            complete.append(required)
    last = len(HINT_KEY_PRIORITY)
    priority = {token: index for index, token in enumerate(HINT_KEY_PRIORITY)}
    return tuple(sorted(complete, key=lambda label: priority.get(label.split(" ", 1)[0], last)))


#: Empty-state marker for the weekly-burn line. Rendered when the project
#: has no ``weekly_eu_target`` set or no actuals have rolled up yet — the
#: EU estimation surface is unpopulated scaffolding today, so a graceful
#: "surface now, data later" placeholder is shown rather than a misleading
#: ``0 / 0`` figure. Sourced from the canonical
#: :data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` sentinel so every
#: "no data" surface stays in lockstep (kept as the footer's public name).
WEEKLY_BURN_EMPTY: str = EMPTY_STATE

#: Static label prefixing the weekly-burn line in both the populated and
#: empty-state forms.
WEEKLY_BURN_LABEL: str = "weekly burn:"

#: Label prefixing the active needs_user badge cell. Kept compact so the
#: first footer row still fits the burn cell + heartbeat dot at 80 cols;
#: it renders in the ``$warn`` attention colour when pauses are pending.
#: ASCII-only per the source-glyph convention.
NEEDS_USER_BADGE_LABEL: str = "needs_user"

#: Separator between mode-row tokens. The same bullet
#: :func:`format_hints` joins key hints with, so the mode row and the hint
#: strip read as one visual family.
MODE_ROW_SEP: str = "  ·  "


def format_needs_user_badge(count: int) -> str:
    """Render the footer needs_user badge text for *count* pending pauses.

    Pure render source — unit-testable without mounting the widget. The
    badge is **quiet** (the empty string, taking no footer space) when
    *count* is ``0`` so an idle surface carries no attention noise (the
    brand-badges-quiet-when-idle convention), and shows
    ``needs_user <count>`` when at least one pause is open. A negative
    count is clamped to ``0`` so a stray decrement never renders a
    nonsensical figure.

    Args:
        count: The number of open needs_user pauses across all scopes.

    Returns:
        The empty string when *count* <= ``0``, else
        ``needs_user <count> `` (a trailing space separates it from the
        heartbeat dot that follows on the same row).
    """
    safe = max(0, count)
    if safe == 0:
        return ""
    return f"{NEEDS_USER_BADGE_LABEL} {safe} "


def format_hints(hints: tuple[str, ...]) -> str:
    """Join key hints into the footer strip with a separating bullet.

    Args:
        hints: The ordered key-hint fragments (full key names).

    Returns:
        The joined hint string, e.g. ``↑↓ select · Enter open · q quit``.
    """
    return "  ·  ".join(hints)


def build_mode_row(active_mode: str | None) -> str:
    """Build the always-visible footer mode row from the mode registry.

    Pure render source — unit-testable without mounting the widget. Reads
    :data:`~eawf.surfaces.tui.modes.registry.MODE_REGISTRY` (imported lazily
    to avoid an import cycle, since the registry pulls the screen/app graph)
    and renders one ``<digit> <title>`` token per mode in registry (digit)
    order, lowercased to match the operator example
    (``1 home · 2 autopilot · ...``), joined by :data:`MODE_ROW_SEP`.

    The token whose mode name equals *active_mode* is highlighted with a
    bold accent span (the brand / heartbeat accent convention); every other
    token renders muted. When *active_mode* is ``None`` (or names no
    registered mode — e.g. a bare test harness whose Textual default mode
    is ``"_default"``) no token is highlighted, so the row stays honest
    rather than implying a mode that is not active. Titles are
    markup-escaped defensively so a bracket in a future title can never be
    parsed as a style tag.

    Args:
        active_mode: The active mode name (``app.current_mode``), or
            ``None`` when no mode is resolvable.

    Returns:
        A Textual content-markup string for the mode row.
    """
    from eawf.surfaces.tui.modes.registry import MODE_REGISTRY

    tokens: list[str] = []
    for spec in MODE_REGISTRY:
        label = f"{spec.digit} {escape_markup(spec.title.lower())}"
        if spec.name == active_mode:
            tokens.append(f"[$accent][b]{label}[/b][/]")
        else:
            tokens.append(f"[$muted]{label}[/]")
    return MODE_ROW_SEP.join(tokens)


def build_weekly_burn_line(state: State | None, *, now: datetime | None = None) -> str:
    """Build the footer weekly-burn line from *state*.

    Pure render source — unit-testable without mounting the widget. The
    rollup comes from :func:`~eawf.workflow.estimation.metrics.compute_weekly_burn`
    (trailing-7-day actual-EU consumption versus
    ``Project.weekly_eu_target``). The graceful :data:`WEEKLY_BURN_EMPTY`
    placeholder is rendered — never a ``0 / 0`` figure — whenever the line
    has no real data to show, namely when the bound state is ``None``, the
    project has no ``weekly_eu_target`` set, or no actuals have rolled up
    yet (the EU surface is unpopulated scaffolding today).

    Args:
        state: The bound state, or ``None`` before first load.
        now: Optional clock injection threaded to
            :func:`~eawf.workflow.estimation.metrics.compute_weekly_burn` so the
            trailing-7-day window is deterministic in tests. Production
            callers leave this ``None`` to anchor on wall-clock.

    Returns:
        ``weekly burn: <consumed> / <target> EU`` when a target is set and
        actuals exist, else ``weekly burn: — no data``.
    """
    if state is None or not state.actuals:
        return f"{WEEKLY_BURN_LABEL} {WEEKLY_BURN_EMPTY}"
    metric = compute_weekly_burn(state, now=now)
    if metric.target_eu is None:
        return f"{WEEKLY_BURN_LABEL} {WEEKLY_BURN_EMPTY}"
    return f"{WEEKLY_BURN_LABEL} {metric.consumed_eu:g} / {metric.target_eu:g} EU"


class Footer(Static):
    """Shared chassis footer: hints + status (row 1) + mode row (row 2).

    Reused verbatim by every per-scope screen (shared chassis). The
    footer is **two rows**: row 1 merges the key-hint strip (left) with
    the weekly-burn cell + needs_user badge + the :class:`Heartbeat` dot
    (right); row 2 is the always-visible mode row — every registered mode
    rendered ``<digit> <title>`` with the active mode highlighted. A host
    screen may override the hints via :meth:`set_hints` without touching
    the chrome. The burn cell is driven by the host
    :class:`~eawf.surfaces.tui.app.EaApp` reactive ``state`` (seeded on mount,
    watched for revisions) and falls back to the
    :data:`WEEKLY_BURN_EMPTY` placeholder when no target / actuals exist.
    The mode row's highlight is seeded from ``app.current_mode`` on mount
    (each mode owns its own scope screen, so the footer mounts fresh on
    every mode switch and reads the now-active mode); standalone tests
    assign :attr:`active_mode` directly. Standalone-testable via the Pilot
    harness.
    """

    DEFAULT_CSS: ClassVar[str] = """
    Footer {
        height: 2;
        dock: bottom;
        background: $panel;
        padding: 0 1;
    }
    Footer .footer-row1 {
        height: 1;
    }
    Footer .footer-hints {
        width: 1fr;
        height: 1;
    }
    Footer .footer-burn {
        width: auto;
        height: 1;
        color: $text-muted;
    }
    Footer .footer-needs-user {
        width: auto;
        height: 1;
        color: $text-muted;
    }
    Footer .footer-needs-user.-attention {
        color: $warn;
        text-style: bold;
    }
    Footer .footer-modes {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    """

    #: Active key hints, watched so a host override repaints the strip. The
    #: default is run through :func:`order_hints` so a surface that never calls
    #: :meth:`set_hints` (or sets hints equal to :data:`DEFAULT_HINTS`) still
    #: shows the canonical strip -- ordered + ``c config`` / ``F5 refresh``
    #: present -- without the host having to canonicalise it.
    hints: reactive[tuple[str, ...]] = reactive(order_hints(DEFAULT_HINTS))

    #: Bound state, watched so a fresh revision repaints the weekly-burn
    #: cell. ``None`` until the first read-only load completes.
    state: reactive[State | None] = reactive(None)

    #: Count of open needs_user pauses across all scopes. Watched so a
    #: change repaints the badge cell + flips its attention colour. The
    #: host App (:class:`~eawf.surfaces.tui.app.EaApp`) pushes the count off
    #: the same pause source the auto-open path reads; standalone tests
    #: assign it directly. Quiet (no count) at ``0``.
    pending_pauses: reactive[int] = reactive(0)

    #: Active mode name (``app.current_mode``), watched so a change
    #: repaints the mode row's highlight. ``None`` until seeded from the
    #: app on mount; standalone tests assign it directly. A value naming
    #: no registered mode (a bare harness's Textual default) highlights
    #: nothing.
    active_mode: reactive[str | None] = reactive(None)

    def compose(self) -> ComposeResult:
        """Lay out the hints+status row (row 1) above the mode row (row 2).

        Row 1 is a Horizontal carrying the full-width hint strip (left) and
        the weekly-burn cell, the needs_user attention badge, and the
        heartbeat dot (right). Row 2 is the always-visible mode row.
        """
        with Horizontal(classes="footer-row1"):
            yield Static(format_hints(self.hints), classes="footer-hints")
            yield Static(build_weekly_burn_line(self.state), classes="footer-burn")
            # The initial attention class is set here (not only in the
            # post-mount watcher) so a count seeded before mount paints
            # attention-coloured on first render rather than waiting for a
            # later change to fire the watcher.
            badge_classes = "footer-needs-user"
            if self.pending_pauses > 0:
                badge_classes += " -attention"
            yield Static(
                format_needs_user_badge(self.pending_pauses),
                classes=badge_classes,
            )
            yield Heartbeat(id="heartbeat")
        yield Static(build_mode_row(self.active_mode), classes="footer-modes")

    def on_mount(self) -> None:
        """Seed the burn line + pause badge + mode row from the app and watch them.

        Standalone tests that assign :attr:`state` / :attr:`pending_pauses`
        / :attr:`active_mode` directly do not need the app watchers; the
        guards skip them when the host exposes no matching attribute (e.g.
        mounted under a bare harness).
        """
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        app_pauses = getattr(self.app, "pending_pauses", None)
        if isinstance(app_pauses, int):
            self.pending_pauses = app_pauses
        if hasattr(self.app, "pending_pauses"):
            self.watch(self.app, "pending_pauses", self._on_app_pending_pauses)
        # Seed the mode-row highlight from the active mode. Each mode owns
        # its own scope screen, so the footer mounts fresh on every mode
        # switch and reads the now-active ``current_mode`` here. Subscribe
        # to the app's mode-change signal too (when present) so a shared
        # footer would still repaint on a flip -- the same defensive seam
        # the Header uses.
        current_mode = getattr(self.app, "current_mode", None)
        if isinstance(current_mode, str):
            self.active_mode = current_mode
        mode_signal = getattr(self.app, "mode_change_signal", None)
        if mode_signal is not None:
            mode_signal.subscribe(self, self._on_mode_change)
        self._repaint_burn()
        self._repaint_needs_user()
        self._repaint_modes()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def _on_app_pending_pauses(self, count: int) -> None:
        """Mirror an app-level pending-pause count onto this widget's reactive."""
        self.pending_pauses = count

    def _on_mode_change(self, mode: str) -> None:
        """Mirror an app-level mode change onto this widget's reactive."""
        self.active_mode = mode

    def set_hints(self, hints: tuple[str, ...]) -> None:
        """Replace the footer key hints (scope-specific override).

        Routes the override through :func:`order_hints` so every surface --
        scope or mode -- gets the identical canonical strip: ``c config`` /
        ``F5 refresh`` guaranteed present and the whole strip ordered by
        :data:`HINT_KEY_PRIORITY`. A host therefore passes its authored tuple
        verbatim and the chokepoint canonicalises it.

        Args:
            hints: The authored key-hint fragments (full key names) for this
                surface; canonicalised before they reach the reactive.
        """
        self.hints = order_hints(hints)

    def watch_hints(self, hints: tuple[str, ...]) -> None:
        """Repaint the hint strip when the hints change.

        Guarded on mount: the child ``Static`` only exists after
        :meth:`compose`, so a pre-mount reactive assignment is a no-op
        (``compose`` reads the current value).
        """
        if not self.is_mounted:
            return
        self.query_one(".footer-hints", Static).update(format_hints(hints))

    def watch_state(self) -> None:
        """Repaint the weekly-burn line when the bound state changes."""
        self._repaint_burn()

    def watch_pending_pauses(self) -> None:
        """Repaint the needs_user badge when the pending-pause count changes."""
        self._repaint_needs_user()

    def watch_active_mode(self) -> None:
        """Repaint the mode row's highlight when the active mode changes."""
        self._repaint_modes()

    def _repaint_burn(self) -> None:
        """Re-render the weekly-burn line from the current state.

        Guarded on mount: the child ``Static`` only exists after
        :meth:`compose`, so a pre-mount reactive assignment is a no-op
        (``compose`` reads the current value).
        """
        if not self.is_mounted:
            return
        self.query_one(".footer-burn", Static).update(build_weekly_burn_line(self.state))

    def _repaint_needs_user(self) -> None:
        """Re-render the needs_user badge + flip its attention colour.

        The ``-attention`` class is toggled on whenever at least one pause
        is pending, so the badge draws the eye via the ``$warn`` colour
        when it matters and stays quiet (``$text-muted``, no count) when
        idle. Queries the child defensively: it only exists after
        :meth:`compose`, so a pre-compose reactive write (or a write during
        ``on_mount`` before the widget reports mounted) is a safe no-op —
        the child's compose-time value / ``on_mount`` repaint covers it.
        """
        cells = self.query(".footer-needs-user")
        if not cells:
            return
        cell = cells.first(Static)
        cell.update(format_needs_user_badge(self.pending_pauses))
        cell.set_class(self.pending_pauses > 0, "-attention")

    def _repaint_modes(self) -> None:
        """Re-render the mode row with the active mode highlighted.

        Queries the child defensively: it only exists after :meth:`compose`,
        so a pre-compose reactive write (or a write during ``on_mount``
        before the widget reports mounted) is a safe no-op — the child's
        compose-time value / ``on_mount`` repaint covers it.
        """
        cells = self.query(".footer-modes")
        if not cells:
            return
        cells.first(Static).update(build_mode_row(self.active_mode))


__all__ = [
    "CANONICAL_HINT_ACTIONS",
    "CANONICAL_HINT_TOKENS",
    "DEFAULT_HINTS",
    "HEARTBEAT_GLYPH",
    "HEARTBEAT_INTERVAL_S",
    "HINT_KEY_PRIORITY",
    "MODE_ROW_SEP",
    "NEEDS_USER_BADGE_LABEL",
    "WEEKLY_BURN_EMPTY",
    "WEEKLY_BURN_LABEL",
    "Footer",
    "Heartbeat",
    "build_mode_row",
    "build_weekly_burn_line",
    "format_hints",
    "format_needs_user_badge",
    "order_hints",
    "render_hint_label",
]
