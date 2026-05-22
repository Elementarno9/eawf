"""Unit + Pilot tests for the C06 shared ``Footer`` + ``Heartbeat`` (P26-W18).

Covers the pure hint formatter (:func:`format_hints`), the Footer's
default + overridden hint strip, the embedded Heartbeat pulse glyph +
degraded-colour class flip (D22), and a Pilot-driven paint confirming
the footer hints + heartbeat dot render under the real palette.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import ComposeResult

from eawf.tui_v2.widgets.footer import (
    DEFAULT_HINTS,
    HEARTBEAT_GLYPH,
    Footer,
    Heartbeat,
    format_hints,
)

from ._palette_harness import PaletteHarnessApp

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "tui_v2" / "theme.tcss"


class _Harness(PaletteHarnessApp):
    """Production-style host loading the real palette CSS."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield Footer(id="ftr")


class _HeartbeatHarness(PaletteHarnessApp):
    """Host mounting a standalone Heartbeat for the degraded-flip test."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield Heartbeat(id="hb")


# --------------------------------------------------------------------------
# format_hints — empty + single + many
# --------------------------------------------------------------------------


def test_format_hints_empty_is_blank() -> None:
    assert format_hints(()) == ""


def test_format_hints_single_has_no_separator() -> None:
    assert format_hints(("q quit",)) == "q quit"


def test_format_hints_many_joined_with_bullet() -> None:
    out = format_hints(("a", "b", "c"))
    assert out == "a  ·  b  ·  c"
    assert out.count("·") == 2


# --------------------------------------------------------------------------
# Footer hints — default + override via set_hints
# --------------------------------------------------------------------------


def test_footer_paints_default_hints() -> None:
    async def body() -> None:
        app = _Harness()
        # Wide canvas: the default hint strip now carries the global
        # w/r/u scope-switch + F5 refresh affordances and overflows 80
        # cols; the real scope screens render at 120.
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            rendered = app.export_screenshot()
            assert "quit" in rendered
            assert "palette" in rendered
            assert "scope" in rendered
            assert "refresh" in rendered

    asyncio.run(body())


def test_footer_set_hints_repaints_strip() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.set_hints(("xyzzy custom",))
            await pilot.pause()
            assert footer.hints == ("xyzzy custom",)
            assert "xyzzy" in app.export_screenshot()

    asyncio.run(body())


def test_footer_default_hints_use_full_key_names() -> None:
    # Operator convention: full key names (no "PgUp" abbreviations).
    joined = format_hints(DEFAULT_HINTS)
    assert "PgUp" not in joined
    assert "PgDn" not in joined


# --------------------------------------------------------------------------
# Footer owns a Heartbeat — D3 shared-chassis bundling
# --------------------------------------------------------------------------


def test_footer_owns_heartbeat() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            assert footer.query(Heartbeat)
            rendered = app.export_screenshot()
            assert HEARTBEAT_GLYPH in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# Heartbeat — pulse glyph + degraded class + ack
# --------------------------------------------------------------------------


def test_heartbeat_paints_glyph_when_lit() -> None:
    async def body() -> None:
        app = _HeartbeatHarness()
        async with app.run_test(size=(20, 3)) as pilot:
            await pilot.pause()
            assert HEARTBEAT_GLYPH in app.export_screenshot()

    asyncio.run(body())


def test_heartbeat_degraded_flag_sets_class() -> None:
    async def body() -> None:
        app = _HeartbeatHarness()
        async with app.run_test(size=(20, 3)) as pilot:
            await pilot.pause()
            hb = app.query_one("#hb", Heartbeat)
            assert not hb.has_class("-degraded")
            hb.degraded = True
            await pilot.pause()
            assert hb.has_class("-degraded")

    asyncio.run(body())


def test_heartbeat_ack_forces_lit_frame() -> None:
    async def body() -> None:
        app = _HeartbeatHarness()
        async with app.run_test(size=(20, 3)) as pilot:
            await pilot.pause()
            hb = app.query_one("#hb", Heartbeat)
            hb._lit = False
            await pilot.pause()
            hb.ack()
            await pilot.pause()
            assert hb._lit is True
            assert HEARTBEAT_GLYPH in app.export_screenshot()

    asyncio.run(body())
