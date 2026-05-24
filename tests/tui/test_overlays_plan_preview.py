"""Pilot + unit tests for the C06 ``PlanPreviewModal`` overlay (P26-W20).

Covers the pure plan-tree builder (:func:`build_plan_tree`) against the
state fixtures and the overlay's three-option AUQ contract: the safe
``approve`` default, ``←`` / ``→`` action movement, ``Enter`` returning
the highlighted action via the dismiss value, ``Esc`` returning
``reject``, and the F14 guard that disables ``approve`` on a no-wave plan.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.plan_preview import (
    PlanIterRow,
    PlanPreviewModal,
    PlanTree,
    PlanWaveRow,
    build_plan_tree,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

_PLAN = PlanTree(
    phase_id="P26",
    title="C06 surfaces",
    iters=(
        PlanIterRow(
            iter_id="P26-I01",
            title="iter one",
            waves=(
                PlanWaveRow(wave_id="P26-I01-W01", title="first", deps=()),
                PlanWaveRow(wave_id="P26-I01-W02", title="second", deps=("P26-I01-W01",)),
            ),
        ),
    ),
)

_EMPTY_PLAN = PlanTree(phase_id="P26", title="C06 surfaces", iters=())


def _push_plan(app: EaApp, plan: PlanTree, sink: list[str]) -> PlanPreviewModal:
    modal = PlanPreviewModal(plan)
    app.push_screen(modal, callback=lambda result: sink.append(result))
    return modal


def _load_state() -> object:
    from eawf.kernel.state.models import State

    raw = json.loads(_PHASE_ITER_WAVE.read_text())
    return State.model_validate(raw)


def test_build_plan_tree_none_state_is_childless() -> None:
    tree = build_plan_tree(None, "P26")
    assert tree.phase_id == "P26"
    assert tree.iters == ()


def test_build_plan_tree_unknown_phase_is_childless() -> None:
    state = _load_state()
    tree = build_plan_tree(state, "P99")  # type: ignore[arg-type]
    assert tree.phase_id == "P99"
    assert tree.iters == ()


def test_build_plan_tree_resolves_phase_iter_wave() -> None:
    state = _load_state()
    # Resolve the first phase present in the fixture by id.
    phase_id = next(iter(state.phases))  # type: ignore[attr-defined]
    tree = build_plan_tree(state, phase_id)  # type: ignore[arg-type]
    assert tree.phase_id == phase_id
    # The fixture is a phase + iter + wave, so at least one iter resolves.
    assert len(tree.iters) >= 1
    assert any(it.waves for it in tree.iters)


def test_plan_preview_defaults_to_approve() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_plan(app, _PLAN, [])
            await pilot.pause()
            assert modal.selected == 0  # index 0 == "approve"

    asyncio.run(body())


def test_plan_preview_enter_returns_approve() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_plan(app, _PLAN, sink)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert sink == ["approve"]

    asyncio.run(body())


def test_plan_preview_right_then_enter_returns_edit() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_plan(app, _PLAN, sink)
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
        assert sink == ["edit"]

    asyncio.run(body())


def test_plan_preview_esc_returns_reject() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_plan(app, _PLAN, sink)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert sink == ["reject"]

    asyncio.run(body())


def test_plan_preview_empty_plan_skips_approve() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_plan(app, _EMPTY_PLAN, [])
            await pilot.pause()
            # F14: a no-wave plan defaults to ``edit`` (approve disabled).
            assert modal.selected == 1  # index 1 == "edit"

    asyncio.run(body())


def test_plan_preview_empty_plan_enter_does_not_approve() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_plan(app, _EMPTY_PLAN, sink)
            await pilot.pause()
            # Force the highlight onto approve, then Enter is suppressed.
            modal.selected = 0
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert sink == []

    asyncio.run(body())


def test_plan_preview_routes_through_push_modal_cap() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.screens.overlays.plan_preview import open_plan_preview

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Fill the stack to the cap, then the helper's push is rejected.
            for _ in range(3):
                app.push_modal(PlanPreviewModal(_PLAN))
                await pilot.pause()
            assert app.modal_depth() == 3
            open_plan_preview(app, _PLAN)
            await pilot.pause()
            assert app.modal_depth() == 3

    asyncio.run(body())
