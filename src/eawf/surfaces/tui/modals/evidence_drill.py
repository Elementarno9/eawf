"""``EvidenceDrillModal`` -- the why-peek evidence drill overlay.

Drills into one close-readiness criterion: a small centred
:class:`~textual.screen.ModalScreen` that renders the criterion's
**evidence chain** -- the criterion id + rolled-up status, then one line
per gate outcome (``G-01 pass`` / ``G-02 fail`` / ...), then the joined
evidence rows (who produced each, its status, its summary). ``Esc`` closes.

The overlay is opened from the Evidence mode's advertised ``p peek`` key,
so the operator can see WHY a criterion landed at its status -- which gate
failed, which evidence row backs it -- without leaving the pane.

The chain content is assembled by pure module functions
(:func:`gate_outcome_lines` / :func:`evidence_chain_lines` /
:func:`render_evidence_chain`) so it is unit-testable without mounting
Textual; the screen is a thin scrollable view over them. The modal holds
no domain logic: it presents a criterion view + its joined evidence rows
and renders the chain.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE
from eawf.surfaces.tui.widgets.sigils import chrome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eawf.kernel.store.kinds.evidence import EvidenceRecord
    from eawf.workflow.verify.models import CriterionView

logger = logging.getLogger(__name__)

#: Rendered in the gate section when the criterion carries no gate results
#: (a legacy / floor criterion) -- the honest-empty gate-chain path.
NO_GATES_NOTICE: str = "no gate outcomes"

#: Rendered in the evidence section when no evidence row joins the criterion
#: -- the honest-empty evidence-chain path.
NO_EVIDENCE_NOTICE: str = "no evidence rows"


def gate_outcome_lines(view: CriterionView) -> tuple[str, ...]:
    """Return one ``<gate-id> <status>`` line per gate outcome of *view*.

    Each :class:`~eawf.workflow.verify.models.GateResult` on the criterion
    renders as a ``<gate_id> <status>`` line, in declaration order, so the
    drill shows exactly which gate landed at which outcome. A criterion with
    no gate results yields a single :data:`NO_GATES_NOTICE` line.

    Args:
        view: The criterion view whose gate outcomes to render.

    Returns:
        The per-gate outcome lines, or a one-element tuple carrying
        :data:`NO_GATES_NOTICE` when the view has no gate results.
    """
    results = view.gate_results
    if not results:
        return (NO_GATES_NOTICE,)
    return tuple(f"{result.gate_id} {result.status}" for result in results)


def evidence_chain_lines(records: Sequence[EvidenceRecord]) -> tuple[str, ...]:
    """Return one ``<produced_by> <status> -- <summary>`` line per record.

    Renders each joined evidence row so the drill shows who produced the
    evidence, its outcome, and its one-line summary. An empty *records*
    yields a single :data:`NO_EVIDENCE_NOTICE` line.

    Args:
        records: The evidence rows joined to the criterion, in display order.

    Returns:
        The per-record chain lines, or a one-element tuple carrying
        :data:`NO_EVIDENCE_NOTICE` when there are no records.
    """
    if not records:
        return (NO_EVIDENCE_NOTICE,)
    return tuple(f"{record.produced_by} {record.status} -- {record.summary}" for record in records)


def render_evidence_chain(view: CriterionView, records: Sequence[EvidenceRecord]) -> str:
    """Render the full evidence chain for a criterion as one text block.

    Lays out the criterion header (``<id> :: <status>``), the gate-outcome
    section (:func:`gate_outcome_lines`), and the evidence-row section
    (:func:`evidence_chain_lines`) so the whole chain reads top-to-bottom in
    one block -- the same content the modal paints, exposed pure for tests.

    Args:
        view: The criterion view being drilled into.
        records: The evidence rows joined to the criterion.

    Returns:
        The newline-joined evidence-chain block (no trailing newline).
    """
    lines = [f"{view.id} :: {view.status}", "gates:"]
    lines.extend(f"  {line}" for line in gate_outcome_lines(view))
    lines.append("evidence:")
    lines.extend(f"  {line}" for line in evidence_chain_lines(records))
    return "\n".join(lines)


class EvidenceDrillModal(ModalScreen[None]):
    """Why-peek overlay rendering one criterion's evidence chain (Esc closes).

    Renders the criterion id + status, the per-gate outcome lines, and the
    joined evidence rows in a scrollable card. Built thin over the pure chain
    helpers so the content is testable without Textual.
    """

    #: One drill overlay at a time -- a re-fired drill over an already-open
    #: drill for the same criterion is a no-op (deduped by
    #: :meth:`~eawf.surfaces.tui.app.EaApp.push_modal` on the dedupe key).
    dedupe_singleton: ClassVar[bool] = False

    DEFAULT_CSS: ClassVar[str] = """
    EvidenceDrillModal {
        align: center middle;
    }
    EvidenceDrillModal > #drill-box {
        width: 70%;
        max-width: 90;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    EvidenceDrillModal .drill-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    EvidenceDrillModal .drill-section {
        text-style: bold;
        color: $accent;
        margin-top: 1;
        height: 1;
    }
    EvidenceDrillModal .drill-row {
        height: auto;
        color: $text;
    }
    EvidenceDrillModal .drill-hint {
        color: $text-muted;
        margin-top: 1;
        height: 1;
    }
    """

    #: ``Esc`` closes the drill overlay; the only binding it owns.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(self, view: CriterionView, records: Sequence[EvidenceRecord] = ()) -> None:
        """Construct the drill for *view* and its joined evidence *records*.

        Args:
            view: The criterion view to drill into.
            records: The evidence rows joined to the criterion (defaults to
                none -- the modal then renders the honest-empty evidence
                section).
        """
        super().__init__()
        self._view = view
        self._records = tuple(records)
        #: Dedupe key keyed on the criterion id so the App push chokepoint
        #: suppresses a duplicate drill of the same criterion.
        self.dedupe_key = f"evidence-drill:{view.id}"

    def compose(self) -> ComposeResult:
        """Yield the scrollable drill card with the criterion evidence chain."""
        gate = chrome("gate", mode=getattr(self.app, "render_mode", DEFAULT_RENDER_MODE))
        with VerticalScroll(id="drill-box"):
            yield Static(
                f"[$accent]{gate}[/] why: {self._view.id} :: {self._view.status}",
                classes="drill-title",
            )
            yield Static("Gate outcomes", classes="drill-section")
            for line in gate_outcome_lines(self._view):
                yield Static(f"  {line}", classes="drill-row")
            yield Static("Evidence chain", classes="drill-section")
            for line in evidence_chain_lines(self._records):
                yield Static(f"  {line}", classes="drill-row")
            yield Static("[ Esc to close ]", classes="drill-hint")

    def action_close(self) -> None:
        """Dismiss the drill overlay (``Esc``)."""
        logger.info(f"evidence_drill_close criterion={self._view.id!r}")
        self.dismiss(None)


__all__ = [
    "NO_EVIDENCE_NOTICE",
    "NO_GATES_NOTICE",
    "EvidenceDrillModal",
    "evidence_chain_lines",
    "gate_outcome_lines",
    "render_evidence_chain",
]
