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

from eawf.platform.lint.eawf022_propose_coverage import CoverageGapViolation
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.plan_preview import (
    DroppedClause,
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
            eu=1.5,
        ),
    ),
)

_EMPTY_PLAN = PlanTree(phase_id="P26", title="C06 surfaces", iters=())

_PLAN_WITH_DROPPED = PlanTree(
    phase_id="P26",
    title="C06 surfaces",
    iters=_PLAN.iters,
    dropped_detail=(
        DroppedClause(span_id="U-007", reason="brief span dropped with no covering criterion"),
        DroppedClause(span_id="U-009", reason="brief span dropped with no covering criterion"),
    ),
)


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
            for _ in range(EaApp.MAX_MODAL_DEPTH):
                app.push_modal(PlanPreviewModal(_PLAN))
                await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH
            open_plan_preview(app, _PLAN)
            await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())


def _state_with_buckets() -> object:
    """Return the fixture state with two waves tagged ``M`` (1.0 EU each)."""
    from eawf.kernel.state.enums import EffortBucket

    state = _load_state()
    # The fixture has one wave; clone it into a second so the iter sums two
    # M-bucket waves to 2.0 EU.
    first = state.waves["P01-I01-W01"].model_copy(  # type: ignore[attr-defined]
        update={"effort_bucket": EffortBucket.M}
    )
    second = first.model_copy(update={"id": "P01-I01-W02", "effort_bucket": EffortBucket.M})
    iteration = state.iters["P01-I01"].model_copy(  # type: ignore[attr-defined]
        update={"wave_ids": ["P01-I01-W01", "P01-I01-W02"]}
    )
    return state.model_copy(  # type: ignore[attr-defined]
        update={
            "waves": {"P01-I01-W01": first, "P01-I01-W02": second},
            "iters": {"P01-I01": iteration},
        }
    )


def test_build_plan_tree_sums_iter_eu_from_buckets() -> None:
    state = _state_with_buckets()
    tree = build_plan_tree(state, "P01")  # type: ignore[arg-type]
    assert len(tree.iters) == 1
    # Two M-bucket waves at 1.0 EU each roll up to 2.0 EU on the iter row.
    assert tree.iters[0].eu == 2.0
    assert tree.total_eu == 2.0


def test_build_plan_tree_unbucketed_waves_roll_up_to_zero() -> None:
    # The bare fixture wave carries no effort_bucket, so the iter EU is 0.
    state = _load_state()
    phase_id = next(iter(state.phases))  # type: ignore[attr-defined]
    tree = build_plan_tree(state, phase_id)  # type: ignore[arg-type]
    assert tree.total_eu == 0.0


def test_build_plan_tree_maps_dropped_detail_findings() -> None:
    findings = [
        CoverageGapViolation(
            lineno=1,
            col_offset=0,
            snippet="U-007",
            reason="brief span dropped with no covering criterion and no deferral",
        ),
        CoverageGapViolation(
            lineno=2,
            col_offset=0,
            snippet="U-013",
            reason="source-brief unit dropped with no covering criterion and no deferral",
        ),
    ]
    state = _load_state()
    phase_id = next(iter(state.phases))  # type: ignore[attr-defined]
    tree = build_plan_tree(state, phase_id, dropped_detail=findings)  # type: ignore[arg-type]
    assert [clause.span_id for clause in tree.dropped_detail] == ["U-007", "U-013"]
    assert tree.dropped_detail[0].reason.startswith("brief span dropped")


def test_build_plan_tree_no_dropped_detail_is_empty() -> None:
    state = _load_state()
    phase_id = next(iter(state.phases))  # type: ignore[attr-defined]
    tree = build_plan_tree(state, phase_id)  # type: ignore[arg-type]
    assert tree.dropped_detail == ()


def _rollup_text(app: EaApp) -> str:
    from textual.widgets import Static

    return str(app.screen.query_one("#plan-rollup", Static).render())


def test_plan_preview_renders_eu_rollup_row() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_plan(app, _PLAN, [])
            await pilot.pause()
            text = _rollup_text(app)
        # The rollup row names the per-iter EU and the phase total.
        assert "effort rollup" in text
        assert "P26-I01 1.5 EU" in text
        assert "total 1.5 EU" in text

    asyncio.run(body())


def test_plan_preview_renders_dropped_detail_when_present() -> None:
    async def body() -> None:
        from textual.css.query import NoMatches
        from textual.widgets import Static

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_plan(app, _PLAN_WITH_DROPPED, [])
            await pilot.pause()
            head = str(app.screen.query_one(".plan-dropped-head", Static).render())
            clauses = [
                str(node.render())
                for node in app.screen.query(".plan-dropped-clause").results(Static)
            ]
            try:
                app.screen.query_one("#plan-dropped")
                present = True
            except NoMatches:
                present = False
        assert present
        assert "dropped detail (2)" in head
        assert any("U-007" in clause for clause in clauses)
        assert any("U-009" in clause for clause in clauses)

    asyncio.run(body())


def test_plan_preview_omits_dropped_detail_when_absent() -> None:
    async def body() -> None:
        from textual.css.query import NoMatches

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_plan(app, _PLAN, [])
            await pilot.pause()
            try:
                app.screen.query_one("#plan-dropped")
                present = True
            except NoMatches:
                present = False
        # No findings -> the section is omitted, not rendered empty.
        assert present is False

    asyncio.run(body())
