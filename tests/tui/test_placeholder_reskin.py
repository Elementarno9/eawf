"""Snapshot + render tests for the placeholder mode reskin (P30-I02-W33).

The placeholder mode (:class:`~eawf.surfaces.tui.modes.placeholder.PlaceholderModeScreen`)
is the honest-empty base for any mode whose per-pane wave has not landed.
The cosmic-terminal reskin speaks the calm honest-empty voice the rest of
the surface uses, so an unbuilt mode reads as INTENTIONALLY empty rather
than broken: a green ``$accent`` pending sigil (the hollow dotted ring --
a "not-yet-here, on the roadmap" mark, NOT a spinner or any other
false-busy chrome) leads the byte-for-byte ``<title> - coming soon`` copy
in the same green accent, with a muted intentional-empty sub-note beneath.

These tests pin two halves:

* the pure :func:`render_placeholder_notice` helper -- asserts the green
  ``$accent`` span, the pending sigil glyph, and the byte-for-byte
  coming-soon copy all land in the reskinned content markup, and that the
  pre-reskin shell (a plain ``<title> - coming soon`` Static with no accent
  and no sigil) FAILS those same assertions, so the golden discriminates;
  and
* the mounted placeholder mode under a Pilot, captured IN ISOLATION
  (pushed straight onto the screen stack, no full-app modes wiring) so the
  snapshot golden of the placeholder body asserts the calm voice renders
  -- the green accent, the pending sigil, and the verbatim copy all present
  in the settled frame, with no fabricated busy chrome.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before
asserting.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Static

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.placeholder import (
    COMING_SOON_SUFFIX,
    INTENTIONAL_EMPTY_NOTE,
    PlaceholderModeScreen,
    coming_soon_text,
    render_placeholder_notice,
)
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"

#: The mode title the placeholder is seeded with under test.
_MODE_TITLE = "Trust"

#: The unicode pending sigil (the default render mode) the reskin leads the
#: notice with -- the hollow dotted ring shared with a not-yet-started
#: lifecycle row.
_PENDING_SIGIL_UNICODE = glyph(Sigil.PENDING, mode="unicode")
_PENDING_SIGIL_ASCII = glyph(Sigil.PENDING, mode="ascii")


# --------------------------------------------------------------------------
# Pure render helper -- the calm voice in content markup (no Textual mount)
# --------------------------------------------------------------------------


def test_coming_soon_copy_is_byte_for_byte_preserved() -> None:
    """The coming-soon words are exactly ``<title> - coming soon``."""
    assert COMING_SOON_SUFFIX == " - coming soon"
    assert coming_soon_text(_MODE_TITLE) == "Trust - coming soon"


def test_render_notice_carries_green_accent_sigil_and_verbatim_copy() -> None:
    """The reskinned notice leads with the green-accent pending sigil + copy."""
    notice = render_placeholder_notice(_MODE_TITLE, mode="unicode")
    # The byte-for-byte coming-soon copy survives verbatim inside the markup.
    assert "Trust - coming soon" in notice
    # The pending sigil leads the line...
    assert _PENDING_SIGIL_UNICODE in notice
    # ...inside the green $accent span (the reskin's calm honest-empty voice),
    # with the sigil + copy sharing the one accent span.
    assert f"[$accent]{_PENDING_SIGIL_UNICODE} Trust - coming soon[/]" in notice
    # The muted intentional-empty sub-note names the state as deliberate.
    assert f"[$muted]{INTENTIONAL_EMPTY_NOTE}[/]" in notice


def test_render_notice_threads_ascii_render_mode() -> None:
    """An ``ascii`` render mode swaps the pending sigil to its ASCII column."""
    notice = render_placeholder_notice(_MODE_TITLE, mode="ascii")
    assert _PENDING_SIGIL_ASCII in notice
    # The copy is unchanged across the glyph-column flip.
    assert "Trust - coming soon" in notice


def test_pre_reskin_shell_fails_the_reskin_golden() -> None:
    """The pre-reskin shell (plain copy, no sigil/accent) fails the golden.

    The pre-reskin placeholder body was a bare ``<title> - coming soon``
    Static carrying neither the green ``$accent`` span nor the pending
    sigil. Reconstructing that shell and running the reskin golden's
    assertions over it proves the golden discriminates the reskinned voice
    from the old shell -- the criterion's "the pre-reskin shell fails the
    golden" bar.
    """
    pre_reskin_shell = coming_soon_text(_MODE_TITLE)  # "Trust - coming soon"
    reskinned = render_placeholder_notice(_MODE_TITLE, mode="unicode")

    # The shared, unchanged copy is present in BOTH (the words are preserved).
    assert "Trust - coming soon" in pre_reskin_shell
    assert "Trust - coming soon" in reskinned

    # But the reskin markers -- green $accent span + pending sigil -- are
    # absent from the pre-reskin shell, so the golden's reskin assertions
    # fail against it while passing against the reskinned notice.
    assert "[$accent]" not in pre_reskin_shell
    assert _PENDING_SIGIL_UNICODE not in pre_reskin_shell
    assert "[$accent]" in reskinned
    assert _PENDING_SIGIL_UNICODE in reskinned


# --------------------------------------------------------------------------
# Mounted placeholder mode -- snapshot golden captured IN ISOLATION
# --------------------------------------------------------------------------


def test_mounted_placeholder_renders_the_calm_honest_empty_voice() -> None:
    """The mounted placeholder body renders the green sigil + verbatim copy.

    The placeholder mode is pushed straight onto the screen stack (IN
    ISOLATION -- no full-app modes wiring), settled, and captured. The
    snapshot golden of the placeholder body asserts the calm honest-empty
    voice renders: the pending sigil and the byte-for-byte coming-soon copy
    are present in the settled frame, with no fabricated busy chrome.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(PlaceholderModeScreen(_MODE_TITLE))
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, PlaceholderModeScreen)

            # Capture the placeholder body's rendered Static directly so the
            # golden is anchored to the notice content (not the chrome).
            notice = screen.query_one(".placeholder-notice", Static)
            rendered = str(notice.render())
            # The byte-for-byte coming-soon copy renders verbatim.
            assert "Trust - coming soon" in rendered
            # The green-accent pending sigil leads the calm line -- the reskin
            # voice (default unicode render mode).
            assert _PENDING_SIGIL_UNICODE in rendered
            # The intentional-empty sub-note names the state as deliberate.
            assert INTENTIONAL_EMPTY_NOTE in rendered

            # And the full settled frame carries the sigil + copy too (the
            # snapshot golden of the placeholder mode), proving the calm voice
            # reaches the rendered terminal text, not just the widget markup.
            frame = normalize_snapshot(capture_screen_text(app))
            assert _PENDING_SIGIL_UNICODE in frame
            assert "Trust - coming soon" in frame

    asyncio.run(body())
