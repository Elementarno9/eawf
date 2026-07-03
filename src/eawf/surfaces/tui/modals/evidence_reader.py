"""``EvidenceReaderModal`` -- the zoomed, full-screen claim evidence reader.

Opens one research-board claim leaf as a FULL-SCREEN
:class:`~textual.screen.ModalScreen`: the untruncated claim body (title +
long-form description), the claim's supporting sources rendered as a numbered
``[1]`` / ``[2]`` list, the CONFLICTS section (the refuted sibling claims the
board's conflict grouping resolves for the same question, or the honest
``Conflicts: none`` line when none is resolvable), and a PROVENANCE line naming
what produced the claim -- the answered question, its status, when it was
logged, and its source artifact when present. ``Esc`` closes and returns to the
board with the prior selection intact.

The reader is the ZOOM surface for the Research mode's ``Enter`` key over a
claim node: the board already carries a SCOPED claim summary in its center pane,
and this modal is the full-screen reader an at-a-glance summary only gestures
at. It is the evidence-modal seam the W03 pass reserved on
:meth:`~eawf.surfaces.tui.modes.research_board.ResearchBoardModeScreen.action_peek_selected`.

The card content is assembled by pure module functions
(:func:`evidence_source_lines` / :func:`conflict_lines` /
:func:`provenance_line` / :func:`render_evidence_reader`) so the content is
unit-testable without mounting Textual; the screen is a thin scrollable view
over them, mirroring the
:class:`~eawf.surfaces.tui.modals.report_detail.ReportDetailModal` and
:class:`~eawf.surfaces.tui.modals.evidence_drill.EvidenceDrillModal`
conventions. The modal holds no domain logic: it presents a pre-resolved
:class:`~eawf.kernel.state.models.Claim` plus its resolved conflicting siblings
and renders them; it never reaches back into the board or the claim ledger.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import chrome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eawf.kernel.state.models import Claim, OpenQuestion

logger = logging.getLogger(__name__)

#: Rendered in the supporting-sources section when the claim cites none -- the
#: honest-empty sources path (a claim whose evidence is not yet attached).
NO_SOURCES_NOTICE: str = "no supporting sources"

#: Rendered in the conflicts section when no contradicting sibling claim is
#: resolvable for the claim -- the honest "no conflict" line the goal pins so
#: the reader never fabricates a conflict that the ledger does not carry.
NO_CONFLICTS_NOTICE: str = "none"

#: Rendered in the provenance line when the claim answers no tracked question.
NO_QUESTION_NOTICE: str = "answers no tracked question"


def evidence_source_lines(claim: Claim) -> tuple[str, ...]:
    """Return the claim's supporting sources as a numbered ``[N]`` list.

    Each :attr:`~eawf.kernel.state.models.Claim.evidence_refs` entry renders as
    a ``[<n>] <ref>`` line, 1-indexed in ledger order, so the reader shows every
    source that ratifies the claim. A claim carrying no evidence yields a single
    :data:`NO_SOURCES_NOTICE` line -- the honest-empty sources path.

    Args:
        claim: The claim whose supporting sources to render.

    Returns:
        The numbered source lines, or a one-element tuple carrying
        :data:`NO_SOURCES_NOTICE` when the claim cites none.
    """
    if not claim.evidence_refs:
        return (NO_SOURCES_NOTICE,)
    return tuple(f"[{index}] {ref}" for index, ref in enumerate(claim.evidence_refs, start=1))


def conflict_lines(conflicts: Sequence[Claim]) -> tuple[str, ...]:
    """Return one ``<status> -- <title>`` line per contradicting sibling claim.

    Each conflict is a refuted sibling candidate the board's conflict grouping
    resolved for the same question -- a competing answer the campaign set aside
    as contradicted -- so it renders as its status word plus its title. An empty
    *conflicts* (a free-standing claim, or one whose siblings are all supported)
    yields a single :data:`NO_CONFLICTS_NOTICE` line so the section reads
    honestly rather than fabricating a conflict.

    Args:
        conflicts: The contradicting sibling claims, in board order.

    Returns:
        The per-conflict lines, or a one-element tuple carrying
        :data:`NO_CONFLICTS_NOTICE` when there is none.
    """
    if not conflicts:
        return (NO_CONFLICTS_NOTICE,)
    return tuple(f"{conflict.status.value} -- {conflict.title}" for conflict in conflicts)


def provenance_line(claim: Claim, questions: Sequence[OpenQuestion]) -> str:
    """Return the claim's one-line provenance -- what produced it.

    Names the tracked question the claim answers (id + title when it resolves,
    or the honest :data:`NO_QUESTION_NOTICE` when the claim is free-standing),
    then its lifecycle status, when it was logged, and its source artifact id
    when present -- so the reader shows the claim's origin at a glance. The
    fields join with a middle-dot separator; the source artifact is dropped when
    absent so the line never trails a blank ``from``.

    Args:
        claim: The claim whose provenance to render.
        questions: The state-resident open-question rows (to name the answered
            question by title).

    Returns:
        The provenance line.
    """
    if claim.answers_question_id is not None:
        question = next((q for q in questions if q.id == claim.answers_question_id), None)
        if question is not None:
            parts = [f"answers {claim.answers_question_id} ({question.title})"]
        else:
            parts = [f"answers {claim.answers_question_id}"]
    else:
        parts = [NO_QUESTION_NOTICE]
    parts.append(f"status {claim.status.value}")
    parts.append(f"logged {claim.created_at.isoformat()}")
    if claim.source_artifact_id is not None:
        parts.append(f"from {claim.source_artifact_id}")
    return " · ".join(parts)


def render_evidence_reader(
    claim: Claim,
    questions: Sequence[OpenQuestion],
    conflicts: Sequence[Claim],
) -> str:
    """Render the full evidence-reader body for a claim as one text block.

    Lays out the claim body (title + any untruncated description), the
    supporting-sources section (:func:`evidence_source_lines`), the conflicts
    section (:func:`conflict_lines`), and the provenance line
    (:func:`provenance_line`) so the whole reader reads top-to-bottom in one
    block -- the same content the modal paints, exposed pure for tests.

    Args:
        claim: The claim being read.
        questions: The state-resident open-question rows (for the provenance
            question title).
        conflicts: The contradicting sibling claims resolved for the claim.

    Returns:
        The newline-joined evidence-reader block (no trailing newline).
    """
    lines = [claim.title]
    if claim.description is not None:
        lines.append(claim.description)
    lines.append("supporting sources:")
    lines.extend(f"  {line}" for line in evidence_source_lines(claim))
    lines.append("conflicts:")
    lines.extend(f"  {line}" for line in conflict_lines(conflicts))
    lines.append("provenance:")
    lines.append(f"  {provenance_line(claim, questions)}")
    return "\n".join(lines)


class EvidenceReaderModal(ModalScreen[None]):
    """Full-screen claim evidence reader (Esc closes).

    Renders one claim's untruncated body, its numbered supporting sources, the
    resolved conflicting sibling claims (or the honest ``none`` line), and its
    provenance in a full-screen scrollable card. Built thin over the pure render
    helpers so the content is testable without Textual; the modal owns no
    mutation -- it presents a pre-resolved
    :class:`~eawf.kernel.state.models.Claim` plus its resolved conflicts and
    never reaches back into the board or the claim ledger.
    """

    #: One reader overlay per claim at a time -- a re-fired open over the same
    #: claim already on top is a no-op (deduped by
    #: :meth:`~eawf.surfaces.tui.app.EaApp.push_modal` on the dedupe key).
    dedupe_singleton: ClassVar[bool] = False

    DEFAULT_CSS: ClassVar[str] = """
    EvidenceReaderModal {
        align: center middle;
    }
    EvidenceReaderModal > #reader-box {
        width: 100%;
        height: 100%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    EvidenceReaderModal .reader-title {
        text-style: bold;
        color: $accent;
        height: auto;
    }
    EvidenceReaderModal .reader-body {
        height: auto;
        color: $text;
        margin-top: 1;
    }
    EvidenceReaderModal .reader-section {
        text-style: bold;
        color: $accent;
        margin-top: 1;
        height: 1;
    }
    EvidenceReaderModal .reader-row {
        height: auto;
        color: $text;
    }
    EvidenceReaderModal .reader-hint {
        color: $text-muted;
        margin-top: 1;
        height: 1;
    }
    """

    #: ``Esc`` closes the reader overlay; the only binding it owns.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(
        self,
        claim: Claim,
        questions: Sequence[OpenQuestion] = (),
        conflicts: Sequence[Claim] = (),
        *,
        mode: RenderMode = DEFAULT_RENDER_MODE,
    ) -> None:
        """Construct the reader over a pre-resolved *claim* + its conflicts.

        Args:
            claim: The claim to read full-screen.
            questions: The state-resident open-question rows, threaded so the
                provenance line names the answered question by title; defaults
                to none (the provenance then names the question by id alone).
            conflicts: The contradicting sibling claims the board resolved for
                the claim; defaults to none (the honest ``Conflicts: none``
                path).
            mode: The App's resolved render-mode label, threaded into the title
                chrome mark; defaults to the ASCII column for a bare standalone
                render.
        """
        super().__init__()
        self._claim = claim
        self._questions = tuple(questions)
        self._conflicts = tuple(conflicts)
        self._mode = mode
        #: Dedupe key keyed on the claim id so the App push chokepoint
        #: suppresses a duplicate open of the same claim.
        self.dedupe_key = f"evidence-reader:{claim.id}"

    def compose(self) -> ComposeResult:
        """Yield the full-screen card: body, numbered sources, conflicts, provenance."""
        gate = chrome("gate", mode=self._mode)
        title = escape_markup(self._claim.title)
        with VerticalScroll(id="reader-box"):
            yield Static(f"[$accent]{gate}[/] claim: {title}", classes="reader-title")
            if self._claim.description is not None:
                yield Static(
                    f"[$muted]{escape_markup(self._claim.description)}[/]",
                    classes="reader-body",
                )
            yield Static("Supporting sources", classes="reader-section")
            for line in evidence_source_lines(self._claim):
                yield Static(f"  {escape_markup(line)}", classes="reader-row")
            yield Static("Conflicts", classes="reader-section")
            for line in conflict_lines(self._conflicts):
                yield Static(f"  {escape_markup(line)}", classes="reader-row")
            yield Static("Provenance", classes="reader-section")
            yield Static(
                f"  {escape_markup(provenance_line(self._claim, self._questions))}",
                classes="reader-row",
            )
            yield Static("[ Esc to close ]", classes="reader-hint")

    def action_close(self) -> None:
        """Dismiss the reader overlay (``Esc``)."""
        logger.info(f"evidence_reader_close claim={self._claim.id!r}")
        self.dismiss(None)


__all__ = [
    "NO_CONFLICTS_NOTICE",
    "NO_QUESTION_NOTICE",
    "NO_SOURCES_NOTICE",
    "EvidenceReaderModal",
    "conflict_lines",
    "evidence_source_lines",
    "provenance_line",
    "render_evidence_reader",
]
