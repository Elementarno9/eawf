"""``RoadmapTree`` — the phase → iter → wave roadmap tree (C06 widget).

Per the C06 brief §5.3 widget row: a :class:`~textual.widgets.Tree` that
renders the full phase → iter → wave hierarchy from the reactive
:class:`~eawf.state.models.State`, prefixing each row with the **V12
glyph schema** (``- > ~ # x !``) keyed off the row's lifecycle status,
and surfacing an inline 5-cell EU bar on iter rows that carry an estimate.

This replaces the P20 regression where the "roadmap pane" shipped as a
flat 5-line numeric counter strip instead of V12's collapsible tree (see
the brief §1 postmortem). Navigation follows the operator keymap: arrow
keys are primary (``↑↓`` move the cursor, ``←→`` collapse / expand),
vim ``hjkl`` ride as hidden aliases declared app-wide on
:class:`~eawf.tui_v2.app.EaApp`. Pressing ``Enter`` on a **wave** row
posts a :class:`RoadmapTree.WaveSelected` message so a host screen can
route to the wave board scoped to that wave (the board screen lands in a
later wave of this band).

The tree is driven entirely by the reactive ``state`` attribute the host
:class:`~eawf.tui_v2.app.EaApp` owns: on mount the widget seeds from
``app.state`` and registers a watcher so any daemon-pushed (or
mtime-poll) state revision rebuilds the tree in place. For standalone
unit tests, assign :attr:`state` directly and the same rebuild fires.

The status signal is the **glyph** (``- > ~ # x !``) per the V12 schema —
deliberately colour-independent so the tree stays legible under the Wong
deuteranopia-safe palette without relying on hue. Tree node labels are
parsed by Rich (not Textual content markup), so labels are built as plain
:class:`~rich.text.Text` to avoid the Rich-vs-Textual markup mismatch on
the ``$`` palette vars; per-row colour is a follow-up that rides Tree's
own styling hooks.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Tree

from eawf.state.enums import (
    IterStatus,
    PhaseStatus,
    WaveStatus,
)
from eawf.tui_v2.widgets.eu_bar import render_bar_plain

if TYPE_CHECKING:
    from textual.widgets.tree import TreeNode

    from eawf.state.models import Iter, Phase, State, Wave

logger = logging.getLogger(__name__)

#: V12 glyph schema (``- > ~ # x !``) mapped onto wave lifecycle status.
#: ``-`` pending · ``>`` claimed · ``~`` in-progress · ``#`` closed ·
#: ``x`` (reserved done/abandoned) · ``!`` failed. Per the C06 brief
#: §5.3 RoadmapTree row + §5.12 glyph set. ASCII-stable so the
#: ``--plain`` fallback needs no swap.
WAVE_GLYPHS: dict[WaveStatus, str] = {
    WaveStatus.PENDING: "-",
    WaveStatus.CLAIMED: ">",
    WaveStatus.IN_PROGRESS: "~",
    WaveStatus.CLOSED: "#",
    WaveStatus.ABANDONED: "x",
    WaveStatus.FAILED: "!",
}

#: Phase-status glyphs reuse the same vocabulary: planned ``-``, active
#: ``~``, closed ``#``, archived ``x``.
PHASE_GLYPHS: dict[PhaseStatus, str] = {
    PhaseStatus.PLANNED: "-",
    PhaseStatus.ACTIVE: "~",
    PhaseStatus.CLOSED: "#",
    PhaseStatus.ARCHIVED: "x",
}

#: Iter-status glyphs: planned ``-``, active ``~``, closed ``#``,
#: abandoned ``x``.
ITER_GLYPHS: dict[IterStatus, str] = {
    IterStatus.PLANNED: "-",
    IterStatus.ACTIVE: "~",
    IterStatus.CLOSED: "#",
    IterStatus.ABANDONED: "x",
}

#: Sentinel glyph for any status that drifts out of the maps above so the
#: tree stays total even if the enums grow.
UNKNOWN_GLYPH: str = "?"


def _glyph_for(status: object, table: dict[Any, str]) -> str:
    """Return the glyph for *status* from *table*, or the sentinel.

    Args:
        status: A lifecycle status enum member.
        table: One of :data:`WAVE_GLYPHS` / :data:`PHASE_GLYPHS` /
            :data:`ITER_GLYPHS`.

    Returns:
        The mapped glyph, or :data:`UNKNOWN_GLYPH` when unmapped.
    """
    return table.get(status, UNKNOWN_GLYPH)


def _row_label(glyph: str, body: str) -> Text:
    """Compose a plain ``<glyph> <body>`` tree row label.

    Returns a Rich :class:`~rich.text.Text` (not a markup string) so the
    body renders literally regardless of any ``[`` it contains and the
    label never trips Rich markup parsing inside the Tree.

    Args:
        glyph: The leading status glyph.
        body: The trailing row text (id + title, etc).

    Returns:
        A plain :class:`~rich.text.Text` for the tree row label.
    """
    return Text(f"{glyph} {body}")


def _iter_eu_suffix(state: State, iter_obj: Iter) -> str | None:
    """Return an inline EU bar (plain string) for *iter_obj*, or ``None``.

    Reads the iter's :class:`~eawf.state.models.EstimateSummary` (via
    ``iter.estimate_id``) for the total and the matching
    :class:`~eawf.state.models.ActualSummary` (by ``scope_id``) for the
    consumed EU, then renders the shared 5-cell bar as a plain string
    (Tree labels are Rich-parsed and cannot resolve the palette vars).
    Returns ``None`` when no estimate is attached so plain iters render
    without a bar.

    Args:
        state: The bound state (estimates / actuals live at the root).
        iter_obj: The iter whose EU bar to build.

    Returns:
        The rendered bar string, or ``None``.
    """
    if iter_obj.estimate_id is None or state.estimates is None:
        return None
    estimate = state.estimates.get(iter_obj.estimate_id)
    if estimate is None:
        return None
    consumed = 0.0
    if state.actuals is not None:
        actual = next(
            (a for a in state.actuals.values() if a.scope_id == estimate.scope_id),
            None,
        )
        if actual is not None:
            consumed = actual.elapsed_eu
    return render_bar_plain(consumed, estimate.expected_eu)


class RoadmapTree(Tree[str]):
    """Collapsible phase → iter → wave tree with V12 status glyphs.

    The tree data payload (``Tree[str]``) is the row's stable id (phase /
    iter / wave id) so a host screen can resolve the selection without
    re-walking the label. Wave-row ``Enter`` posts :class:`WaveSelected`.
    """

    DEFAULT_CSS: ClassVar[str] = """
    RoadmapTree {
        height: 1fr;
        width: 1fr;
        background: $surface;
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

    def on_mount(self) -> None:
        """Seed from the app's reactive state and watch for revisions.

        Standalone tests that assign :attr:`state` directly do not need
        the app watcher; the guard skips it when the app has no ``state``
        attribute (e.g. mounted under a bare harness).
        """
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def watch_state(self, new_state: State | None) -> None:
        """Rebuild the tree whenever the bound state changes."""
        self._rebuild(new_state)

    def _rebuild(self, state: State | None) -> None:
        """Repopulate the tree from *state* (phase → iter → wave).

        Phases sort by id; iters sort by their order in the phase's
        ``iter_ids``; waves sort by their order in the iter's ``wave_ids``.
        A ``None`` state clears the tree to just the (hidden) root so a
        fresh / unreadable workspace renders an empty tree rather than
        crashing.

        Args:
            state: The state to render, or ``None`` to clear.
        """
        self.root.remove_children()
        if state is None:
            return
        for phase_id in sorted(state.phases):
            phase = state.phases[phase_id]
            self._add_phase(state, phase)

    def _add_phase(self, state: State, phase: Phase) -> None:
        """Add a phase node and recurse into its iters.

        Args:
            state: The bound state.
            phase: The phase to add.
        """
        glyph = _glyph_for(phase.status, PHASE_GLYPHS)
        label = _row_label(glyph, f"{phase.id}  {phase.title}")
        node = self.root.add(label, data=phase.id, expand=phase.status is PhaseStatus.ACTIVE)
        for iter_id in phase.iter_ids:
            iter_obj = state.iters.get(iter_id)
            if iter_obj is not None:
                self._add_iter(state, node, iter_obj)

    def _add_iter(self, state: State, parent: TreeNode[str], iter_obj: Iter) -> None:
        """Add an iter node (with optional inline EU bar) and its waves.

        Args:
            state: The bound state.
            parent: The phase node to attach under.
            iter_obj: The iter to add.
        """
        glyph = _glyph_for(iter_obj.status, ITER_GLYPHS)
        label = _row_label(glyph, f"{iter_obj.id}  {iter_obj.title}")
        eu_suffix = _iter_eu_suffix(state, iter_obj)
        if eu_suffix is not None:
            label.append(f"  {eu_suffix}")
        node = parent.add(
            label,
            data=iter_obj.id,
            expand=iter_obj.status is IterStatus.ACTIVE,
        )
        for wave_id in iter_obj.wave_ids:
            wave = state.waves.get(wave_id)
            if wave is not None:
                self._add_wave(node, wave)

    def _add_wave(self, parent: TreeNode[str], wave: Wave) -> None:
        """Add a wave leaf row.

        Args:
            parent: The iter node to attach under.
            wave: The wave to add.
        """
        glyph = _glyph_for(wave.status, WAVE_GLYPHS)
        label = _row_label(glyph, f"{wave.id}  {wave.title}")
        parent.add_leaf(label, data=wave.id)

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


__all__ = [
    "ITER_GLYPHS",
    "PHASE_GLYPHS",
    "UNKNOWN_GLYPH",
    "WAVE_GLYPHS",
    "RoadmapTree",
]
