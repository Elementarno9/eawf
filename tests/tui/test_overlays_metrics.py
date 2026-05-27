"""Tests for the C06 ``MetricsModal`` 3x2 dashboard overlay (P26-W21).

Two layers: pure :func:`parse_metrics_args` arg parsing + the
:data:`TILE_SPECS` grid-inventory contract (without Textual), and
Pilot-driven mounting of the six-tile grid through the ``/metrics`` palette
verb + the modal-stack cap.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
from rich.text import Text
from textual.widgets import Static

from eawf.kernel.state.models import State
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.palette.verbs import split_verb_args
from eawf.surfaces.tui.screens.overlays.metrics import (
    DEFAULT_WINDOW,
    METRIC_WINDOWS,
    TILE_SPECS,
    MetricsArgs,
    MetricsModal,
    parse_metrics_args,
    render_wave_elapsed_tile,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


def _text(widget: Static) -> str:
    rendered = widget.render()
    return rendered.plain if isinstance(rendered, Text) else str(rendered)


def _load_state() -> State:
    return State.model_validate(orjson.loads(_PHASE_ITER_WAVE.read_bytes()))


# --------------------------------------------------------------------------
# TILE_SPECS — 3x2 grid inventory (D9)
# --------------------------------------------------------------------------


def test_tile_specs_count_is_six() -> None:
    # 3x2 grid — exactly six tiles per D9.
    assert len(TILE_SPECS) == 6


def test_tile_specs_ids_are_unique() -> None:
    ids = [spec.tile_id for spec in TILE_SPECS]
    assert len(ids) == len(set(ids))


def test_tile_specs_cover_the_v7_metric_surface() -> None:
    titles = " ".join(spec.title.lower() for spec in TILE_SPECS)
    for needle in ("variance", "burn", "elapsed", "cache", "switchover", "token"):
        assert needle in titles


# --------------------------------------------------------------------------
# parse_metrics_args — window + scope flags
# --------------------------------------------------------------------------


def test_parse_metrics_args_empty_uses_defaults() -> None:
    args = parse_metrics_args("")
    assert args == MetricsArgs(window=DEFAULT_WINDOW, scope_filter=None)


def test_parse_metrics_args_window_flag() -> None:
    assert parse_metrics_args("--window 30d").window == "30d"
    assert parse_metrics_args("--window 90d").window == "90d"


def test_parse_metrics_args_unknown_window_falls_back_to_default() -> None:
    # A bogus window degrades to the default rather than raising.
    assert parse_metrics_args("--window 5y").window == DEFAULT_WINDOW


def test_parse_metrics_args_scope_flag() -> None:
    args = parse_metrics_args("--scope urn:eawf:v1:repo:eawf")
    assert args.scope_filter == "urn:eawf:v1:repo:eawf"


def test_parse_metrics_args_both_flags() -> None:
    args = parse_metrics_args("--window 30d --scope urn:x")
    assert args.window == "30d"
    assert args.scope_filter == "urn:x"


def test_parse_metrics_args_ignores_unknown_flags() -> None:
    # An unrecognised flag is ignored; recognised flags still parse.
    args = parse_metrics_args("--bogus z --window 90d")
    assert args.window == "90d"
    assert args.scope_filter is None


def test_metric_windows_are_the_three_v7_windows() -> None:
    assert METRIC_WINDOWS == ("7d", "30d", "90d")


# --------------------------------------------------------------------------
# tile-elapsed — local state-binding metric (W15)
# --------------------------------------------------------------------------


def test_render_wave_elapsed_tile_uses_compute_wave_elapsed() -> None:
    body = render_wave_elapsed_tile(_load_state())
    assert "median 0.0m" in body
    assert "samples 0" in body


def test_render_wave_elapsed_tile_none_keeps_placeholder() -> None:
    assert render_wave_elapsed_tile(None) == "[$text-muted]awaiting telemetry projection[/]"


# --------------------------------------------------------------------------
# MetricsModal — mounting + the /metrics verb (Pilot)
# --------------------------------------------------------------------------


def test_metrics_modal_mounts_six_tiles() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal())
            await pilot.pause()
            assert isinstance(app.screen, MetricsModal)
            tiles = [app.screen.query_one(f"#{spec.tile_id}", Static) for spec in TILE_SPECS]
            assert len(tiles) == 6
            assert tiles[0].border_title == TILE_SPECS[0].title
            assert "median 0.0m" in _text(app.screen.query_one("#tile-elapsed", Static))

    asyncio.run(body())


def test_metrics_modal_heading_shows_window() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal(MetricsArgs(window="30d", scope_filter=None)))
            await pilot.pause()
            heading = _text(app.screen.query_one(".metrics-title", Static))
            assert "window 30d" in heading

    asyncio.run(body())


def test_metrics_verb_opens_modal_through_cap() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.palette.verbs import VERBS

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            handler = next(v.handler for v in VERBS if v.name == "/metrics")
            handler(app, "--window 90d")
            await pilot.pause()
            assert isinstance(app.screen, MetricsModal)
            assert app.modal_depth() == 1
            assert "window 90d" in _text(app.screen.query_one(".metrics-title", Static))

    asyncio.run(body())


def test_metrics_modal_esc_closes() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal())
            await pilot.pause()
            assert app.modal_depth() == 1
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_metrics_verb_args_split() -> None:
    name, args = split_verb_args("/metrics --window 30d")
    assert name == "/metrics"
    assert args == "--window 30d"


def test_metrics_hint_has_top_margin() -> None:
    # W15 polish: the close-hint sits flush against the tile grid without a
    # gap; a top margin separates it (mirrors the DetailModal hint gap).
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal())
            await pilot.pause()
            hint = app.screen.query_one(".metrics-hint", Static)
            assert hint.styles.margin.top == 1

    asyncio.run(body())
