"""Tests for the shared ASCII-art Seal honest-empty hero.

The research board leads its honest-empty surface with the centered half-block
ASCII-art Seal; :func:`~eawf.surfaces.tui.widgets.empty_state.seal_empty_hero`
+ :func:`~eawf.surfaces.tui.widgets.empty_state.seal_hero_css` spread that same
brand mark to the four other honest-empty surfaces (autopilot / feed /
agent-watch / evidence) so the seal reads consistently rather than only a small
muted brand glyph. These tests pin the two shared helpers (the wrapper id /
class / children, the load-bearing ``width: 1fr; text-align: center`` centering
CSS) and assert each of the four modes' unicode honest-empty pane renders the
seal art HORIZONTALLY CENTERED on the screen midline -- the operator-approved
research-board centering, not a left-anchored block.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.containers import Vertical
from textual.widgets import Static

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.snapshot import capture_screen_text, settle_screen
from eawf.surfaces.tui.widgets.empty_state import (
    SEAL_HERO_CLASS,
    SEAL_HERO_ID,
    seal_empty_hero,
    seal_hero_css,
)
from eawf.surfaces.tui.widgets.seal import SEAL_ART_CLASS, SEAL_ART_ID, SEAL_ART_LINES

#: The empty-repo state fixture -- no waves / sessions / reports / events, so
#: every mode below renders its honest-empty pane.
_EMPTY_REPO = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)

#: Screen width the seal centers within (the standard mode-snapshot size).
_WIDTH = 120

#: The seal's widest art row (the central star band spans the full 42 columns);
#: its leading whitespace in the captured frame pins the block's left edge.
_WIDEST_ROW = SEAL_ART_LINES[9].strip()  # "██   ██████████    ████    ██████████   ██"


# --------------------------------------------------------------------------
# seal_empty_hero -- wrapper structure
# --------------------------------------------------------------------------


def test_seal_empty_hero_wraps_art_over_body_with_default_id() -> None:
    """The hero is a Vertical of the art Static then the body, id'd by default."""
    body = Static("headline", id="my-empty")
    hero = seal_empty_hero(body)
    assert isinstance(hero, Vertical)
    assert hero.id == SEAL_HERO_ID
    assert SEAL_HERO_CLASS in hero.classes
    # Pre-mount, Textual stashes constructor children on ``_pending_children``.
    children = list(hero._pending_children)
    assert len(children) == 2
    # The art Static leads (carrying the seal-art id + class), then the body.
    assert children[0].id == SEAL_ART_ID
    assert SEAL_ART_CLASS in children[0].classes
    assert children[1] is body


def test_seal_empty_hero_id_none_omits_id_keeps_class() -> None:
    """``hero_id=None`` drops the id (dynamic-remount race) but keeps the class.

    The autopilot list re-mounts the hero on every rebuild; a fixed id would
    collide with the not-yet-torn-down prior hero, so the dynamic host passes
    ``hero_id=None`` and relies on the class for the centering hook.
    """
    hero = seal_empty_hero(Static("x"), hero_id=None)
    assert hero.id is None
    assert SEAL_HERO_CLASS in hero.classes


# --------------------------------------------------------------------------
# seal_hero_css -- scoped + load-bearing centering
# --------------------------------------------------------------------------


def test_seal_hero_css_scopes_both_rules_to_the_screen_selector() -> None:
    """Both the wrapper + the art rules are prefixed so they never leak."""
    css = seal_hero_css("FooScreen")
    # The wrapper rule (class, not id) + the art rule both carry the prefix.
    assert f"FooScreen .{SEAL_HERO_CLASS}" in css
    assert f"FooScreen .{SEAL_ART_CLASS}" in css
    # The wrapper centers + stacks its children.
    assert "align: center middle" in css


def test_seal_hero_css_art_rule_is_full_width_and_center_text() -> None:
    """The art rule pins ``width: 1fr; text-align: center`` -- the centering key.

    A fixed ``width: 42`` would left-anchor the symmetric block; the full-width
    Static + centered text is what centers the 42-wide art on the screen
    midline (the regression this rule forecloses).
    """
    css = seal_hero_css("FooScreen")
    assert "width: 1fr" in css
    assert "text-align: center" in css
    # The seal renders in the brand accent on the show-through surface.
    assert "color: $accent" in css
    # The art Static is sized to the 19 art rows so the disc never clips.
    assert f"height: {len(SEAL_ART_LINES)}" in css


# --------------------------------------------------------------------------
# Cross-mode: every honest-empty pane leads with the centered seal art
# --------------------------------------------------------------------------


def _seal_centered_in_frame(frame: str) -> bool:
    """Return whether the seal's widest row centers on the screen midline.

    Finds every captured row carrying the widest art band and checks each one's
    visible block centers within one cell of the midline -- a left-anchored seal
    (a regression to a fixed ``width: 42``) fails this.

    Args:
        frame: The captured screen text (one row per line; trailing whitespace
            trimmed per row by :func:`capture_screen_text`).

    Returns:
        ``True`` when the seal renders AND every widest row is centered.
    """
    matches = [line for line in frame.splitlines() if _WIDEST_ROW in line]
    if not matches:
        return False
    for line in matches:
        lead = len(line) - len(line.lstrip(" "))
        content = line.rstrip()
        center = lead + (len(content) - lead) / 2
        if abs(center - _WIDTH / 2) > 1.0:
            return False
    return True


def _assert_mode_empty_seal_centered(digit: str) -> None:
    """Switch to the *digit* mode on the empty repo and assert a centered seal.

    Args:
        digit: The mode-row digit key that switches into the mode under test.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_EMPTY_REPO)
        async with app.run_test(size=(_WIDTH, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(digit)
            await settle_screen(pilot)
            screen = app.screen
            # The seal art Static is mounted on the unicode honest-empty pane.
            assert screen.query(f"#{SEAL_ART_ID}"), f"no seal art on mode {digit}"
            frame = capture_screen_text(app)
            assert _seal_centered_in_frame(frame), f"seal not centered on mode {digit}"

    asyncio.run(body())


def test_autopilot_empty_pane_leads_with_centered_seal() -> None:
    """The autopilot dry-frontier honest-empty pane centers the seal art."""
    _assert_mode_empty_seal_centered("2")


def test_feed_empty_pane_leads_with_centered_seal() -> None:
    """The feed pre-event honest-empty pane centers the seal art."""
    _assert_mode_empty_seal_centered("7")


def test_agent_watch_empty_pane_leads_with_centered_seal() -> None:
    """The agent-watch no-session honest-empty pane centers the seal art."""
    _assert_mode_empty_seal_centered("8")


def test_evidence_empty_pane_leads_with_centered_seal() -> None:
    """The evidence no-reports honest-empty pane centers the seal art."""
    _assert_mode_empty_seal_centered("6")
