"""Render-mode flip test for the block-eighths bar.

Covers the W25 flip: a unicode->ascii render-mode flip swaps every
block-eighths bar to an ASCII fallback glyph. Two surfaces:

* the pure :mod:`~eawf.surfaces.render.mode` flip helper -- every
  block-eighths glyph maps to the ``#`` fill, every blank cell to ``-``, so
  no block glyph survives; and
* a Pilot-driven live flip under the real :class:`~eawf.surfaces.tui.app.EaApp`
  -- flipping ``render_mode`` from the unicode set to ASCII removes every
  block-eighths glyph the W24 bar swap painted into the mounted widgets.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.models import State
from eawf.surfaces.render.bars import BLOCK_EIGHTHS, render_block_bar
from eawf.surfaces.render.mode import (
    ASCII,
    ASCII_EMPTY,
    ASCII_FULL,
    UNICODE,
    render_bar,
    to_ascii_fallback,
)
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.roadmap_tree import RoadmapTree
from eawf.surfaces.tui.widgets.status_pane import StatusPane

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _PHASE_ITER_WAVE.is_file(), f"missing fixture: {_PHASE_ITER_WAVE}"


def _has_block(text: str) -> bool:
    """Return ``True`` if *text* carries any block-eighths glyph."""
    return any(ch in BLOCK_EIGHTHS for ch in text)


def _state_phase_half_closed() -> State:
    """Return the active fixture with a 2-wave iter, one CLOSED (1/2 fill)."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    opened = payload["phases"]["P01"]["opened_at"]
    payload["iters"]["P01-I01"]["wave_ids"] = ["P01-I01-W01", "P01-I01-W02"]
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = opened
    payload["waves"]["P01-I01-W02"] = {
        "id": "P01-I01-W02",
        "iter_id": "P01-I01",
        "title": "second",
        "status": "in_progress",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [],
        "opened_at": opened,
        "closed_at": None,
    }
    return State.model_validate(payload)


def _tree_labels(tree: RoadmapTree) -> list[str]:
    """Flatten every non-root tree node label to a plain string."""
    out: list[str] = []

    def walk(node: object) -> None:
        for child in node.children:  # type: ignore[attr-defined]
            out.append(str(child.label))
            walk(child)

    walk(tree.root)
    return out


# --------------------------------------------------------------------------
# Pure flip helper -- mode.py
# --------------------------------------------------------------------------


def test_to_ascii_fallback_swaps_every_block_glyph() -> None:
    """Every block-eighths glyph flips to the ASCII full fill, blanks to empty."""
    for glyph in BLOCK_EIGHTHS:
        assert to_ascii_fallback(glyph) == ASCII_FULL
    assert to_ascii_fallback(" ") == ASCII_EMPTY


def test_to_ascii_fallback_leaves_no_block_glyph() -> None:
    """A flipped bar carries no block-eighths glyph at any fill ratio."""
    for ratio in (0.0, 0.13, 0.5, 0.875, 1.0):
        block_bar = render_block_bar(ratio, width=10)
        flipped = to_ascii_fallback(block_bar)
        assert not _has_block(flipped)
        assert set(flipped) <= {ASCII_FULL, ASCII_EMPTY}


def test_to_ascii_fallback_rejects_non_bar_glyph() -> None:
    """A non-bar character raises ``ValueError``."""
    with pytest.raises(ValueError, match="not a block-eighths bar glyph"):
        to_ascii_fallback("x")


def test_render_bar_unicode_is_block_eighths() -> None:
    """Unicode mode renders the block-eighths bar."""
    assert render_bar(0.5, mode=UNICODE, width=10) == render_block_bar(0.5, width=10)


def test_render_bar_ascii_is_fallback() -> None:
    """ASCII mode renders the flipped fallback of the same fill."""
    assert render_bar(0.5, mode=ASCII, width=10) == to_ascii_fallback(
        render_block_bar(0.5, width=10)
    )
    assert not _has_block(render_bar(0.5, mode=ASCII, width=10))


def test_render_bar_full_and_empty_flip() -> None:
    """A full bar flips to all ``#``, an empty bar to all ``-``."""
    assert render_bar(1.0, mode=ASCII, width=6) == ASCII_FULL * 6
    assert render_bar(0.0, mode=ASCII, width=6) == ASCII_EMPTY * 6


def test_render_bar_unknown_mode_raises() -> None:
    """An unknown render mode raises ``ValueError``."""
    with pytest.raises(ValueError, match="unknown render mode"):
        render_bar(0.5, mode="braille", width=10)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Live flip under EaApp -- the propagation is total
# --------------------------------------------------------------------------


def _write_state(state: State, tmp_path: Path) -> Path:
    """Write *state* to a temp file and return the path."""
    path = tmp_path / "state.json"
    path.write_bytes(orjson.dumps(state.model_dump(mode="json")))
    return path


def test_live_flip_removes_every_block_bar(tmp_path: Path) -> None:
    """Flipping render_mode unicode->ascii removes every block-eighths bar.

    Mounts the repo screen (StatusPane + RoadmapTree both carry W24 block
    bars), confirms the unicode mode paints a block glyph, then flips to
    ascii and asserts no block glyph survives in either widget -- the flip
    swapped every block-eighths bar to its ASCII fallback.
    """
    state_path = _write_state(_state_phase_half_closed(), tmp_path)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            app.render_mode = "unicode"  # the live app's unicode mode
            await settle_screen(pilot)
            pane = app.screen.query_one(StatusPane)
            tree = app.screen.query_one(RoadmapTree)
            unicode_text = str(pane.render()) + "".join(_tree_labels(tree))
            assert _has_block(unicode_text), "expected a block bar before the flip"

            app.render_mode = "ascii"
            await settle_screen(pilot)
            pane = app.screen.query_one(StatusPane)
            tree = app.screen.query_one(RoadmapTree)
            ascii_text = str(pane.render()) + "".join(_tree_labels(tree))
            assert not _has_block(ascii_text), f"block bar survived the flip: {ascii_text!r}"
            assert "#" in ascii_text  # the ASCII fallback fill

    asyncio.run(body())
