"""Tests for TUI clickable references, ``/goto``, and ref nav."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Static

from eawf.kernel.state.models import State
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.palette.verbs import _handle_goto, rank_goto_refs
from eawf.surfaces.tui.screens.overlays.detail import DetailCard, DetailModal
from eawf.surfaces.tui.screens.overlays.reference import (
    ReferenceModal,
    resolve_reference,
    tooltip_for_text,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_WAVE_ID = "P01-I01-W01"


def _state() -> State:
    return State.model_validate_json(_PHASE_ITER_WAVE.read_text())


def test_resolve_reference_returns_card_for_each_catalog_kind() -> None:
    state = _state()
    targets = {
        "repo": "QR",
        "project": "QR",
        "phase": "P01",
        "iter": "P01-I01",
        "wave": _WAVE_ID,
        "hypothesis": "H01-01",
        "decision": "DEC-001",
        "audit": "AUD-001",
        "artifact": "ART-001",
        "memory": "MEM-001",
        "report": "REPORT-001",
        "event": "EVENT-001",
        "profile": "profile:engineering",
        "spec": _WAVE_ID,
    }
    for kind, target in targets.items():
        card = resolve_reference(state, kind, target)  # type: ignore[arg-type]
        assert card.kind == kind
        assert card.rows


def test_rank_goto_refs_prefers_explicit_link_wrap_match() -> None:
    hits = rank_goto_refs(_state(), _WAVE_ID)
    assert hits[0].kind == "wave"
    assert hits[0].target == _WAVE_ID


def test_rank_goto_refs_fuzzy_matches_state_title() -> None:
    hits = rank_goto_refs(_state(), "validate")
    assert any(hit.kind == "wave" and hit.target == _WAVE_ID for hit in hits)


def test_tooltip_for_text_uses_reference_preview() -> None:
    tooltip = tooltip_for_text(_state(), f"see {_WAVE_ID}")
    assert tooltip is not None
    assert f"wave {_WAVE_ID}" in tooltip


def test_detail_modal_rows_get_hover_tooltip() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_modal(
                DetailModal(
                    DetailCard(title="refs", rows=(("ref", f"see {_WAVE_ID}"),)),
                    state=app.state,
                )
            )
            await pilot.pause()
            row = app.screen.query_one(".detail-row", Static)
            assert row.tooltip is not None
            assert f"wave {_WAVE_ID}" in str(row.tooltip)

    asyncio.run(body())


def test_app_reference_nav_stack_back_and_forward() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_open_wave_ref(_WAVE_ID)
            await pilot.pause()
            assert isinstance(app.screen, ReferenceModal)
            assert app._current_reference is not None
            assert app._current_reference.target == _WAVE_ID

            app.action_open_phase_ref("P01")
            await pilot.pause()
            assert app._current_reference is not None
            assert app._current_reference.kind == "phase"
            assert [ref.target for ref in app._reference_back_stack] == [_WAVE_ID]

            app.action_reference_back()
            await pilot.pause()
            assert app._current_reference is not None
            assert app._current_reference.target == _WAVE_ID
            assert [ref.target for ref in app._reference_forward_stack] == ["P01"]

            await pilot.press("escape")
            await pilot.pause()
            app.action_reference_forward()
            await pilot.pause()
            assert app._current_reference is not None
            assert app._current_reference.kind == "phase"

    asyncio.run(body())


def test_goto_handler_opens_reference_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _handle_goto(app, _WAVE_ID)
            await pilot.pause()
            assert isinstance(app.screen, ReferenceModal)
            assert app.screen._card.title == f"wave {_WAVE_ID}"

    asyncio.run(body())
