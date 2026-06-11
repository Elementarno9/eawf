"""``RoadmapTree`` — the phase → iter → wave roadmap tree (widget).

A :class:`~textual.widgets.Tree` that renders the full
phase → iter → wave hierarchy from the reactive
:class:`~eawf.kernel.state.models.State`, prefixing each row with a
lifecycle **sigil** drawn from the shared cosmic-terminal vocabulary
(:func:`eawf.surfaces.tui.widgets.sigils.glyph`) keyed off the row's
lifecycle status, and surfacing a right-pinned bar on every row: iter and
phase rows carry a completion bar (closed ÷ total child waves) rendered by
the unified :func:`~eawf.surfaces.tui.widgets.eu_bar.render_completion_bar`
(a block ``█`` fill over a ``▒`` remainder track plus the ``n/m`` ratio);
wave rows carry a hybrid size/burn bar — the ``effort_bucket`` size bar
(``XS``..``XL``) by default, auto-upgrading to the live token-burn bar
(``tokens_consumed ÷ token_budget``) once a wave carries a budget, and
falling back to the empty-state sentinel only when it has neither. Every
bar pins flush at the pane right edge with a blank gap — the title
ellipsizes if it would collide with the bar, never the bar.

Leaf wave rows draw the FULL lifecycle sigil set (pending / claimed /
running / closed / failed). Iter and phase rows draw the FOUR-state subset
(pending / running / closed / failed): a phase or iter is never CLAIMED —
that state belongs to a single voice claiming one wave — so the claimed
sigil never appears on a branch row.

This replaces the P20 regression where the "roadmap pane" shipped as a
flat 5-line numeric counter strip instead of a collapsible tree (see the
brief §1 postmortem). Navigation follows the operator keymap: arrow keys
are primary (``↑↓`` move the cursor, ``←→`` collapse / expand), vim
``hjkl`` ride as hidden aliases declared app-wide on
:class:`~eawf.surfaces.tui.app.EaApp`. Pressing ``Enter`` on a **wave** row
posts a :class:`RoadmapTree.WaveSelected` message so a host screen can
route to the wave board scoped to that wave (the board screen lands in a
later wave of this band).

The tree is driven entirely by the reactive ``state`` attribute the host
:class:`~eawf.surfaces.tui.app.EaApp` owns: on mount the widget seeds from
``app.state`` and registers a watcher so any daemon-pushed (or
mtime-poll) state revision rebuilds the tree in place. For standalone
unit tests, assign :attr:`state` directly and the same rebuild fires.

The status signal is the **sigil** — the glyph stays the primary,
colour-independent signal so the tree is legible without relying on hue.
Colour is *additive* on top of the glyph (colour-blind safe): the leading
glyph is tinted by its lifecycle status from the Wong deuteranopia-safe
palette. Tree node labels are parsed by Rich (not Textual content markup)
and cannot resolve the ``$`` palette vars, so the glyph tint is applied as
a concrete-colour Rich span sourced from the shared
:data:`eawf.surfaces.tui.widgets.status_tint.STATUS_COLOURS` map
(re-exported here) — the same hex set ``theme.tcss`` carries as
``$status-*`` vars (Rich-label colours mirror the CSS vars, as the inline
completion bar's plain renderer already does).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Tree

from eawf.kernel.state.enums import (
    IterStatus,
    PhaseStatus,
    WaveStatus,
)
from eawf.surfaces.render.plan_view import build_roadmap_rows
from eawf.surfaces.tui.widgets.eu_bar import (
    CANONICAL_BAR_CELLS,
    DEFAULT_RENDER_MODE,
    EMPTY_STATE,
    render_bar_plain,
    render_completion_bar,
    render_size_bar,
)
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph
from eawf.surfaces.tui.widgets.status_tint import STATUS_COLOURS, status_colour

if TYPE_CHECKING:
    from textual.events import Resize
    from textual.widgets.tree import TreeNode

    from eawf.kernel.state.models import Iter, Phase, State, Wave
    from eawf.surfaces.tui.widgets.eu_bar import RenderMode

logger = logging.getLogger(__name__)

#: Cell count for the inline completion bar on iter / phase rows. Aliases
#: the single :data:`~eawf.surfaces.tui.widgets.eu_bar.CANONICAL_BAR_CELLS`
#: home so the completion bar and the EU / burn bar stacked in the same
#: roadmap row render at one width; the ``done/total`` ratio reads flush-right
#: after the fixed-width fill. Matches
#: :func:`~eawf.surfaces.tui.widgets.eu_bar.render_completion_bar`'s default
#: width so the block-fill maths stay one home.
COMPLETION_BAR_CELLS: int = CANONICAL_BAR_CELLS

#: Active-wave elapsed burn bands. The row marker uses ``~`` at/above the
#: warning threshold and ``!`` at/above the error threshold so the status is
#: visible even when the plain bar is colourless inside a Rich tree label.
TIME_WARN_FRACTION: float = 0.8
TIME_ERROR_FRACTION: float = 1.0

#: Minimum blank cells kept between a (possibly truncated) row title and a
#: right-pinned bar, so the bar never abuts the title even when the title
#: fills its budget. The title is truncated to leave at least this gap.
_BAR_GAP: int = 2

#: Single-character ellipsis appended to a title truncated to the row
#: width. The U+2026 glyph is one cell wide, so the truncated body plus
#: this marker never exceeds the computed budget.
ELLIPSIS: str = "…"

#: Per-depth guide indentation Textual prepends to a Tree row, mirroring
#: ``Tree.guide_depth`` (the widget sets ``guide_depth = 2``). Used to
#: size the title-truncation budget so the visible row — guide chrome plus
#: ``<glyph> <body>`` — fits the tree's content width.
_GUIDE_INDENT_PER_DEPTH: int = 2

#: Width of the leading expand/collapse toggle (``▼ `` / ``▶ ``) or the
#: leaf spacer Textual renders before a row label. Folded into the
#: truncation budget alongside the depth-scaled guide indent.
_ROW_TOGGLE_WIDTH: int = 2

#: Width of the ``<glyph><space>`` prefix every row label carries (the
#: status glyph plus its trailing space), counted against the budget.
_GLYPH_PREFIX_WIDTH: int = 2

#: Width of the vertical scrollbar Textual reserves on the right edge when
#: the tree's content outgrows the pane height. Folded into the truncation
#: budget so a row sized before the scrollbar appears still fits the
#: narrower content region afterwards — otherwise ``overflow-x: hidden``
#: clips the row's trailing completion-bar count. The first layout has no
#: scrollbar (content width = full interior) and adding enough rows shows
#: the scrollbar without firing a second resize, so reserving the gutter
#: up front is what keeps the budget correct across that transition.
#: Matches Textual's default 2-cell scrollbar.
_SCROLLBAR_GUTTER: int = 2

#: Minimum number of body characters kept when a row is truncated, so an
#: extremely narrow pane still shows a sliver of the id rather than
#: collapsing the body to just the ellipsis.
_MIN_BODY_CHARS: int = 4

#: Budget used when the tree has no measured width yet (pre-layout, or a
#: bare standalone harness that never lays the widget out). Wide enough
#: that no realistic title truncates until a real width lands via
#: :meth:`RoadmapTree.on_resize`.
_UNSIZED_BUDGET: int = 1024

#: Wave lifecycle status -> the FULL :class:`~eawf.surfaces.tui.widgets.sigils.Sigil`
#: set a leaf row draws. ``ABANDONED`` has no sigil of its own (the cosmic
#: vocabulary carries no abandoned mark); it reads as :attr:`Sigil.FAILED`
#: because an abandoned wave is a terminal non-success the same as a failed
#: one, and the tinted glyph still distinguishes it (abandoned greys, failed
#: reds) on the colour axis.
WAVE_SIGILS: dict[WaveStatus, Sigil] = {
    WaveStatus.PENDING: Sigil.PENDING,
    WaveStatus.CLAIMED: Sigil.CLAIMED,
    WaveStatus.IN_PROGRESS: Sigil.RUNNING,
    WaveStatus.CLOSED: Sigil.CLOSED,
    WaveStatus.ABANDONED: Sigil.FAILED,
    WaveStatus.FAILED: Sigil.FAILED,
}

#: Phase status -> the FOUR-state sigil subset a branch row draws (no
#: ``CLAIMED`` — a phase is never claimed by one voice). ``PLANNED`` reads as
#: :attr:`Sigil.PENDING`, ``ACTIVE`` as :attr:`Sigil.RUNNING`, ``CLOSED`` as
#: :attr:`Sigil.CLOSED`, and ``ARCHIVED`` as :attr:`Sigil.CLOSED` (a clean
#: terminal state; the muted archived tint carries the distinction).
PHASE_SIGILS: dict[PhaseStatus, Sigil] = {
    PhaseStatus.PLANNED: Sigil.PENDING,
    PhaseStatus.ACTIVE: Sigil.RUNNING,
    PhaseStatus.CLOSED: Sigil.CLOSED,
    PhaseStatus.ARCHIVED: Sigil.CLOSED,
}

#: Iter status -> the FOUR-state sigil subset a branch row draws (no
#: ``CLAIMED``). ``PLANNED`` reads as :attr:`Sigil.PENDING`, ``ACTIVE`` as
#: :attr:`Sigil.RUNNING`, ``CLOSED`` as :attr:`Sigil.CLOSED`, and
#: ``ABANDONED`` as :attr:`Sigil.FAILED` (a terminal non-success).
ITER_SIGILS: dict[IterStatus, Sigil] = {
    IterStatus.PLANNED: Sigil.PENDING,
    IterStatus.ACTIVE: Sigil.RUNNING,
    IterStatus.CLOSED: Sigil.CLOSED,
    IterStatus.ABANDONED: Sigil.FAILED,
}

#: Internal alias kept so the tree's own call sites read locally; the
#: status-tint map + lookup live in the shared
#: :mod:`eawf.surfaces.tui.widgets.status_tint` helper (one home for the Wong
#: fallback hexes, re-exported here for back-compat). ``STATUS_COLOURS`` is
#: imported above and re-exported in ``__all__``.
_status_colour = status_colour


def _sigil_glyph(sigil: Sigil, *, mode: RenderMode) -> str:
    """Return *sigil*'s glyph in the active render *mode*.

    Threads the tree's resolved render mode into the shared SHAPE-layer
    helper so a single ``"unicode"`` / ``"ascii"`` flip repaints every row's
    leading sigil in the matching glyph column.

    Args:
        sigil: The lifecycle-state mark to render.
        mode: The App's resolved render-mode label (``"ascii"`` selects the
            ASCII column; any other value selects the unicode column).

    Returns:
        The single-cell sigil glyph string for the resolved column.
    """
    return glyph(sigil, mode=mode)


def _row_label(glyph: str, body: str, glyph_colour: str | None = None) -> Text:
    """Compose a ``<glyph> <body>`` tree row label with a tinted glyph.

    Returns a Rich :class:`~rich.text.Text` (not a markup string) so the
    body renders literally regardless of any ``[`` it contains and the
    label never trips Rich markup parsing inside the Tree. When
    *glyph_colour* is given, only the leading glyph carries the colour
    span — the body stays the theme's default text colour so colour is
    additive on top of the glyph signal (colour-blind safe).

    Args:
        glyph: The leading status glyph.
        body: The trailing row text (id + title, etc).
        glyph_colour: Optional concrete colour for the glyph span; ``None``
            renders the glyph in the default text colour.

    Returns:
        A :class:`~rich.text.Text` for the tree row label.
    """
    label = Text()
    label.append(glyph, style=glyph_colour or "")
    label.append(f" {body}")
    return label


def _wave_completion(state: State, wave_ids: list[str]) -> tuple[int, int]:
    """Return ``(closed, total)`` over *wave_ids* against the state table.

    The populated completion signal for an iter or a phase: how many of
    its child waves are CLOSED. Ids that do not resolve to a known wave
    are skipped so a dangling reference never inflates *total*.

    Args:
        state: The bound state holding the wave table.
        wave_ids: The child-wave ids to tally.

    Returns:
        A ``(closed, total)`` pair; *total* is the count of *wave_ids* that
        resolve to a known wave, *closed* the subset with a CLOSED status.
    """
    total = 0
    closed = 0
    for wave_id in wave_ids:
        wave = state.waves.get(wave_id)
        if wave is None:
            continue
        total += 1
        if wave.status is WaveStatus.CLOSED:
            closed += 1
    return closed, total


def _phase_wave_ids(state: State, phase: Phase) -> list[str]:
    """Return the flat list of every wave id under *phase*'s iters.

    Walks the phase's ``iter_ids`` in order and concatenates each iter's
    ``wave_ids``, so a phase completion bar tallies across all its iters.
    Iter ids that do not resolve are skipped.

    Args:
        state: The bound state (its ``iters`` table resolves the ids).
        phase: The phase whose child waves to collect.

    Returns:
        The ordered list of child-wave ids across the phase's iters.
    """
    wave_ids: list[str] = []
    for iter_id in phase.iter_ids:
        iter_obj = state.iters.get(iter_id)
        if iter_obj is not None:
            wave_ids.extend(iter_obj.wave_ids)
    return wave_ids


def _truncate_body(body: str, budget: int) -> str:
    """Truncate *body* to *budget* cells, appending an ellipsis when cut.

    Width-aware row-title ellipsis: a *body* already within *budget* is
    returned unchanged (short titles untouched); a longer one is cut so
    the kept text plus the single-cell :data:`ELLIPSIS` marker fits the
    budget exactly. The kept slice never falls below
    :data:`_MIN_BODY_CHARS` characters so an extreme budget still shows a
    sliver of the id rather than a lone ellipsis.

    Args:
        body: The row body text (``<id>  <title>``).
        budget: The maximum cell width the body may occupy. Non-positive
            budgets are treated as :data:`_MIN_BODY_CHARS` (clamped).

    Returns:
        Either *body* unchanged, or its truncated ``<head>…`` form.
    """
    if len(body) <= budget:
        return body
    keep = max(budget - len(ELLIPSIS), _MIN_BODY_CHARS)
    return f"{body[:keep]}{ELLIPSIS}"


def _pin_bar_right(
    glyph: str, body: str, bar: str, *, budget: int, glyph_colour: str | None
) -> Text:
    """Compose a row label with *bar* pinned flush-right after *body*.

    Builds the ``<glyph> <body>`` label (tinted glyph via :func:`_row_label`),
    truncates *body* so it leaves at least a :data:`_BAR_GAP` blank gap
    before the bar, then pads with spaces so the bar's trailing cell lands
    at the right edge of *budget* (the body-region cell count, which already
    reserves the scrollbar gutter). The title ellipsizes on collision; the
    bar is never cut.

    Args:
        glyph: The leading status glyph.
        body: The row body text (``<id>  <title>``) before truncation.
        bar: The pre-rendered bar string (plain, no Rich markup).
        budget: The cell count the body region (``<body> <gap> <bar>``) may
            occupy — the value :meth:`RoadmapTree._body_budget` returns.
        glyph_colour: Optional concrete colour for the glyph span.

    Returns:
        A :class:`~rich.text.Text` for the tree row label with the bar
        pinned flush-right.
    """
    title_budget = max(budget - len(bar) - _BAR_GAP, _MIN_BODY_CHARS)
    truncated = _truncate_body(body, title_budget)
    pad = max(budget - len(truncated) - len(bar), _BAR_GAP)
    label = _row_label(glyph, truncated, glyph_colour)
    label.append(f"{' ' * pad}{bar}")
    return label


def _burn_marker(consumed: float, total: float) -> str:
    """Return the visible burn-band marker for a consumed / total pair."""
    fraction = consumed / total if total > 0 else 0.0
    if fraction >= TIME_ERROR_FRACTION:
        return "!"
    if fraction >= TIME_WARN_FRACTION:
        return "~"
    return "."


def _burn_bar_with_band(label: str, consumed: float, total: float, *, mode: RenderMode) -> str:
    """Return ``<label><band>:<bar>`` for a time or token burn gauge."""
    return f"{label}{_burn_marker(consumed, total)}:{render_bar_plain(consumed, total, mode=mode)}"


def _wave_time_budget_minutes(state: State, wave: Wave) -> float | None:
    """Return a wave's elapsed-time budget in minutes, preferring estimates."""
    estimates = state.estimates or {}
    estimate = estimates.get(wave.id)
    if estimate is not None and estimate.pessimistic_minutes > 0:
        return estimate.pessimistic_minutes
    if wave.effort_bucket is None:
        return None
    from eawf.workflow.estimation.buckets import EU_MINUTES, wave_estimate_eu

    minutes = wave_estimate_eu(wave) * EU_MINUTES
    return minutes if minutes > 0 else None


@dataclass(slots=True)
class _ViewSnapshot:
    """Captured cursor / scroll / expansion state across an in-place rebuild.

    A daemon-pushed (or mtime-poll) state revision rebuilds the tree in
    place by clearing every node and repopulating, which would otherwise
    drop the operator's cursor, scroll position, and the set of branches
    they had expanded. This snapshot is taken before the clear and replayed
    after the repopulate so a background refresh never re-collapses an
    expanded iter or jumps the cursor.

    Attributes:
        cursor_data_id: The ``data`` payload (phase / iter / wave id) of the
            row the cursor was on, or ``None`` when the tree was empty or
            had no cursor (the signal to fall back to the active-phase
            auto-scroll).
        scroll_y: The vertical scroll offset (in lines) at snapshot time.
        expanded_ids: The ``data`` payloads of every branch node the
            operator had expanded, so they re-expand after the rebuild
            regardless of their lifecycle status.
    """

    cursor_data_id: str | None
    scroll_y: float
    expanded_ids: set[str] = field(default_factory=set)


class RoadmapTree(Tree[str]):
    """Collapsible phase → iter → wave tree with V12 status glyphs.

    The tree data payload (``Tree[str]``) is the row's stable id (phase /
    iter / wave id) so a host screen can resolve the selection without
    re-walking the label. Wave-row ``Enter`` posts :class:`WaveSelected`.

    Textual's ``Tree`` binds only ``shift+left`` / ``shift+right`` for
    cursor-to-parent navigation; plain ``←`` / ``→`` are unbound (and no
    longer scroll horizontally now that ``overflow-x`` is hidden). These
    BINDINGS wire plain ``←`` / ``→`` to standard collapse / expand so the
    operator keymap (arrows primary) matches the docstring above.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("right", "expand_or_descend", "Expand", show=False),
        Binding("left", "collapse_or_ascend", "Collapse", show=False),
    ]

    DEFAULT_CSS: ClassVar[str] = """
    RoadmapTree {
        height: 1fr;
        width: 1fr;
        background: $surface;
        overflow-x: hidden;
    }
    """

    class WaveSelected(Message):
        """Posted when the operator presses Enter on a wave row.

        Attributes:
            wave_id: The selected wave's id (the row's tree-data payload).
        """

        def __init__(self, wave_id: str) -> None:
            self.wave_id = wave_id
            super().__init__()

    #: Bound state, watched so a fresh revision rebuilds the tree. ``None``
    #: until the first read-only load completes.
    state: reactive[State | None] = reactive(None)

    def __init__(self, **kwargs: Any) -> None:
        """Construct the tree with a fixed root label.

        Args:
            **kwargs: Forwarded to :class:`textual.widgets.Tree` (e.g.
                ``id=`` for the host screen's ``query_one``).
        """
        super().__init__("roadmap", data="__root__", **kwargs)
        self.show_root = False
        self.guide_depth = 2
        # Cosmetic id-column padding widths, recomputed per rebuild so 2-
        # and 3-digit ids (P9 / P10 / P100) co-render with their titles in
        # a single aligned column. Seeded to the historical narrow widths
        # so an unbound tree (pre-rebuild) does not crash if a label
        # render slips through.
        self._phase_id_width: int = 3
        self._iter_id_width: int = 6
        self._wave_id_width: int = 10
        # The set of branch data-ids to re-expand during the current
        # rebuild, seeded from the pre-clear snapshot so a background refresh
        # keeps the operator's expanded branches open. Empty outside a
        # rebuild and on the first (snapshot-less) populate, where the
        # status-default expansion governs instead.
        self._restore_expanded: set[str] = set()

    def on_mount(self) -> None:
        """Seed from the app's reactive state and watch for revisions.

        Standalone tests that assign :attr:`state` directly do not need
        the app watcher; the guard skips it when the app has no ``state``
        attribute (e.g. mounted under a bare harness). The same guard
        wires a ``render_mode`` watcher so a Braille ↔ ASCII flip rebuilds
        the tree — its bars are baked into the eager row labels, so the
        flip must re-run :meth:`_rebuild` to repaint them in the new set.
        """
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def _on_render_mode(self, _mode: RenderMode) -> None:
        """Rebuild the tree so its baked-in bars repaint in the flipped set."""
        if self.state is not None:
            self._rebuild(self.state)

    def watch_state(self, new_state: State | None) -> None:
        """Rebuild the tree whenever the bound state changes."""
        self._rebuild(new_state)

    def on_resize(self, event: Resize) -> None:
        """Re-truncate row titles to the new width when the pane resizes.

        Row-title ellipsis is budgeted against the tree's content width,
        which is unknown until layout runs. Rebuilding on resize re-cuts
        every title to fit the new width (and restores a title in full
        when the pane grows back). A no-op while the state is unbound.

        Args:
            event: The Textual resize event (unused; the new width is read
                from :attr:`size` during the rebuild).
        """
        del event
        if self.state is not None:
            self._rebuild(self.state)

    def _render_mode(self) -> RenderMode:
        """Return the app's live bar render mode, or the safe default.

        Threads :attr:`eawf.surfaces.tui.app.EaApp.render_mode` into the bar
        renderers so a Braille ↔ ASCII flip rerenders the tree. Falls back
        to :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_RENDER_MODE` under a bare
        harness whose host App carries no ``render_mode`` attribute.

        Returns:
            The active ``"braille"`` / ``"ascii"`` mode.
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def _body_budget(self, depth: int) -> int:
        """Return the cell budget a row body may occupy at *depth*.

        Subtracts the guide chrome Textual prepends — the depth-scaled
        guide indent, the expand/collapse toggle, the ``<glyph> `` prefix,
        and the reserved vertical-scrollbar gutter — from the tree's
        measured content width. Reserving the scrollbar gutter up front
        keeps the budget correct even on the first (pre-scrollbar) layout,
        so a row stays inside the narrower content region once the
        scrollbar appears. Falls back to :data:`_UNSIZED_BUDGET` when the
        width is not yet known (pre-layout or a bare harness), so short
        titles never truncate before a real width arrives via
        :meth:`on_resize`.

        Args:
            depth: The row's depth below the hidden root (phase ``0``,
                iter ``1``, wave ``2``).

        Returns:
            The maximum cell width the row body (``<id>  <title>``) may use.
        """
        width = self.size.width
        if width <= 0:
            return _UNSIZED_BUDGET
        chrome = (
            _GUIDE_INDENT_PER_DEPTH * depth
            + _ROW_TOGGLE_WIDTH
            + _GLYPH_PREFIX_WIDTH
            + _SCROLLBAR_GUTTER
        )
        return max(width - chrome, _MIN_BODY_CHARS)

    def _rebuild(self, state: State | None) -> None:
        """Repopulate the tree from *state* (phase → iter → wave).

        Phase order is driven by
        :func:`eawf.surfaces.render.plan_view.build_roadmap_rows`
        (P28-W18 unification) so the TUI, ``eawf roadmap show --md``,
        and ``/prep`` plan-mode share one canonical phase projection.
        Iters sort by their order in the phase's ``iter_ids``; waves
        sort by their order in the iter's ``wave_ids``. A ``None``
        state clears the tree to just the (hidden) root so a fresh /
        unreadable workspace renders an empty tree rather than
        crashing.

        The rebuild is in place (the node tree is cleared and
        repopulated), so it snapshots the cursor / scroll / expanded-set
        before clearing and replays them after repopulating: a
        daemon-pushed refresh keeps the operator's cursor, scroll
        position, and expanded branches instead of silently re-collapsing
        them. The active-phase auto-scroll is the fallback only when there
        was no prior cursor (a first populate or a refresh of an empty
        tree).

        Args:
            state: The state to render, or ``None`` to clear.
        """
        snapshot = self._snapshot_view()
        self.root.remove_children()
        if state is None:
            self._phase_id_width = 0
            self._iter_id_width = 0
            self._wave_id_width = 0
            return
        # Cosmetic display-pad: compute the widest id at each level once
        # per rebuild so mixed 2-digit and 3-digit ids (P9 / P10 / P100)
        # render with their titles aligned in a single column, rather than
        # the title jitter that raw ``{id}  {title}`` produces. Min-width
        # ``3`` matches the post-AGENTS ``\d{2,}`` widening floor (``P##``
        # is the historical narrow form; ``P###`` is the wide form), so
        # an all-2-digit project still renders at its natural width.
        self._phase_id_width = max((len(pid) for pid in state.phases), default=3)
        self._iter_id_width = max((len(iid) for iid in state.iters), default=6)
        self._wave_id_width = max((len(wid) for wid in state.waves), default=10)
        self._restore_expanded = snapshot.expanded_ids
        try:
            for row in build_roadmap_rows(state):
                phase = state.phases.get(row.id)
                if phase is None:
                    continue
                self._add_phase(state, phase)
        finally:
            self._restore_expanded = set()
        # Textual's Tree recomputes ``cursor_line`` after the add/remove
        # batch flushes, clobbering a cursor move issued mid-rebuild, so the
        # cursor + scroll restore is deferred to after the refresh settles.
        # The expanded-set replay already happened synchronously above (via
        # ``_expand_for``), so only the cursor / scroll wait.
        self.call_after_refresh(self._restore_view, state, snapshot)

    def _snapshot_view(self) -> _ViewSnapshot:
        """Capture the cursor / scroll / expanded-set before an in-place clear.

        Walks the current node tree (before it is cleared) and records the
        cursor row's data-id, the vertical scroll offset, and every expanded
        branch's data-id. A tree with no children yields an empty snapshot
        whose ``None`` cursor signals the active-phase fallback on the next
        populate.

        Returns:
            The :class:`_ViewSnapshot` to replay after the repopulate.
        """
        cursor = self.cursor_node
        cursor_data_id = cursor.data if cursor is not None else None
        expanded: set[str] = set()

        def walk(node: TreeNode[str]) -> None:
            for child in node.children:
                if child.allow_expand and child.is_expanded and child.data is not None:
                    expanded.add(child.data)
                walk(child)

        walk(self.root)
        return _ViewSnapshot(
            cursor_data_id=cursor_data_id,
            scroll_y=self.scroll_offset.y,
            expanded_ids=expanded,
        )

    def _restore_view(self, state: State, snapshot: _ViewSnapshot) -> None:
        """Replay the pre-clear cursor / scroll after a repopulate.

        Restores the cursor to the row whose data-id matches the snapshot
        (so a background refresh keeps the operator's selection); when that
        id is gone (the wave / iter it pointed at was removed) or there was
        no prior cursor, falls back to the active-phase auto-scroll. The
        expanded set is replayed during the populate itself (see
        :meth:`_expand_for`), so this only re-pins the cursor + scroll.

        Args:
            state: The freshly rendered state (its ``current.phase_id``
                drives the active-phase fallback).
            snapshot: The :class:`_ViewSnapshot` captured before the clear.
        """
        if snapshot.cursor_data_id is None:
            self._scroll_to_active_phase(state)
            return
        node = self._find_node(snapshot.cursor_data_id)
        if node is None:
            self._scroll_to_active_phase(state)
            return
        self.move_cursor(node)
        # ``move_cursor`` scrolls the cursor into view; re-pin the prior
        # scroll so a refresh keeps the exact viewport the operator had.
        self.scroll_to(y=snapshot.scroll_y, animate=False)

    def _find_node(self, data_id: str) -> TreeNode[str] | None:
        """Return the first node whose ``data`` payload equals *data_id*.

        Args:
            data_id: The phase / iter / wave id to resolve.

        Returns:
            The matching node, or ``None`` when no node carries the id (it
            was removed since the snapshot).
        """
        found: TreeNode[str] | None = None

        def walk(node: TreeNode[str]) -> None:
            nonlocal found
            for child in node.children:
                if found is not None:
                    return
                if child.data == data_id:
                    found = child
                    return
                walk(child)

        walk(self.root)
        return found

    def _expand_for(self, data_id: str, *, status_default: bool) -> bool:
        """Return whether a branch node should mount expanded.

        During an in-place rebuild a branch re-expands when the operator had
        it expanded in the pre-clear snapshot, regardless of its lifecycle
        status, so a background refresh never re-collapses a branch the
        operator opened. On the first (snapshot-less) populate the
        status-default governs (active phase / iter auto-expand).

        Args:
            data_id: The branch's data payload (phase / iter id).
            status_default: The status-driven default (``True`` for an
                ACTIVE phase / iter).

        Returns:
            ``True`` to mount the node expanded.
        """
        if data_id in self._restore_expanded:
            return True
        return status_default

    def _scroll_to_active_phase(self, state: State) -> None:
        """Move the cursor to the current phase's node so it starts in view.

        The roadmap can outgrow the pane; without this the tree opens
        scrolled to the top (oldest phase) and the operator has to scroll
        down to the work in flight. Moving the cursor to the node whose
        data matches ``state.current.phase_id`` also scrolls it into view.
        A ``None`` pointer (or a phase with no matching node) leaves the
        cursor at its default top position.

        Args:
            state: The bound state (its ``current.phase_id`` names the
                active phase).
        """
        active_phase_id = state.current.phase_id
        if active_phase_id is None:
            return
        for node in self.root.children:
            if node.data == active_phase_id:
                self.move_cursor(node)
                return

    def _add_phase(self, state: State, phase: Phase) -> None:
        """Add a phase node and recurse into its iters.

        Args:
            state: The bound state.
            phase: The phase to add.
        """
        mode = self._render_mode()
        sigil = _sigil_glyph(PHASE_SIGILS[phase.status], mode=mode)
        closed, total = _wave_completion(state, _phase_wave_ids(state, phase))
        bar = render_completion_bar(
            closed,
            total,
            width=COMPLETION_BAR_CELLS,
            mode=mode,
        )
        label = _pin_bar_right(
            sigil,
            f"{phase.id.ljust(self._phase_id_width)}  {phase.title}",
            bar,
            budget=self._body_budget(depth=0),
            glyph_colour=_status_colour(phase.status),
        )
        node = self.root.add(
            label,
            data=phase.id,
            expand=self._expand_for(phase.id, status_default=phase.status is PhaseStatus.ACTIVE),
        )
        for iter_id in phase.iter_ids:
            iter_obj = state.iters.get(iter_id)
            if iter_obj is not None:
                self._add_iter(state, node, iter_obj)

    def _add_iter(self, state: State, parent: TreeNode[str], iter_obj: Iter) -> None:
        """Add an iter node (with a right-pinned completion bar) and its waves.

        Args:
            state: The bound state.
            parent: The phase node to attach under.
            iter_obj: The iter to add.
        """
        mode = self._render_mode()
        sigil = _sigil_glyph(ITER_SIGILS[iter_obj.status], mode=mode)
        closed, total = _wave_completion(state, list(iter_obj.wave_ids))
        bar = render_completion_bar(
            closed,
            total,
            width=COMPLETION_BAR_CELLS,
            mode=mode,
        )
        label = _pin_bar_right(
            sigil,
            f"{iter_obj.id.ljust(self._iter_id_width)}  {iter_obj.title}",
            bar,
            budget=self._body_budget(depth=1),
            glyph_colour=_status_colour(iter_obj.status),
        )
        node = parent.add(
            label,
            data=iter_obj.id,
            expand=self._expand_for(
                iter_obj.id, status_default=iter_obj.status is IterStatus.ACTIVE
            ),
        )
        for wave_id in iter_obj.wave_ids:
            wave = state.waves.get(wave_id)
            if wave is not None:
                self._add_wave(state, node, wave)

    def _add_wave(self, state: State, parent: TreeNode[str], wave: Wave) -> None:
        """Add a wave leaf row with a right-pinned hybrid size/burn bar.

        The bar is the wave's ``effort_bucket`` size bar by default,
        auto-upgrading to elapsed-time and ``tokens_consumed / token_budget``
        burn bars once those budgets exist,
        status-tinted via the row's glyph colour. A wave with neither a
        budget nor a bucket shows
        :data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` pinned right rather
        than a fabricated 0 % bar.

        Args:
            state: The bound state, used to resolve per-wave estimates.
            parent: The iter node to attach under.
            wave: The wave to add.
        """
        sigil = _sigil_glyph(WAVE_SIGILS[wave.status], mode=self._render_mode())
        bar = self._wave_burn_bar(state, wave)
        label = _pin_bar_right(
            sigil,
            f"{wave.id.ljust(self._wave_id_width)}  {wave.title}",
            bar,
            budget=self._body_budget(depth=2),
            glyph_colour=_status_colour(wave.status),
        )
        parent.add_leaf(label, data=wave.id)

    def _wave_burn_bar(self, state: State, wave: Wave) -> str:
        """Return the wave's hybrid size/time/token bar, or :data:`EMPTY_STATE`.

        Resolves the wave-row gauge in priority order:

        1. Active waves with a time budget render an elapsed-time bar.
        2. A positive ``token_budget`` renders the live token burn bar.
        3. If both are available, both bars render side by side.
        4. Otherwise an ``effort_bucket`` lights the ``XS``..``XL`` size
           bar, the populated signal for today's planned waves.
        5. Neither falls back to the empty-state sentinel.

        Args:
            state: The bound state, used to resolve per-wave estimates.
            wave: The wave whose ``token_budget`` / ``tokens_consumed`` and
                ``effort_bucket`` drive the bar.

        Returns:
            One or two plain braille / ASCII burn bars, a size bar, or
            :data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` when the wave has
            neither a positive ``token_budget`` nor an ``effort_bucket``.
        """
        time_bar = self._wave_time_burn_bar(state, wave)
        token_bar = None
        if wave.token_budget:
            token_bar = _burn_bar_with_band(
                "K",
                float(wave.tokens_consumed),
                float(wave.token_budget),
                mode=self._render_mode(),
            )
        if time_bar is not None and token_bar is not None:
            return f"{time_bar} {token_bar}"
        if time_bar is not None:
            return time_bar
        if token_bar is not None:
            return token_bar
        if wave.effort_bucket is not None:
            return render_size_bar(wave.effort_bucket.value, mode=self._render_mode())
        return EMPTY_STATE

    def _wave_time_burn_bar(self, state: State, wave: Wave) -> str | None:
        """Return the active wave's elapsed-time burn bar, when computable.

        Anchors on ``claimed_at`` (work-start), not ``opened_at``
        (plan/creation), so a wave planned long before it is claimed does
        not render an inflated clock. A wave without a ``claimed_at`` has
        no work-start fact to elapse from, so no time bar is rendered.
        """
        if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
            return None
        if wave.claimed_at is None:
            return None
        budget_minutes = _wave_time_budget_minutes(state, wave)
        if budget_minutes is None:
            return None
        elapsed_seconds = (datetime.now(UTC) - wave.claimed_at).total_seconds()
        if elapsed_seconds < 0:
            return None
        return _burn_bar_with_band(
            "T",
            elapsed_seconds / 60.0,
            budget_minutes,
            mode=self._render_mode(),
        )

    def on_tree_node_selected(self, event: Tree.NodeSelected[str]) -> None:
        """Route Enter on a **wave** leaf to a :class:`WaveSelected` message.

        Phase / iter rows toggle expand/collapse (Textual's default Enter
        behaviour on a branch); only leaf rows whose data is a wave id
        post the drill-in message.

        Args:
            event: The Textual node-selected event.
        """
        node = event.node
        wave_id = node.data
        if wave_id is None or node.allow_expand:
            return
        self.post_message(self.WaveSelected(wave_id))

    def action_expand_or_descend(self) -> None:
        """Handle plain ``→``: expand a collapsed branch, else descend.

        On a collapsed branch (it has children and is not yet expanded)
        the node expands. On an already-expanded branch the cursor moves
        to its first child. On a leaf (a wave row, ``allow_expand`` False)
        this is a no-op.
        """
        node = self.cursor_node
        if node is None or not node.allow_expand:
            return
        if node.is_expanded:
            if node.children:
                self.move_cursor(node.children[0])
        else:
            node.expand()

    def action_collapse_or_ascend(self) -> None:
        """Handle plain ``←``: collapse an expanded branch, else ascend.

        On an expanded branch the node collapses. On an already-collapsed
        branch (or a leaf) the cursor moves to the node's parent; a parent
        that is the hidden root is skipped so the cursor never lands on it.
        """
        node = self.cursor_node
        if node is None:
            return
        if node.allow_expand and node.is_expanded:
            node.collapse()
            return
        parent = node.parent
        if parent is not None and not parent.is_root:
            self.move_cursor(parent)


__all__ = [
    "COMPLETION_BAR_CELLS",
    "ELLIPSIS",
    "ITER_SIGILS",
    "PHASE_SIGILS",
    "STATUS_COLOURS",
    "WAVE_SIGILS",
    "RoadmapTree",
]
