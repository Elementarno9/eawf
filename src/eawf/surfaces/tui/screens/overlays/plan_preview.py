"""``PlanPreviewModal`` — plan-mode wave-DAG preview overlay.

The ``/prep`` plan-mode surface: opened when ``/roadmap propose`` returns
a ``status=needs_user`` envelope, it renders the proposed phase's
three-tier spec aggregate (PhaseSpec + IterSpec + WaveSpec) as a
hierarchical :class:`~textual.widgets.Tree` and surfaces a three-option
AUQ — ``approve`` / ``edit`` / ``reject`` (``approve`` runs
``eawf roadmap apply <P##>`` only). ``←`` / ``→`` move the highlight,
``Enter`` confirms the highlighted action, and ``Esc`` is equivalent to
``reject``.

This wave lands the **overlay**: the hierarchical tree built from the
bound :class:`~eawf.kernel.state.models.State`, the three-option AUQ, and the
chosen-action result returned through the ``ModalScreen`` dismiss value.
Wiring the ``approve`` pick to the ``eawf roadmap apply`` CLI verb +
re-rendering on the Edit-Plan subagent's ``agent_end`` report rides the
wave that lands those CLI verbs — they do not exist yet — so the host
gates its mutation on the returned action label.

The tree content is assembled by a pure builder (:func:`build_plan_tree`)
that takes the reactive state + a phase id and returns a typed
:class:`PlanTree` (the phase row plus ordered iter / wave child rows).
Keeping the layout pure means the rendered plan is unit-testable without
mounting Textual, and the modal stays a thin view over it. Construct the
modal with a pre-built :class:`PlanTree` (the host screen builds it from
``app.state``) so the overlay never reaches back into App state itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static, Tree

from eawf.surfaces.tui.widgets import sigils
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.workflow.estimation.buckets import wave_estimate_eu

if TYPE_CHECKING:
    from collections.abc import Iterable

    from eawf.kernel.state.models import State
    from eawf.platform.lint.eawf022_propose_coverage import CoverageGapViolation

logger = logging.getLogger(__name__)

#: The three plan-mode actions, ordered left-to-right for the arrow
#: toggle. Index ``0`` (``approve``) is the default highlight — the
#: operator's most common plan-mode response.
_ACTIONS: tuple[str, ...] = ("approve", "edit", "reject")


@dataclass(frozen=True)
class PlanWaveRow:
    """One wave row under an iter in the plan tree.

    Attributes:
        wave_id: The wave id (e.g. ``P26-I01-W20``).
        title: The wave title.
        deps: Ordered dep wave ids (empty when the wave has none).
    """

    wave_id: str
    title: str
    deps: tuple[str, ...]


@dataclass(frozen=True)
class PlanIterRow:
    """One iter row under the phase in the plan tree.

    Attributes:
        iter_id: The iter id (e.g. ``P26-I01``).
        title: The iter title.
        waves: Ordered wave rows under this iter.
        eu: Summed planned effort (EU) across the iter's waves, derived
            from each wave's ``effort_bucket`` via the canonical
            :data:`~eawf.workflow.estimation.buckets.BUCKET_EU` mapping. A
            wave with no bucket contributes ``0``.
    """

    iter_id: str
    title: str
    waves: tuple[PlanWaveRow, ...]
    eu: float


@dataclass(frozen=True)
class DroppedClause:
    """One dropped-detail clause surfaced by the propose lint (EAWF022).

    A brief span the propose render found covered by neither an emitted
    success criterion nor an explicit deferral — i.e. detail the generator
    silently dropped. The plan preview names each so the operator sees the
    lost intent before approving the plan.

    Attributes:
        span_id: The dropped span id (e.g. ``U-007``), surfaced verbatim
            so it ties back to the propose-render finding.
        reason: The lowercase-led, period-free explanation of why the span
            is a finding (from the EAWF022 rule).
    """

    span_id: str
    reason: str


@dataclass(frozen=True)
class PlanTree:
    """A resolved plan-mode preview: a phase plus its iter / wave rows.

    Attributes:
        phase_id: The proposed phase id (e.g. ``P26``).
        title: The phase title rendered at the tree root.
        iters: Ordered iter rows; each carries its own wave rows.
        dropped_detail: Ordered dropped-detail clauses from the propose
            lint (EAWF022). Empty when the propose render found no silently
            dropped brief span — the preview then omits the section.
    """

    phase_id: str
    title: str
    iters: tuple[PlanIterRow, ...]
    dropped_detail: tuple[DroppedClause, ...] = ()

    @property
    def total_eu(self) -> float:
        """Return the summed planned EU across every iter, rounded to 2 dp."""
        return round(sum(it.eu for it in self.iters), 2)


def build_plan_tree(
    state: State | None,
    phase_id: str,
    *,
    dropped_detail: Iterable[CoverageGapViolation] = (),
) -> PlanTree:
    """Resolve *phase_id* to a :class:`PlanTree` from *state*.

    Walks the phase's ``iter_ids`` and each iter's ``wave_ids`` in their
    stored order, building the hierarchical row aggregate the overlay
    renders. Each iter row carries its summed planned EU (the per-wave
    ``effort_bucket`` resolved through the canonical
    :func:`~eawf.workflow.estimation.buckets.wave_estimate_eu`), and the
    tree carries the propose lint's dropped-detail findings. An
    unresolvable phase (or a ``None`` state) yields a tree with the phase
    id and no children so the preview stays total even when the state and
    the proposal briefly disagree (e.g. mid daemon-push) — the host
    disables ``approve`` on an empty tree.

    Args:
        state: The bound state, or ``None`` when no state is loaded.
        phase_id: The proposed phase id to render.
        dropped_detail: The EAWF022 dropped-detail findings the propose
            render produced for this phase (empty when the generator
            dropped no brief span). Each names a span the operator should
            see before approving.

    Returns:
        The resolved plan tree, or a childless fallback for an unknown
        phase.
    """
    dropped = _dropped_clauses(dropped_detail)
    if state is None:
        return PlanTree(phase_id=phase_id, title=phase_id, iters=(), dropped_detail=dropped)
    phase = state.phases.get(phase_id)
    if phase is None:
        return PlanTree(phase_id=phase_id, title=phase_id, iters=(), dropped_detail=dropped)
    iter_rows: list[PlanIterRow] = []
    for iter_id in phase.iter_ids:
        iteration = state.iters.get(iter_id)
        if iteration is None:
            continue
        wave_rows: list[PlanWaveRow] = []
        iter_eu = 0.0
        for wave_id in iteration.wave_ids:
            wave = state.waves.get(wave_id)
            if wave is None:
                continue
            wave_rows.append(PlanWaveRow(wave_id=wave.id, title=wave.title, deps=tuple(wave.deps)))
            iter_eu += wave_estimate_eu(wave)
        iter_rows.append(
            PlanIterRow(
                iter_id=iteration.id,
                title=iteration.title,
                waves=tuple(wave_rows),
                eu=round(iter_eu, 2),
            )
        )
    return PlanTree(
        phase_id=phase.id,
        title=phase.title,
        iters=tuple(iter_rows),
        dropped_detail=dropped,
    )


def _dropped_clauses(
    findings: Iterable[CoverageGapViolation],
) -> tuple[DroppedClause, ...]:
    """Normalise EAWF022 findings into the overlay's :class:`DroppedClause` rows.

    Maps each :class:`~eawf.platform.lint.eawf022_propose_coverage.CoverageGapViolation`
    onto the span id + reason the preview names, preserving the propose
    render's source order.

    Args:
        findings: The EAWF022 dropped-detail findings (possibly empty).

    Returns:
        One :class:`DroppedClause` per finding, in order.
    """
    return tuple(
        DroppedClause(span_id=finding.snippet, reason=finding.reason) for finding in findings
    )


class PlanPreviewModal(ModalScreen[str]):
    """Plan-mode wave-DAG preview with a 3-option AUQ (Esc = reject).

    Built with a pre-resolved :class:`PlanTree`; the host screen builds
    the tree from ``app.state`` via :func:`build_plan_tree` when
    ``/roadmap propose`` returns ``status=needs_user``. The modal owns the
    presentation, the ``←`` / ``→`` action toggle, and returns the chosen
    action label (``approve`` / ``edit`` / ``reject``) through the dismiss
    value so the host can run ``eawf roadmap apply`` on ``approve`` only.
    ``approve`` is disabled when the tree carries no waves.
    """

    DEFAULT_CSS: ClassVar[str] = """
    PlanPreviewModal {
        align: center middle;
    }
    PlanPreviewModal > #plan-box {
        width: 80%;
        max-width: 110;
        height: auto;
        max-height: 85%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    PlanPreviewModal .plan-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    PlanPreviewModal #plan-tree {
        height: auto;
        max-height: 60%;
        margin-top: 1;
    }
    PlanPreviewModal .plan-rollup {
        height: 1;
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }
    PlanPreviewModal #plan-dropped {
        height: auto;
        margin-top: 1;
        color: $warn;
    }
    PlanPreviewModal .plan-dropped-head {
        text-style: bold;
        color: $warn;
        height: 1;
    }
    PlanPreviewModal .plan-dropped-clause {
        color: $warn;
        height: auto;
    }
    PlanPreviewModal #plan-actions {
        height: 1;
        align-horizontal: center;
        margin-top: 1;
    }
    PlanPreviewModal .plan-action {
        width: auto;
        margin: 0 2;
        color: $text-muted;
    }
    PlanPreviewModal .plan-action.-selected {
        color: $accent;
        text-style: bold reverse;
    }
    PlanPreviewModal .plan-action.-disabled {
        color: $text-muted;
        text-style: dim;
    }
    PlanPreviewModal .plan-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``←`` / ``→`` cycle the action, ``Enter`` confirms, ``Esc``
    #: rejects. Vim ``h`` / ``l`` ride the arrows per the operator keymap.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "move(-1)", "prev", show=False),
        Binding("right", "move(1)", "next", show=False),
        Binding("h", "move(-1)", "prev", show=False),
        Binding("l", "move(1)", "next", show=False),
        Binding("enter", "confirm", "confirm", show=False),
        Binding("escape", "reject", "reject", show=False),
    ]

    #: Index into :data:`_ACTIONS` of the highlighted action (``0`` =
    #: approve).
    selected: reactive[int] = reactive(0)

    def __init__(self, plan: PlanTree) -> None:
        """Construct the preview for a pre-resolved plan tree.

        Args:
            plan: The plan tree to render (built by the host screen from
                the proposed phase id + the bound state).
        """
        super().__init__()
        self._plan = plan
        self._has_waves = any(it.waves for it in plan.iters)

    def compose(self) -> ComposeResult:
        """Yield title, tree, EU rollup, dropped-detail, action row, hint."""
        with Vertical(id="plan-box"):
            yield Static(f"plan preview: {self._plan.phase_id}", classes="plan-title")
            with VerticalScroll():
                yield self._build_tree_widget()
            yield Static(self._rollup_line(), classes="plan-rollup", id="plan-rollup")
            yield from self._dropped_detail_section()
            with Vertical(id="plan-actions"):
                yield Static("", id="plan-action-row")
            yield Static("[ ←/→ select · Enter confirm · Esc reject ]", classes="plan-hint")

    def _rollup_line(self) -> str:
        """Return the per-iter EU rollup line: each iter's EU + the phase total.

        Names each iter's summed planned EU (``<iter-id> <eu> EU``) and the
        phase total so the operator reads the effort shape of the plan before
        approving. A no-iter plan reports ``0 EU``.

        Returns:
            The single-line EU rollup string.
        """
        if not self._plan.iters:
            return "effort rollup: 0 EU"
        per_iter = " · ".join(f"{it.iter_id} {it.eu:g} EU" for it in self._plan.iters)
        return f"effort rollup: {per_iter} · total {self._plan.total_eu:g} EU"

    def _dropped_detail_section(self) -> ComposeResult:
        """Yield the dropped-detail section when the propose lint flagged spans.

        Renders one clause line per :class:`DroppedClause` under a header so
        the operator sees each silently-dropped brief span before approving.
        Yields nothing when the propose render produced no dropped detail —
        the section is omitted rather than rendered empty.
        """
        if not self._plan.dropped_detail:
            return
        warn = sigils.chrome("attention", mode=self._render_mode())
        with Vertical(id="plan-dropped"):
            yield Static(
                f"{warn} dropped detail ({len(self._plan.dropped_detail)})",
                classes="plan-dropped-head",
            )
            for clause in self._plan.dropped_detail:
                yield Static(
                    f"  {clause.span_id} — {clause.reason}",
                    classes="plan-dropped-clause",
                )

    def _build_tree_widget(self) -> Tree[str]:
        """Build the hierarchical phase → iter → wave Textual tree.

        Returns:
            A :class:`~textual.widgets.Tree` rooted at the phase, with one
            child per iter (its summed EU appended to the iter label) and
            one grandchild per wave (deps appended to the wave label so the
            DAG edges read inline).
        """
        tree: Tree[str] = Tree(f"{self._plan.phase_id} · {self._plan.title}", id="plan-tree")
        tree.root.expand()
        if not self._plan.iters:
            tree.root.add_leaf("(no iters)")
            return tree
        for iter_row in self._plan.iters:
            node = tree.root.add(
                f"{iter_row.iter_id} · {iter_row.title}  ({iter_row.eu:g} EU)", expand=True
            )
            if not iter_row.waves:
                node.add_leaf("(no waves)")
                continue
            for wave_row in iter_row.waves:
                label = f"{wave_row.wave_id} · {wave_row.title}"
                if wave_row.deps:
                    label = f"{label}  ← {', '.join(wave_row.deps)}"
                node.add_leaf(label)
        return tree

    def on_mount(self) -> None:
        """Paint the initial action highlight (approve, or edit if empty)."""
        if not self._has_waves:
            # A no-wave plan cannot be approved; default to ``edit``.
            self.selected = _ACTIONS.index("edit")
        self._repaint_actions()

    def watch_selected(self) -> None:
        """Repaint the action row when the highlight moves."""
        if self.is_mounted:
            self._repaint_actions()

    def _render_mode(self) -> RenderMode:
        """Return the App's resolved render mode, defaulting when unbound.

        Reads :attr:`~eawf.surfaces.tui.app.EaApp.render_mode` so the
        frontier marker picks the right chrome-glyph column. A bare harness
        whose host App carries no ``render_mode`` (a direct construction
        outside the full app) falls back to the shared default.

        Returns:
            The active render mode (``"unicode"`` / ``"ascii"``).
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def _repaint_actions(self) -> None:
        """Rebuild the action row, marking selected + disabled cells.

        The selected action is marked with the shared dispatch chrome glyph
        (the cosmic-terminal frontier pointer), drawn from the single-home
        sigil vocabulary so the marker honours the active render mode rather
        than hardcoding a unicode triangle. ``approve`` carries the
        ``(disabled)`` suffix when the plan has no waves; the highlight
        skips it in :meth:`action_move`.
        """
        row = self.query_one("#plan-action-row", Static)
        frontier = sigils.chrome("dispatch", mode=self._render_mode())
        cells: list[str] = []
        for index, action in enumerate(_ACTIONS):
            disabled = action == "approve" and not self._has_waves
            marker = f"{frontier} " if index == self.selected else "  "
            suffix = " (disabled)" if disabled else ""
            cells.append(f"{marker}{action}{suffix}")
        row.update("    ".join(cells))

    def action_move(self, delta: int) -> None:
        """Move the highlight by *delta*, skipping a disabled ``approve``.

        Args:
            delta: ``-1`` for the previous action, ``+1`` for the next;
                wraps at the ends.
        """
        count = len(_ACTIONS)
        index = (self.selected + delta) % count
        if _ACTIONS[index] == "approve" and not self._has_waves:
            index = (index + delta) % count
        self.selected = index

    def action_confirm(self) -> None:
        """Dismiss with the highlighted action label.

        A highlighted-but-disabled ``approve`` (only reachable if the
        guard is bypassed) is suppressed so an empty plan cannot be
        approved.
        """
        action = _ACTIONS[self.selected]
        if action == "approve" and not self._has_waves:
            return
        logger.info(f"plan_preview action={action!r} phase={self._plan.phase_id}")
        self.dismiss(action)

    def action_reject(self) -> None:
        """Dismiss with ``reject`` (``Esc`` = reject)."""
        logger.info(f"plan_preview action='reject' phase={self._plan.phase_id}")
        self.dismiss("reject")


def open_plan_preview(app: object, plan: PlanTree) -> None:
    """Push the plan-preview overlay onto *app*'s screen stack (cap-checked).

    Routes through the App's modal-cap-aware ``push_modal`` helper when
    present (so the modal-stack depth limit is enforced), falling back to a
    plain ``push_screen`` under a bare harness — mirroring the
    :func:`~eawf.surfaces.tui.screens.help.open_help` pattern. The ``/roadmap
    propose`` palette path and the future ``status=needs_user``
    plan-mode handler both call this.

    Args:
        app: The running App (typed loosely to avoid an import cycle with
            :mod:`eawf.surfaces.tui.app`).
        plan: The plan tree to render (built via :func:`build_plan_tree`).
    """
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        push_modal(PlanPreviewModal(plan))
        return
    push_screen = getattr(app, "push_screen", None)
    if callable(push_screen):
        push_screen(PlanPreviewModal(plan))


__all__ = [
    "PlanIterRow",
    "PlanPreviewModal",
    "PlanTree",
    "PlanWaveRow",
    "build_plan_tree",
    "open_plan_preview",
]
