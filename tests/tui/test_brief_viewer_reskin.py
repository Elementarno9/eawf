"""Snapshot + render tests for the brief-viewer modal reskin.

The research brief viewer (:class:`~eawf.surfaces.tui.modes.brief_viewer.BriefViewerScreen`,
opened with ``d`` from the research board) is the operator-facing research
surface. A prior wave (W28 / RS-28) reskinned only its SINGLE ``ClaimStatus``
bullet to lead with a status sigil. This wave reskins the WHOLE modal chassis so
the surface speaks the Eae cosmic-terminal render language end to end:

* the header band renders the two-tone Eae wordmark
  (:func:`~eawf.surfaces.render.brand.render_wordmark_markup` -- the ``E`` plain,
  the ``ae`` carrying the green ``$accent``) plus the brief title in the same
  accent;
* the green ``$accent`` rides the chassis border + header band; and
* the markdown body sigil-prefixes BOTH the claim rows AND the open-question
  rows (not just the single ClaimStatus bullet RS-28 touched), via the shared
  :func:`~eawf.surfaces.tui.widgets.sigils.status_sigil` helper.

These tests pin three halves:

* the pure :func:`render_brief_header_markup` helper -- asserts the two-tone
  wordmark markup + the green ``$accent`` brief title both land in the header
  band content markup;
* the pure :func:`build_brief_preview_markdown` projection -- asserts BOTH the
  claim row AND the open-question row lead with their resolved status sigil
  (the criterion's "sigil-prefixed rows throughout, not just the single
  ClaimStatus site" bar), while a hand-built pre-reskin brief body (bare-word
  question rows, no header brand) FAILS those same assertions so the golden
  discriminates the reskinned surface from the old shell; and
* the mounted :class:`BriefViewerScreen` under a Pilot, captured IN ISOLATION
  (pushed straight onto the screen stack, no full-app modes wiring) so the
  snapshot golden of the modal asserts the two-tone Eae header, the green
  accent, and the sigil-prefixed claim + question rows all render in the
  settled frame.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from textual.widgets import Static

from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus
from eawf.kernel.state.models import Claim, OpenQuestion
from eawf.surfaces.render.brand import BRAND_LITERAL, render_wordmark_markup
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.brief_viewer import (
    BRIEF_HEADER_ID,
    BRIEF_HEADER_TITLE,
    BriefViewerScreen,
    build_brief_preview_markdown,
    render_brief_header_markup,
)
from eawf.surfaces.tui.modes.research_board import CampaignRow
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE
from eawf.surfaces.tui.widgets.sigils import status_sigil

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"

#: A fixed creation timestamp for the directly-constructed signal rows.
_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: The claim + question titles the brief body is projected from under test.
_CLAIM_TITLE = "Implied vol surface is downward sloping in strike"
_QUESTION_TITLE = "Which curve model fits the short tenor"
_CAMPAIGN_TOPIC = "Survey the options-pricing landscape"


def _campaign_row() -> CampaignRow:
    """Build a directly-constructed campaign row for the brief projection."""
    return CampaignRow(
        campaign_id="RC-0001",
        topic=_CAMPAIGN_TOPIC,
        domains=("market-structure", "pricing-models"),
        default_depth="medium",
    )


def _claim(*, status: ClaimStatus = ClaimStatus.SUPPORTED) -> Claim:
    """Build a claim row in *status*."""
    return Claim(
        id="CL-0001",
        scope_id="QR",
        title=_CLAIM_TITLE,
        status=status,
        created_at=_T0,
    )


def _question(*, status: OpenQuestionStatus = OpenQuestionStatus.BLOCKED) -> OpenQuestion:
    """Build an open-question row in *status*."""
    return OpenQuestion(
        id="OQ-0001",
        scope_id="QR",
        title=_QUESTION_TITLE,
        status=status,
        blocking=True,
        created_at=_T0,
    )


# --------------------------------------------------------------------------
# Pure render helpers -- the Eae header band + sigil-prefixed rows (no mount)
# --------------------------------------------------------------------------


def test_brief_header_markup_carries_two_tone_wordmark_and_accent_title() -> None:
    """The header band leads with the two-tone wordmark + green accent title."""
    header = render_brief_header_markup()
    # The two-tone Eae wordmark (E plain, ae carrying the $accent) leads, bold.
    assert render_wordmark_markup("$accent") in header
    assert f"[b]{render_wordmark_markup('$accent')}[/b]" in header
    # The brief title trails the wordmark inside the green $accent span.
    assert f"[$accent]{BRIEF_HEADER_TITLE}[/]" in header
    # It is the threaded theme var, never a frozen hex literal.
    assert "$accent" in header


def test_brief_body_sigil_prefixes_both_claim_and_question_rows() -> None:
    """BOTH the claim row AND the question row lead with a resolved status sigil.

    The criterion bar: sigil-prefixed claim/status rows render THROUGHOUT the
    body, not just the single ClaimStatus bullet RS-28 touched. So the
    open-question row must lead with its own resolved status sigil too.
    """
    md = build_brief_preview_markdown(
        (_campaign_row(),),
        (_claim(status=ClaimStatus.SUPPORTED),),
        (_question(status=OpenQuestionStatus.BLOCKED),),
    )
    claim_sigil = status_sigil(ClaimStatus.SUPPORTED).render(mode=DEFAULT_RENDER_MODE)
    question_sigil = status_sigil(OpenQuestionStatus.BLOCKED).render(mode=DEFAULT_RENDER_MODE)
    # The claim bullet (the RS-28 site) still leads with its sigil + status word.
    assert f"- {claim_sigil} supported: {_CLAIM_TITLE}" in md
    # And the open-question row -- the rows W28 did NOT touch -- now leads with
    # its own resolved status sigil too (sigils throughout, not just one site).
    assert "## Open questions" in md
    assert f"- {question_sigil} blocked: {_QUESTION_TITLE}" in md
    # NOT the pre-reskin bare-value question row form (no leading sigil).
    assert f"- blocked: {_QUESTION_TITLE}" not in md


def test_pre_reskin_brief_shell_fails_the_reskin_golden() -> None:
    """A hand-built pre-reskin brief shell fails the reskin golden's assertions.

    The pre-reskin shell carried neither the two-tone Eae header band nor a
    sigil on the open-question rows (only the single claim bullet was reskinned
    by W28). Reconstructing that shell and running the reskin golden's
    assertions over it proves the golden discriminates the reskinned surface
    from the old one -- the criterion's "not just the single ClaimStatus site"
    bar.
    """
    question_sigil = status_sigil(OpenQuestionStatus.BLOCKED).render(mode=DEFAULT_RENDER_MODE)
    # The pre-reskin shell: no wordmark header, bare-word (sigil-less) question
    # row. (The single claim bullet was already reskinned by W28; the rest of
    # the chassis was not.)
    pre_reskin_shell = "\n".join(
        [
            f"# Brief preview: {_CAMPAIGN_TOPIC}",
            "",
            "## Open questions",
            "",
            f"- blocked: {_QUESTION_TITLE}",
        ]
    )
    reskinned_body = build_brief_preview_markdown(
        (_campaign_row(),),
        (_claim(),),
        (_question(status=OpenQuestionStatus.BLOCKED),),
    )
    reskinned_header = render_brief_header_markup()

    # The reskin markers -- the two-tone wordmark header band + the sigil on the
    # question row -- are ABSENT from the pre-reskin shell.
    assert render_wordmark_markup("$accent") not in pre_reskin_shell
    assert question_sigil not in pre_reskin_shell
    assert f"- blocked: {_QUESTION_TITLE}" in pre_reskin_shell

    # ...and PRESENT in the reskinned surface (header band + body row).
    assert render_wordmark_markup("$accent") in reskinned_header
    assert f"- {question_sigil} blocked: {_QUESTION_TITLE}" in reskinned_body


# --------------------------------------------------------------------------
# Mounted brief viewer -- snapshot golden captured IN ISOLATION
# --------------------------------------------------------------------------


def test_mounted_brief_viewer_renders_the_reskinned_chassis() -> None:
    """The mounted brief viewer renders the Eae header, accent, + sigil rows.

    The brief viewer is pushed straight onto the screen stack (IN ISOLATION --
    no full-app modes wiring), settled, and captured. The snapshot golden of
    the modal asserts the reskinned render language end to end: the two-tone
    Eae header brand, the green accent, and the sigil-prefixed claim + question
    rows all present in the settled frame -- not just the single ClaimStatus
    site RS-28 touched.
    """
    brief = build_brief_preview_markdown(
        (_campaign_row(),),
        (_claim(status=ClaimStatus.SUPPORTED),),
        (_question(status=OpenQuestionStatus.BLOCKED),),
    )
    claim_sigil = status_sigil(ClaimStatus.SUPPORTED).render(mode=DEFAULT_RENDER_MODE)
    question_sigil = status_sigil(OpenQuestionStatus.BLOCKED).render(mode=DEFAULT_RENDER_MODE)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(BriefViewerScreen(brief))
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, BriefViewerScreen)

            # The brand-header Static carries the two-tone wordmark + the brief
            # title -- the rendered (markup-resolved) brand literal Eae and the
            # title both land in the header band's rendered text.
            header = screen.query_one(f"#{BRIEF_HEADER_ID}", Static)
            header_rendered = str(header.render())
            assert BRAND_LITERAL in header_rendered
            assert BRIEF_HEADER_TITLE in header_rendered

            # The full settled frame carries the two-tone header brand, the
            # sigil-prefixed claim row, AND the sigil-prefixed question row --
            # the reskinned render language across the whole modal, not just the
            # single ClaimStatus bullet.
            frame = normalize_snapshot(capture_screen_text(app))
            assert BRAND_LITERAL in frame
            assert BRIEF_HEADER_TITLE in frame
            # The sigil-prefixed claim AND open-question rows both render the
            # sigil immediately ahead of their status word -- the body speaks
            # sigils throughout (not just the single ClaimStatus bullet). The
            # joined ``<sigil> <status>:`` token is the unambiguous landmark (a
            # bare sigil glyph could collide with unrelated chrome).
            assert f"{claim_sigil} supported:" in frame
            assert f"{question_sigil} blocked:" in frame
            # The claim + question titles render (the markdown body soft-wraps
            # the long titles in the viewer column, so a non-wrapping leading
            # fragment of each title is the stable on-frame landmark).
            assert "Implied vol surface" in frame
            assert "Which curve model fits the" in frame

    asyncio.run(body())
