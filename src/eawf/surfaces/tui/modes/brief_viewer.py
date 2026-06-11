"""The research brief viewer -- a routing :class:`MarkdownViewer` + its modal.

The Research board's ``d`` (brief) key projects the active scope's research
signal into a chassis-shaped markdown document (:func:`build_brief_preview_markdown`)
and pushes a :class:`BriefViewerScreen`, which renders it through a
:class:`BriefMarkdownViewer`. The viewer is a small subclass that routes brief
link clicks three ways:

* an ``#ref-N`` jump scrolls the rendered reference row near the top of the
  viewport (Textual's :meth:`Markdown.goto_anchor` resolves heading slugs only,
  so the W13 HTML ``<a id="ref-N">`` row anchors need a custom scroll);
* a whole-span EAWF entity reference opens its typed reference card; and
* anything else is an inert, logged no-op.

This unit lives in its own module (rather than inside ``research_board``) so the
brief-rendering concern stays separate from the three-pane board orchestrator
and neither module crowds the per-module length cap.
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePath
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import MarkdownViewer, Static
from textual.widgets.markdown import MarkdownBlock

from eawf.platform.artifacts.references import Citation
from eawf.surfaces.render.artifact_chassis import (
    link_inline_citations,
    render_references,
)
from eawf.surfaces.render.brand import render_wordmark_markup
from eawf.surfaces.render.link_wrap import iter_refs
from eawf.surfaces.tui.modes.research_board import (
    EMPTY_NOTICE,
    NONE_YET,
    CampaignRow,
    has_research_signal,
)
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE
from eawf.surfaces.tui.widgets.sigils import status_sigil

if TYPE_CHECKING:
    from eawf.kernel.state.models import Claim, OpenQuestion

logger = logging.getLogger(__name__)

#: Id of the :class:`MarkdownViewer` the brief d-tab mounts (addressable so a
#: Pilot test can target the viewer + its table-of-contents).
BRIEF_VIEWER_ID: str = "research-brief-viewer"

#: Id of the brand-header :class:`~textual.widgets.Static` the brief modal docks
#: above the viewer (addressable so a Pilot test can target the two-tone Eae
#: wordmark band). The band leads the reskinned chassis so the brief reads in the
#: Eae cosmic-terminal render language end to end, not just on the single
#: ClaimStatus bullet a prior wave touched.
BRIEF_HEADER_ID: str = "research-brief-header"

#: Id of the vertical shell that stacks the header band over the viewer as one
#: centred block (a screen ``align: center middle`` aligns sibling children
#: independently, so the band + viewer share one wrapper to read as a single
#: framed modal rather than two overlapping boxes).
_BRIEF_SHELL_ID: str = "research-brief-shell"

#: Label trailing the two-tone Eae wordmark in the brief modal's header band --
#: a fixed brief-surface title (the modal carries no per-brief heading of its
#: own; the brief body's H1 names the topic). It rides the green ``$accent`` span
#: so it tracks the active palette rather than a frozen hex.
BRIEF_HEADER_TITLE: str = "Research brief"

#: Href prefix of an in-document reference jump (``#ref-N``) -- the target a
#: rendered inline ``[N]`` marker and a reference-row self-link both point at
#: (:func:`eawf.surfaces.render.artifact_chassis.render_references` /
#: :func:`eawf.surfaces.render.link_wrap.linkify_citations`). Routed to a custom
#: row scroll rather than Textual's heading-slug ``goto_anchor``, which cannot
#: resolve the HTML ``<a id="ref-N">`` anchors those rows carry.
_REF_HREF_PREFIX: str = "#ref-"

#: Matches the leading ``[N]`` self-link token a rendered reference row begins
#: with, so the viewer can locate the row block for a ``#ref-N`` jump by its
#: number (the inline ``<a id="ref-N">`` anchor is stripped by the markdown
#: parser, so the row block carries no queryable ``#ref-N`` id -- the rendered
#: ``[N]`` self-link text is the stable on-block landmark instead).
_REFERENCE_ROW_RE: re.Pattern[str] = re.compile(r"^\s*\[(?P<n>[1-9][0-9]*)\]")

#: Repo-relative provenance sources the brief preview is projected from -- the
#: real on-disk inputs the board reads, cited honestly so the preview's
#: ``## References`` rows point at where its content came from (not fabricated
#: citations).
_BRIEF_STATE_REF: str = ".ea/state.json"
_BRIEF_CAMPAIGN_STORE_REF: str = ".ea/store/research_campaign.jsonl"

#: Brief-preview body shown when the scope carries no research signal.
BRIEF_EMPTY_MARKDOWN: str = "\n".join(
    [
        "# Brief preview",
        "",
        "## Summary",
        "",
        f"_{EMPTY_NOTICE}_ -- no staged campaign, claim, or open question yet.",
        "",
    ]
)


def render_brief_header_markup() -> str:
    """Render the brief modal's header band as Textual content markup.

    The band leads with the two-tone Eae wordmark
    (:func:`~eawf.surfaces.render.brand.render_wordmark_markup` -- the ``E``
    plain, the ``ae`` carrying the accent) wrapped in a ``[b]...[/b]`` bold span,
    exactly as the shared chassis :class:`~eawf.surfaces.tui.widgets.header.Header`
    composes it, then trails the :data:`BRIEF_HEADER_TITLE` inside the same green
    ``[$accent]...[/]`` span. The accent is threaded as the theme ``$accent`` var
    (never a frozen hex) so the band tracks the active palette.

    Returns:
        A Textual content-markup string: the bold two-tone wordmark, a gap, then
        the brief title in the green accent span.
    """
    wordmark = render_wordmark_markup("$accent")
    return f"[b]{wordmark}[/b]  [$accent]{BRIEF_HEADER_TITLE}[/]"


def build_brief_preview_markdown(
    campaigns: tuple[CampaignRow, ...],
    claims: tuple[Claim, ...],
    questions: tuple[OpenQuestion, ...],
) -> str:
    """Render the scope's research signal as a brief-preview markdown document.

    Projects the staged campaign, the claim ledger, and the open questions into
    a chassis-shaped brief preview: an H1, a ``## Summary`` of the campaign +
    counts, a ``## Claims`` list, a ``## Open questions`` list, and a numbered
    ``## References`` block
    (via :func:`eawf.surfaces.render.artifact_chassis.render_references`) whose
    rows cite the real on-disk sources the preview was projected from. Each claim
    AND open-question row leads with its resolved status sigil
    (:func:`~eawf.surfaces.tui.widgets.sigils.status_sigil`) so the body speaks
    the Eae sigil language throughout, not just on the single claim bullet. The
    inline ``[N]`` summary markers are linkified to their ``#ref-N`` anchors
    (:func:`link_inline_citations`) so the brief, mounted in a
    :class:`BriefMarkdownViewer`, fast-travels from a marker to its row. An empty
    scope renders :data:`BRIEF_EMPTY_MARKDOWN`.

    Args:
        campaigns: The staged campaign rows for the scope.
        claims: The state-resident claim ledger rows.
        questions: The state-resident open-question rows.

    Returns:
        The brief-preview markdown body (a single document, headings + rows).
    """
    if not has_research_signal(campaigns, claims, questions):
        return BRIEF_EMPTY_MARKDOWN
    topic = campaigns[0].topic if campaigns else "research scope"
    citations = [Citation(n=1, ref=_BRIEF_STATE_REF, title="state ledger")]
    if campaigns:
        citations.append(Citation(n=2, ref=_BRIEF_CAMPAIGN_STORE_REF, title="campaign store"))
    summary = (
        f"{len(claims)} claim(s) and {len(questions)} open question(s) in the scope ledger [1]"
    )
    if campaigns:
        summary += f"; {len(campaigns)} staged campaign(s) [2]"
    lines = [
        f"# Brief preview: {topic}",
        "",
        "## Summary",
        "",
        f"{summary}.",
        "",
        "## Claims",
        "",
    ]
    if claims:
        lines.extend(
            f"- {status_sigil(claim.status).render(mode=DEFAULT_RENDER_MODE)} "
            f"{claim.status.value}: {claim.title}"
            for claim in claims
        )
    else:
        lines.append(f"_{NONE_YET}_")
    lines.extend(["", "## Open questions", ""])
    if questions:
        lines.extend(
            f"- {status_sigil(question.status).render(mode=DEFAULT_RENDER_MODE)} "
            f"{question.status.value}: {question.title}"
            for question in questions
        )
    else:
        lines.append(f"_{NONE_YET}_")
    lines.append("")
    lines.extend(render_references(citations))
    lines.append("")
    return link_inline_citations("\n".join(lines))


class BriefMarkdownViewer(MarkdownViewer):
    """A :class:`MarkdownViewer` that routes brief link clicks three ways.

    Textual's :class:`MarkdownViewer` handles every ``Markdown.LinkClicked`` by
    feeding the href to :meth:`MarkdownViewer.go`, whose anchor branch resolves
    only heading slugs via :meth:`Markdown.goto_anchor` and whose else branch
    tries to *load a file* off disk. A research brief carries link shapes neither
    branch serves: a ``#ref-N`` jump (its target is an HTML ``<a id="ref-N">``
    anchor on a list row, not a heading, so ``goto_anchor`` cannot find it) and
    an entity reference whose href is an EAWF URN / code that must open the typed
    reference card (the base would try to read it as a file path). Overriding
    :meth:`go` is the single routing seam -- the base ``LinkClicked`` handler
    funnels every click through ``go``, so claiming it routes all three shapes
    without a second, partly-redundant message handler (a subclass that
    overrides the framework ``_on_*`` handler does not *replace* the base one --
    both run -- so the seam has to be ``go`` itself):

    * **``#ref-N`` jump** -- scrolls the rendered reference row near the top of
      the viewport (:meth:`scroll_to_reference`); it never opens a card.
    * **entity reference** -- an href the link catalog
      (:func:`eawf.surfaces.render.link_wrap.iter_refs`) resolves whole to one
      ``(kind, target)`` routes through the host app's
      ``action_open_ref(kind, target)`` so the typed card mounts.
    * **anything else** -- a plain file path / external URL / other anchor is a
      logged no-op (the brief is one in-memory document with no file
      navigation), so a stray href never crashes the viewer on a missing file.

    The viewer is constructed with ``open_links=False`` so the inner
    :class:`Markdown`'s own ``app.open_url`` auto-open also stays dormant -- this
    subclass is the sole link router.
    """

    def __init__(
        self,
        markdown: str | None = None,
        *,
        show_table_of_contents: bool = True,
        id: str | None = None,
    ) -> None:
        """Construct the routing viewer with auto-open disabled.

        Args:
            markdown: The brief document to render, or ``None`` for empty.
            show_table_of_contents: Whether to show the heading TOC sidebar.
            id: The DOM id of the viewer.
        """
        super().__init__(
            markdown,
            show_table_of_contents=show_table_of_contents,
            id=id,
            open_links=False,
        )

    async def go(self, location: str | PurePath) -> None:
        """Route a clicked *location* to a row scroll, a card, or a no-op.

        Overrides :meth:`MarkdownViewer.go` -- the single funnel the base
        ``Markdown.LinkClicked`` handler calls -- so the three brief link shapes
        are routed in one place and the base file-load branch never runs.

        Args:
            location: The clicked link target (a ``#ref-N`` anchor, an EAWF
                entity reference, or some other href).
        """
        href = str(location)
        if href.startswith(_REF_HREF_PREFIX):
            anchor = href[1:]
            self.scroll_to_reference(anchor)
            logger.info(f"go ref_jump anchor={anchor!r}")
            return
        if self._route_entity_ref(href):
            return
        logger.debug(f"go ignored href={href!r}")

    def scroll_to_reference(self, anchor: str) -> bool:
        """Scroll the reference row for *anchor* (``ref-N``) near the top.

        The W13 reference rows carry their jump target as an inline HTML
        ``<a id="ref-N">`` span, which the markdown parser strips -- so the row
        block has no ``#ref-N`` id to query and :meth:`Markdown.goto_anchor`
        (heading slugs only) cannot resolve it. The row is located instead by
        its rendered leading ``[N]`` self-link text and scrolled with
        ``scroll_to_widget(..., top=True)`` so its top lands at the viewport top.

        Args:
            anchor: The reference anchor id (``ref-N``).

        Returns:
            ``True`` when the row was found and scrolled; ``False`` when no row
            matched the anchor (an unparseable anchor or an absent row).
        """
        number = anchor.removeprefix("ref-")
        if not number.isdigit():
            return False
        block = self._reference_row_block(number)
        if block is None:
            logger.debug(f"scroll_to_reference row_absent anchor={anchor!r}")
            return False
        self.scroll_to_widget(block, top=True, animate=False)
        return True

    def _reference_row_block(self, number: str) -> MarkdownBlock | None:
        """Return the rendered reference-row block whose ``[N]`` matches *number*.

        Args:
            number: The decimal citation number (the ``N`` of ``ref-N``).

        Returns:
            The first :class:`MarkdownBlock` whose rendered content begins with
            the ``[N]`` self-link token, or ``None`` when none matches.
        """
        for block in self.document.query(MarkdownBlock):
            content = getattr(block, "_content", None)
            if content is None:
                continue
            match = _REFERENCE_ROW_RE.match(content.plain)
            if match is not None and match.group("n") == number:
                return block
        return None

    def _route_entity_ref(self, href: str) -> bool:
        """Open the typed card for *href* when it is one whole entity reference.

        Args:
            href: The clicked link href.

        Returns:
            ``True`` when *href* resolved to exactly one entity reference and was
            routed through the host app's ``action_open_ref``; ``False`` when it
            is not a whole-span entity reference (a plain path / external URL /
            other anchor the base viewer should handle).
        """
        refs = iter_refs(href)
        if len(refs) != 1:
            return False
        ref = refs[0]
        if ref.start != 0 or ref.end != len(href):
            return False
        action = getattr(self.app, "action_open_ref", None)
        if not callable(action):
            return False
        action(ref.kind, ref.target)
        logger.info(f"_route_entity_ref kind={ref.kind} target={ref.target!r}")
        return True


class BriefViewerScreen(ModalScreen[None]):
    """Modal that renders a research brief through a scrollable MarkdownViewer.

    Docks an Eae brand-header band (:data:`BRIEF_HEADER_ID`, the two-tone
    wordmark + brief title in the green ``$accent``) above a
    :class:`BriefMarkdownViewer` (``show_table_of_contents=True``) over a
    pre-built brief markdown body, so the modal opens in the reskinned
    cosmic-terminal render language and the brief reads with a heading
    table-of-contents and the numbered ``## References`` list renders as an
    ordered list. ``Esc`` closes. The host research board builds the brief body
    from the active scope's signal (:func:`build_brief_preview_markdown`) and
    pushes this screen, so the brief d-tab is a real rendered document rather
    than a flat Static. The viewer subclass routes an inline ``[N]`` /
    ``#ref-N`` click to a reference-row scroll and an entity reference to its
    typed card.
    """

    #: The brief viewer is a dwell-on reading surface, so it opts out of the
    #: modal-depth cap (:attr:`~eawf.surfaces.tui.app.EaApp.MAX_MODAL_DEPTH`).
    #: Reference drills opened off a brief stack as ordinary modals; exempting
    #: the brief itself keeps a brief plus its drill-ins from tripping the cap
    #: toast.
    counts_toward_depth: ClassVar[bool] = False

    DEFAULT_CSS: ClassVar[str] = """
    BriefViewerScreen {
        align: center middle;
    }
    BriefViewerScreen > #research-brief-shell {
        width: 90%;
        max-width: 120;
        height: 85%;
        layout: vertical;
    }
    BriefViewerScreen #research-brief-header {
        width: 100%;
        height: 1;
        background: $panel;
        color: $accent;
        padding: 0 1;
    }
    BriefViewerScreen #research-brief-viewer {
        width: 100%;
        height: 1fr;
        border: round $accent;
        background: $surface;
    }
    """

    #: ``Esc`` closes the brief viewer; the arrow keys keep their native
    #: MarkdownViewer scroll behaviour (deliberately not bound here).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(self, brief_markdown: str) -> None:
        """Construct the viewer over a pre-built brief markdown body.

        Args:
            brief_markdown: The brief document to render (headings + rows),
                built by the host from the scope's research signal.
        """
        super().__init__()
        self._brief_markdown = brief_markdown

    def compose(self) -> ComposeResult:
        """Yield the Eae brand-header band over the routing MarkdownViewer.

        The header band (:data:`BRIEF_HEADER_ID`) leads with the two-tone Eae
        wordmark + brief title (:func:`render_brief_header_markup`) so the modal
        opens in the reskinned cosmic-terminal render language; the viewer below
        renders the brief body (whose claim + open-question rows already carry
        their resolved status sigils) with a heading table-of-contents.
        """
        with Vertical(id=_BRIEF_SHELL_ID):
            yield Static(render_brief_header_markup(), id=BRIEF_HEADER_ID)
            yield BriefMarkdownViewer(
                self._brief_markdown,
                show_table_of_contents=True,
                id=BRIEF_VIEWER_ID,
            )

    def action_close(self) -> None:
        """Dismiss the brief viewer."""
        self.dismiss(None)


__all__ = [
    "BRIEF_EMPTY_MARKDOWN",
    "BRIEF_HEADER_ID",
    "BRIEF_HEADER_TITLE",
    "BRIEF_VIEWER_ID",
    "BriefMarkdownViewer",
    "BriefViewerScreen",
    "build_brief_preview_markdown",
    "render_brief_header_markup",
]
