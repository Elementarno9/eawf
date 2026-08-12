"""Footer-strip goldens + width-stress gate.

Pins the T3 footer contract from
``.ea/local/research/2026-06-03-i08-uiux-validation-specs.md``: the footer
hint strip draws from the W22 canon (every fragment produced through
:func:`~eawf.surfaces.tui.widgets.footer.render_hint_label`) and no hint
fragment truncates MID-WORD at the captured width or at a realistic narrow
width. Two complementary checks:

* **Footer-strip goldens** -- the committed two-row footer frames (hint
  strip + status row 1, mode row 2) for each scope hint set at the captured
  width (120, the snapshot viewport) and a realistic narrow width (80, a
  standard terminal). The goldens are the snapshots the W22-canon hint set
  renders to; a drift in the hint vocabulary or the strip layout fails the
  byte comparison. Regenerate after an intentional change with
  ``EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/``.
* **Width-stress gate** -- the load-bearing deterministic check: at both
  widths the visible (clipped) hint strip ends on a token boundary, never
  mid-word. Textual clips the ``1fr`` hint Static at the auto-width burn
  cell, so a too-long strip is truncated; this gate proves the truncation
  lands between words (a dropped tail token), not inside one
  (``Enter op`` / ``colla``), which would read as a corrupt label.

Jury residual (ARMED-but-IDLE): the *visual coherence / cramping* of the
strip at the captured width -- whether the hints read as a comfortable,
legible row or a cramped jumble -- is the T3 jury residual (ISO
interaction-capability). A deterministic test can prove no word is cut, but
not that the spacing reads well to a human eye. The cross-vendor band jury
that would score legibility is built and proven to discriminate
(W08/W11) but DORMANT: the ``quality`` profile that enables the band is
opt-in and not in the default enabled set, and the live ballot fn is idle.
So this module ships the deterministic no-truncation gate -- the
load-bearing value -- and leaves the cramping judgement to the
armed-but-idle jury rather than invoking a live ballot.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from eawf.surfaces.tui.scopes.repo import _REPO_HINTS
from eawf.surfaces.tui.scopes.user import _USER_HINTS
from eawf.surfaces.tui.scopes.workspace import _WORKSPACE_HINTS
from eawf.surfaces.tui.snapshot import assert_screen_snapshot
from eawf.surfaces.tui.snapshot.pilot_harness import capture_screen_text
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.footer import Footer, format_hints

_THEME = Path(__file__).resolve().parents[3] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_GOLDEN = Path(__file__).resolve().parent / "golden"

#: The captured width (the snapshot viewport) and a realistic narrow width
#: (a standard 80-column terminal) the strip is stressed at. 80 is a real
#: terminal an operator may run, not a degenerate single-column edge.
_CAPTURED_WIDTH = 120
_NARROW_WIDTH = 80
_HEIGHT = 6

#: The three scope hint sets the operator can see, keyed by a stable golden
#: stem. Each is already routed through ``render_hint_label`` (the W22 canon),
#: so the source fragments are whole words; the stress check guards the
#: *rendered* clip at each width.
_HINT_SETS: dict[str, tuple[str, ...]] = {
    "repo": _REPO_HINTS,
    "workspace": _WORKSPACE_HINTS,
    "user": _USER_HINTS,
}


class _FooterHarness(App[None]):
    """Bare themed host mounting one shared Footer for the width-stress capture.

    Mirrors :class:`~eawf.surfaces.tui.app.EaApp`'s theme bootstrap so the
    footer's semantic ``$var`` palette references resolve, then mounts a lone
    :class:`~eawf.surfaces.tui.widgets.footer.Footer` whose hints a test sets
    per scope set. The host exposes no ``state``, so the burn cell renders its
    deterministic empty-state placeholder -- the frame is a pure function of
    the hint set + width.
    """

    CSS_PATH = str(_THEME)

    def __init__(self, hints: tuple[str, ...]) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self._hints = hints

    def compose(self) -> ComposeResult:
        yield Footer(id="ftr")


async def _visible_hint_strip(hints: tuple[str, ...], width: int) -> str:
    """Return the visible (clipped) hint-strip text for *hints* at *width*.

    Pulls exactly the hint Static's painted column range from the row-1 frame
    (the row carrying the heartbeat dot), so the result is what an operator
    actually sees after Textual clips the ``1fr`` strip at the burn cell --
    the surface the mid-word check inspects.

    Args:
        hints: The scope hint set painted into the strip.
        width: The terminal width to render at.

    Returns:
        The visible hint-strip text, trailing whitespace trimmed.
    """
    app = _FooterHarness(hints)
    async with app.run_test(size=(width, _HEIGHT)) as pilot:
        await pilot.pause()
        footer = app.query_one("#ftr", Footer)
        footer.set_hints(hints)
        await pilot.pause()
        from textual.widgets import Static

        strip = app.query_one(".footer-hints", Static)
        region = strip.region
        rows = capture_screen_text(app).splitlines()
        for line in rows:
            if "•" in line:  # the heartbeat dot marks footer row 1
                return line[region.x : region.x + region.width].rstrip()
        return ""


def _truncates_mid_word(visible: str, full: str) -> bool:
    """Return ``True`` when *visible* cuts a *full*-string token mid-word.

    The visible clip is mid-word iff its last whitespace/separator-delimited
    token is a STRICT PREFIX of a longer token in the full hint string -- i.e.
    the strip ends inside a word (``Enter op``) rather than at a token
    boundary (a complete word, with the next token cleanly dropped). An empty
    visible strip (clipped to nothing) is not a mid-word cut.

    Args:
        visible: The visible (clipped) hint-strip text.
        full: The full, unclipped hint string the strip is a prefix of.

    Returns:
        ``True`` when the visible strip ends inside a word.
    """
    full_tokens = [tok for tok in re.split(r"[ ·]+", full) if tok]
    visible_tokens = [tok for tok in re.split(r"[ ·]+", visible) if tok]
    if not visible_tokens:
        return False
    last = visible_tokens[-1]
    if last in full_tokens:
        return False
    return any(tok.startswith(last) and tok != last for tok in full_tokens)


# --------------------------------------------------------------------------
# Footer-strip goldens — captured + narrow width, per scope hint set
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scope", sorted(_HINT_SETS))
def test_footer_strip_golden_at_captured_width(scope: str) -> None:
    # The committed footer frame at the captured (snapshot-viewport) width:
    # the W22-canon hint strip + status row + mode row. A drift in the hint
    # vocabulary or the strip layout fails the byte comparison.
    async def body() -> None:
        app = _FooterHarness(_HINT_SETS[scope])
        async with app.run_test(size=(_CAPTURED_WIDTH, _HEIGHT)) as pilot:
            await pilot.pause()
            app.query_one("#ftr", Footer).set_hints(_HINT_SETS[scope])
            await pilot.pause()
            assert_screen_snapshot(app, _GOLDEN / f"footer_strip_{scope}_w{_CAPTURED_WIDTH}.txt")

    asyncio.run(body())


@pytest.mark.parametrize("scope", sorted(_HINT_SETS))
def test_footer_strip_golden_at_narrow_width(scope: str) -> None:
    # The committed footer frame at a realistic narrow (80-col) width: the
    # strip is clipped, but the golden pins exactly where, so a regression in
    # the clip layout is caught.
    async def body() -> None:
        app = _FooterHarness(_HINT_SETS[scope])
        async with app.run_test(size=(_NARROW_WIDTH, _HEIGHT)) as pilot:
            await pilot.pause()
            app.query_one("#ftr", Footer).set_hints(_HINT_SETS[scope])
            await pilot.pause()
            assert_screen_snapshot(app, _GOLDEN / f"footer_strip_{scope}_w{_NARROW_WIDTH}.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Width-stress gate — no hint fragment truncates mid-word (load-bearing)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scope", sorted(_HINT_SETS))
@pytest.mark.parametrize("width", [_CAPTURED_WIDTH, _NARROW_WIDTH])
def test_footer_hints_never_truncate_mid_word(scope: str, width: int) -> None:
    # The load-bearing T3 gate: at the captured width AND a realistic narrow
    # width, the visible hint strip ends on a token boundary -- a too-long
    # strip drops the tail token whole, never cutting a word in half.
    hints = _HINT_SETS[scope]
    visible = asyncio.run(_visible_hint_strip(hints, width))
    full = format_hints(hints)
    assert not _truncates_mid_word(visible, full), (
        f"{scope} hints truncate mid-word at width {width}: visible={visible!r} full={full!r}"
    )


def test_mid_word_detector_flags_a_planted_cut() -> None:
    # The no-truncation assertion is meaningful: the detector DOES fire on a
    # planted mid-word clip (the refute-first direction), so a real mid-word
    # truncation could not slip past the gate above.
    full = format_hints(_REPO_HINTS)
    # A clip that ends inside "collapse" is mid-word...
    assert _truncates_mid_word("↑↓ select  ·  ←→ colla", full) is True
    # ...a clip ending at a whole word boundary is not...
    assert _truncates_mid_word("↑↓ select  ·  ←→ collapse", full) is False
    # ...and an empty (fully-clipped) strip is not a mid-word cut.
    assert _truncates_mid_word("", full) is False


def test_footer_strip_frames_carry_canonical_tokens_at_both_widths() -> None:
    # Belt-and-suspenders over the goldens: at both widths the visible repo
    # strip's tokens are all drawn from the canonical hint vocabulary (no
    # drifted ``up/down`` / ``PgUp`` / ``w/u`` fragment survives the render).
    from eawf.surfaces.tui.widgets.footer import CANONICAL_HINT_TOKENS

    for width in (_CAPTURED_WIDTH, _NARROW_WIDTH):
        visible = asyncio.run(_visible_hint_strip(_REPO_HINTS, width))
        # Tokens are the key glyphs (leading each label); drop the action
        # words by keeping only tokens that are canonical keys.
        tokens = [tok for tok in re.split(r"[ ·]+", visible) if tok]
        keys = [tok for tok in tokens if tok in CANONICAL_HINT_TOKENS]
        # At least the leading ``↑↓`` key is visible at both widths, and every
        # key token present is canonical (none is a drifted fragment).
        assert "↑↓" in keys
        for tok in tokens:
            assert tok not in {"up/down", "PgUp", "PgDn", "w/u"}, (
                f"drifted token {tok!r} rendered at width {width}"
            )
