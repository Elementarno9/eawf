"""CON-063: ``ui.glyphs`` is the one switch between the Unicode and ASCII allocations.

The launcher reads the setting from the layered config and the live console draws every
row through the plain-mode twins when it selects ``ascii``, so no glyph the twin table
maps reaches the terminal. ``auto`` follows the terminal's encoding.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import pytest

from eawf.surfaces.tui.chassis.pilot_harness import capture_screen_text
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.plain import ASCII_TWINS
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import SessionSetup
from eawf.surfaces.tui.launch import persisted_glyphs


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repo root whose only config layer is its own ``.ea/config.yaml``."""
    monkeypatch.setattr(
        "eawf.kernel.config.layered.global_config_path", lambda: tmp_path / "absent.yaml"
    )
    monkeypatch.delenv("EAWF_UI__GLYPHS", raising=False)
    (tmp_path / ".ea").mkdir()
    return tmp_path


def _set(tree: Path, value: str) -> None:
    (tree / ".ea" / "config.yaml").write_text(f"ui:\n  glyphs: {value}\n", encoding="utf-8")


def _stdout(monkeypatch: pytest.MonkeyPatch, encoding: str) -> None:
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding=encoding))


def _live_frame(glyphs: str) -> tuple[list[str], str]:
    """Return the rows the live console painted and the screen text it shows."""

    async def run() -> tuple[list[str], str]:
        app = ConsoleApp(clock=FakeClock(), glyphs=glyphs)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            return list(app.frame_rows), capture_screen_text(app)

    return asyncio.run(run())


def _twinned(text: str) -> set[str]:
    return {ch for ch in text if ch in ASCII_TWINS}


def test_con_063_an_ascii_setting_draws_the_live_frame_with_no_twinned_glyph(
    tree: Path,
) -> None:
    _set(tree, "ascii")
    rows, screen = _live_frame(persisted_glyphs(tree))
    assert rows
    assert all(row.isascii() for row in rows)
    assert screen.isascii()


FIXTURE_DIR = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden" / "fixture"


@pytest.mark.parametrize("size", [(80, 24), (120, 30)], ids=["80x24", "120x30"])
def test_con_063_every_route_draws_pure_ascii_rows_of_one_width(size: tuple[int, int]) -> None:
    """Every route, on the prototype registers, draws ASCII only and keeps the frame grid."""
    fixture = load_fixture(FIXTURE_DIR)

    async def frames() -> dict[str, list[str]]:
        drawn: dict[str, list[str]] = {}
        app = ConsoleApp(fixture, FakeClock(), glyphs="ascii")
        async with app.run_test(size=size) as pilot:
            for route in REGISTRY.ids:
                app.reset(SessionSetup(route=route))
                app.render_frame()
                await pilot.pause()
                drawn[route] = list(app.frame_rows)
        return drawn

    for route, rows in asyncio.run(frames()).items():
        assert all(row.isascii() for row in rows), route
        assert len({len(row) for row in rows}) == 1, route


def test_con_063_a_unicode_setting_keeps_the_glyphs(tree: Path) -> None:
    _set(tree, "unicode")
    rows, _screen = _live_frame(persisted_glyphs(tree))
    assert _twinned("\n".join(rows))


@pytest.mark.parametrize("value", ["unicode", "ascii"])
def test_con_063_an_explicit_setting_is_taken_as_written(
    tree: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _stdout(monkeypatch, "ascii")
    _set(tree, value)
    assert persisted_glyphs(tree) == value


@pytest.mark.parametrize(("encoding", "expected"), [("utf-8", "unicode"), ("ascii", "ascii")])
def test_con_063_auto_follows_the_terminal_encoding(
    tree: Path, monkeypatch: pytest.MonkeyPatch, encoding: str, expected: str
) -> None:
    _stdout(monkeypatch, encoding)
    _set(tree, "auto")
    assert persisted_glyphs(tree) == expected


def test_con_063_an_unrecognised_value_reads_as_auto(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stdout(monkeypatch, "ascii")
    _set(tree, "braille")
    assert persisted_glyphs(tree) == "ascii"


def test_con_063_no_config_layer_reads_as_auto(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stdout(monkeypatch, "utf-8")
    assert persisted_glyphs(tree) == "unicode"


def test_con_063_the_console_refuses_an_unknown_allocation() -> None:
    with pytest.raises(ValueError, match="glyph allocation"):
        ConsoleApp(clock=FakeClock(), glyphs="braille")
