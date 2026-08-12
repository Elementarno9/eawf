"""Per-cell colour capture of the rotated footer + heartbeat tokens.

The teal -> green ``accent`` / ``primary`` rotation (the W01 keystone) must
reach the two chassis-footer surfaces this wave pins: the footer's
active-mode token + needs_user attention badge, and the embedded heartbeat
dot. The footer + heartbeat are ALREADY structurally correct (the footer is
two rows, the heartbeat keeps its ``*`` bullet) -- this suite is the
colour-aware proof that every load-bearing token resolves the rotated theme
var rather than a hardcoded teal hex.

The existing ``tests/snapshots/tui`` golden harness captures the rendered
screen as plain ASCII text and so discards colour entirely -- it cannot
prove a footer cell carries the green accent. This suite mounts the footer
(which owns the heartbeat) under a Textual ``run_test`` Pilot, reads the
per-cell foreground hex off the compositor's rendered strips, and asserts:

1. The footer's **active-mode token** renders the green ``$accent``
   (``#16b384`` on the dark theme). ``build_mode_row`` wraps the active
   token in ``[$accent][b]...[/]``, so the accent rotation lands on it.
2. The footer's **needs_user badge**, when at least one pause is pending,
   renders the ``$warn`` attention colour (``#e69f00``) via the
   ``-attention`` class -- unchanged by the rotation (warn stayed gold).
3. The embedded **heartbeat dot** renders the green ``$accent`` (``#16b384``)
   when healthy and the ``$err`` red (``#d55e00``) when ``degraded`` flips
   on (the ``Heartbeat.-degraded`` class swaps to ``$error``).

Each assertion fails if a token reverts to the old teal ``#56b6c2``: the
accent + heartbeat cells are explicitly asserted NOT to carry the teal, so a
regression that re-pins the old palette is caught.

Colour capture is deterministic here: the footer + heartbeat colours are a
pure function of the registered theme's ``variables`` map (the bullet pulse
is driven to a lit frame before capture), with no clock / git / daemon
volatility, so no golden file is needed -- the assertion is the resolved hex.
"""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.widgets import Static

from eawf.surfaces.tui.modes.registry import MODE_REGISTRY
from eawf.surfaces.tui.widgets.footer import Footer
from eawf.surfaces.tui.widgets.heartbeat import HEARTBEAT_GLYPH, Heartbeat

from ._palette_harness import PaletteHarnessApp

#: The rotated dark-theme ``$accent`` green (teal -> green; see W01).
_DARK_ACCENT: str = "#16b384"

#: The dark-theme ``$warn`` attention gold (unchanged by the rotation).
_DARK_WARN: str = "#e69f00"

#: The red the degraded heartbeat dot resolves. The heartbeat's ``-degraded``
#: rule paints the Textual built-in ``$error`` (so the widget stays mountable
#: in a bare host that registers no Eae theme), which the dark theme seeds
#: from the canonical rotated ``error="#d55e00"`` palette hex -- Textual rounds
#: it to ``#d45e00`` when it derives the built-in semantic colour. The dot is
#: therefore the rotated ERROR red (not the old teal); the one-channel rounding
#: off the canonical ``#d55e00`` is Textual's, not a hardcoded value.
_DARK_HEARTBEAT_ERR: str = "#d45e00"

#: The OLD teal ``$accent`` the rotation replaced. No reskinned token may
#: resolve this hex -- the assertions pin its absence so a revert is caught.
_OLD_TEAL: str = "#56b6c2"

#: A per-cell foreground map: char -> the set of foreground hexes (or None)
#: it carries across the widget's rendered strips.
_CellMap = dict[str, set[str | None]]


class _FooterHarness(PaletteHarnessApp):
    """Minimal app mounting only the footer (which owns the heartbeat).

    Surface-scoped on purpose: the colour claim is proven against the footer
    + its embedded heartbeat in isolation, not the whole scope-screen graph,
    so the capture stays decoupled from the clock / git / daemon volatility
    that lives on the full app.
    """

    def compose(self) -> ComposeResult:
        yield Footer(id="ftr")


def _cell_fg_hexes(widget: object) -> _CellMap:
    """Map each rendered character to the set of foreground hexes it carries.

    Walks the widget's rendered strips off the screen compositor and reads
    the truecolor foreground of every segment, bucketing by the segment's
    characters. A cell with no explicit foreground maps to ``None``. This is
    the W01 capture pattern, reused so the two colour-aware suites prove the
    rotation the same way.

    Args:
        widget: A mounted widget exposing ``screen._compositor``.

    Returns:
        ``{char: {hex_or_None, ...}}`` over the widget's visible cells.
    """
    strips = widget.screen._compositor.render_strips()  # type: ignore[attr-defined]
    out: _CellMap = {}
    for strip in strips:
        for segment in strip._segments:
            style = segment.style
            fg: str | None = None
            if style is not None and style.color is not None:
                trip = style.color.get_truecolor()
                fg = f"#{trip.red:02x}{trip.green:02x}{trip.blue:02x}"
            for char in segment.text:
                out.setdefault(char, set()).add(fg)
    return out


def test_footer_active_mode_token_renders_rotated_accent_not_teal() -> None:
    """The footer's active-mode token carries the green ``$accent``, not teal.

    The active mode's first title character is wrapped in ``[$accent][b]`` by
    ``build_mode_row``, so the rotation lands on its glyph. The assertion pins
    the green ``#16b384`` present and the old teal ``#56b6c2`` absent.
    """

    async def body() -> None:
        # Pick a non-Home mode whose title's first character is distinct from
        # every other mode's, so the captured cell is unambiguous.
        active = MODE_REGISTRY[1]  # autopilot -> leading lowercased char 'a'
        app = _FooterHarness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.active_mode = active.name
            await pilot.pause()
            await pilot.pause()
            modes = app.query_one(".footer-modes", Static)
            cell_map = _cell_fg_hexes(modes)
            # The active token's lead char (lowercased title initial) carries
            # the accent span. Locate it via the active title's first letter.
            lead_char = active.title.lower()[0]
            lead_fg = cell_map.get(lead_char)
            assert lead_fg is not None, "active-mode token lead char not rendered"
            assert _DARK_ACCENT in lead_fg, (
                f"active-mode token resolved {lead_fg}, expected rotated accent {_DARK_ACCENT}"
            )
            # The rotation regression guard: the old teal must NOT appear on
            # the active-mode token cell.
            assert _OLD_TEAL not in lead_fg, (
                f"active-mode token reverted to old teal {_OLD_TEAL}: {lead_fg}"
            )

    asyncio.run(body())


def test_footer_needs_user_badge_renders_warn_not_teal() -> None:
    """The needs_user attention badge carries the ``$warn`` gold via -attention.

    A pending pause flips the ``-attention`` class, whose ``color: $warn`` rule
    paints the badge gold (``#e69f00`` on dark). The assertion pins the warn
    hex present and the teal absent.
    """

    async def body() -> None:
        app = _FooterHarness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.pending_pauses = 2
            await pilot.pause()
            await pilot.pause()
            badge = app.query_one(".footer-needs-user", Static)
            cell_map = _cell_fg_hexes(badge)
            # The badge digit (the pause count) is part of the warn-coloured
            # text; assert the '2' carries the warn hex.
            count_fg = cell_map.get("2")
            assert count_fg is not None, "needs_user badge count not rendered"
            assert _DARK_WARN in count_fg, (
                f"needs_user badge resolved {count_fg}, expected warn {_DARK_WARN}"
            )
            assert _OLD_TEAL not in count_fg, (
                f"needs_user badge carries old teal {_OLD_TEAL}: {count_fg}"
            )

    asyncio.run(body())


def test_heartbeat_dot_renders_rotated_accent_when_healthy_not_teal() -> None:
    """The embedded heartbeat dot carries the green ``$accent`` when healthy.

    The dot's ``Heartbeat`` ``DEFAULT_CSS`` paints ``color: $accent`` by
    default, so a lit (healthy) dot resolves the rotated green ``#16b384``.
    The assertion pins the green present and the old teal absent.
    """

    async def body() -> None:
        app = _FooterHarness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            hb = footer.query_one("#heartbeat", Heartbeat)
            # Force a lit frame so the bullet is on-screen at capture time
            # (the pulse timer may have toggled it off by the time we read).
            hb._lit = True
            hb.ack()
            await pilot.pause()
            await pilot.pause()
            cell_map = _cell_fg_hexes(footer)
            dot_fg = cell_map.get(HEARTBEAT_GLYPH)
            assert dot_fg is not None, "heartbeat dot not rendered"
            assert _DARK_ACCENT in dot_fg, (
                f"healthy heartbeat dot resolved {dot_fg}, expected rotated accent {_DARK_ACCENT}"
            )
            assert _OLD_TEAL not in dot_fg, (
                f"healthy heartbeat dot reverted to old teal {_OLD_TEAL}: {dot_fg}"
            )

    asyncio.run(body())


def test_heartbeat_dot_renders_err_when_degraded() -> None:
    """The embedded heartbeat dot carries the rotated ERROR red when degraded.

    Flipping ``degraded`` adds the ``Heartbeat.-degraded`` class, whose
    ``color: $error`` rule swaps the dot to the rotated error red on dark
    (``#d45e00`` -- the Textual-rounded built-in seeded from the canonical
    ``error="#d55e00"`` palette hex; see :data:`_DARK_HEARTBEAT_ERR`). The
    assertion pins that red present, the green accent absent (the swap
    actually happened), and the old teal absent (no revert).
    """

    async def body() -> None:
        app = _FooterHarness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            hb = footer.query_one("#heartbeat", Heartbeat)
            hb.degraded = True
            hb._lit = True
            hb.ack()
            await pilot.pause()
            await pilot.pause()
            cell_map = _cell_fg_hexes(footer)
            dot_fg = cell_map.get(HEARTBEAT_GLYPH)
            assert dot_fg is not None, "degraded heartbeat dot not rendered"
            assert _DARK_HEARTBEAT_ERR in dot_fg, (
                f"degraded heartbeat dot resolved {dot_fg}, expected rotated "
                f"error red {_DARK_HEARTBEAT_ERR}"
            )
            # The swap actually happened: no stale healthy accent on the dot.
            assert _DARK_ACCENT not in dot_fg, (
                f"degraded heartbeat dot still carries the healthy accent {_DARK_ACCENT}: {dot_fg}"
            )
            # The rotation regression guard: the old teal never appears.
            assert _OLD_TEAL not in dot_fg, (
                f"degraded heartbeat dot carries old teal {_OLD_TEAL}: {dot_fg}"
            )

    asyncio.run(body())
