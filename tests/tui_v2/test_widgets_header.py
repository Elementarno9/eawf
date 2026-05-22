"""Unit + Pilot tests for the C06 shared ``Header`` widget (P26-W18).

Covers the pure render source (:func:`build_breadcrumb`,
:func:`runtime_cell_text`, :func:`render_header`) including the
None-state fallback frame, the brand prefix + breadcrumb separator, the
idle/active runtime cell (D29), and a Pilot-driven paint under the real
palette confirming the ``Eä`` brand reaches the rendered screen.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
from textual.app import ComposeResult

from eawf.state.enums import ScopeKind
from eawf.state.models import State
from eawf.tui_v2.widgets.header import (
    BRAND,
    CRUMB_SEP,
    DEFAULT_PROJECT_CODE,
    RUNTIME_IDLE,
    Header,
    build_breadcrumb,
    render_header,
    runtime_cell_text,
)

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_EMPTY_REPO = _FIXTURES / "01-empty-repo.json"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "tui_v2" / "theme.tcss"


class _Harness(PaletteHarnessApp):
    """Production-style host loading the real palette CSS."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield Header(id="hdr")


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


def _state_with_active_wave() -> State:
    """Return the active fixture with a wave id pinned into ``current``."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["current"]["active_wave_ids"] = ["P01-I01-W01"]
    return State.model_validate(payload)


# --------------------------------------------------------------------------
# build_breadcrumb — None fallback + populated + workspace-no-project
# --------------------------------------------------------------------------


def test_build_breadcrumb_none_state_falls_back_to_default_code() -> None:
    assert build_breadcrumb(None) == DEFAULT_PROJECT_CODE


def test_build_breadcrumb_repo_fixture_includes_scope_and_code() -> None:
    crumb = build_breadcrumb(_load(_EMPTY_REPO))
    assert "repo" in crumb
    assert "QR" in crumb
    assert CRUMB_SEP in crumb


def test_build_breadcrumb_includes_phase_when_active() -> None:
    crumb = build_breadcrumb(_load(_PHASE_ITER_WAVE))
    assert "P01" in crumb
    # scope + code + phase => two separators.
    assert crumb.count(CRUMB_SEP.strip()) == 2


def test_build_breadcrumb_workspace_uses_default_code_when_no_project() -> None:
    crumb = build_breadcrumb(_load(_WORKSPACE))
    assert "workspace" in crumb
    assert DEFAULT_PROJECT_CODE in crumb


# --------------------------------------------------------------------------
# runtime_cell_text — idle (D29) vs active
# --------------------------------------------------------------------------


def test_runtime_cell_text_none_state_is_idle() -> None:
    assert runtime_cell_text(None) == f"runtime: {RUNTIME_IDLE}"


def test_runtime_cell_text_no_active_wave_is_idle() -> None:
    # The empty-repo fixture has no active wave => idle (D29).
    assert runtime_cell_text(_load(_EMPTY_REPO)) == f"runtime: {RUNTIME_IDLE}"


def test_runtime_cell_text_active_wave_is_active() -> None:
    assert runtime_cell_text(_state_with_active_wave()) == "runtime: active"


# --------------------------------------------------------------------------
# render_header — brand prefix present in every frame
# --------------------------------------------------------------------------


def test_render_header_none_state_has_brand_and_default_code() -> None:
    rendered = render_header(None)
    assert BRAND in rendered
    assert DEFAULT_PROJECT_CODE in rendered
    assert RUNTIME_IDLE in rendered


def test_render_header_populated_has_brand_left_of_breadcrumb() -> None:
    rendered = render_header(_load(_PHASE_ITER_WAVE))
    # Brand sits outside-left of the breadcrumb.
    assert rendered.index(BRAND) < rendered.index("QR")


# --------------------------------------------------------------------------
# Pilot paint — brand + breadcrumb render under the real palette
# --------------------------------------------------------------------------


def test_header_paints_brand_and_breadcrumb() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            app.query_one("#hdr", Header).state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            rendered = app.export_screenshot()
            assert BRAND in rendered
            assert "QR" in rendered

    asyncio.run(body())


def test_header_repaints_on_state_revision() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            header = app.query_one("#hdr", Header)
            # Fresh frame falls back to the default code.
            assert DEFAULT_PROJECT_CODE in app.export_screenshot()
            header.state = _load(_EMPTY_REPO)
            await pilot.pause()
            assert "QR" in app.export_screenshot()

    asyncio.run(body())


def test_header_state_is_read_only_to_fixture(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(_EMPTY_REPO.read_bytes())
    before = target.read_bytes()
    # Building a breadcrumb never touches the file.
    build_breadcrumb(_load(target))
    assert target.read_bytes() == before


def test_header_render_returns_brand_after_assignment() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            header = app.query_one("#hdr", Header)
            header.state = _load(_EMPTY_REPO)
            await pilot.pause()
            assert ScopeKind.REPO.value in str(header.render())

    asyncio.run(body())
