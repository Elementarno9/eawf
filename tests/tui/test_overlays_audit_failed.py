"""Pilot + unit tests for the C06 ``AuditFailedModal`` overlay (P26-W20).

This is the wave's tagged success criterion (D17 mutating menu + D27
status line). Covers the pure D27 line formatter
(:func:`format_dispatch_line`), the five-action menu (retry / split /
land-partial / abandon / scope-change) with ``↑`` / ``↓`` movement, the
``Enter`` dispatch returning the chosen action via the dismiss value, the
``Esc`` close, and the D27 status-line render seam
(:meth:`AuditFailedModal.mark_dispatching` / ``mark_closed``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Static

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.audit_failed import (
    AuditFailedModal,
    format_dispatch_line,
    open_audit_failed,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

_WAVE = "P26-I01-W20"

#: The five D17 actions in menu order (mirrors the overlay's _ACTIONS).
_EXPECTED_ACTIONS = ("retry", "split", "land-partial", "abandon", "scope-change")


def _push_audit_failed(app: EaApp, sink: list[str]) -> AuditFailedModal:
    modal = AuditFailedModal(_WAVE)
    app.push_screen(modal, callback=lambda result: sink.append(result))
    return modal


def test_format_dispatch_line_d27_wording() -> None:
    # D27: ``dispatching <action> → <runtime> · attempt <n>``.
    assert format_dispatch_line("retry", "claude-code", 2) == (
        "dispatching retry → claude-code · attempt 2"
    )


def test_format_dispatch_line_each_action() -> None:
    for action in _EXPECTED_ACTIONS:
        line = format_dispatch_line(action, "codex", 1)
        assert line == f"dispatching {action} → codex · attempt 1"


def test_audit_failed_defaults_to_retry() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_audit_failed(app, [])
            await pilot.pause()
            assert modal.selected == 0  # index 0 == "retry"

    asyncio.run(body())


def test_audit_failed_renders_all_five_actions() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_audit_failed(app, [])
            await pilot.pause()
            for index, action in enumerate(_EXPECTED_ACTIONS):
                cell = modal.query_one(f"#action-{index}", Static)
                assert action in str(cell.render())

    asyncio.run(body())


def test_audit_failed_enter_returns_retry() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_audit_failed(app, sink)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert sink == ["retry"]

    asyncio.run(body())


def test_audit_failed_down_to_abandon_returns_abandon() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_audit_failed(app, sink)
            await pilot.pause()
            # retry(0) -> split(1) -> land-partial(2) -> abandon(3)
            for _ in range(3):
                await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
        assert sink == ["abandon"]

    asyncio.run(body())


def test_audit_failed_esc_returns_close() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_audit_failed(app, sink)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert sink == ["close"]

    asyncio.run(body())


def test_audit_failed_up_wraps_to_scope_change() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_audit_failed(app, [])
            await pilot.pause()
            await pilot.press("up")  # wraps 0 -> last (index 4 == scope-change)
            await pilot.pause()
            assert modal.selected == 4

    asyncio.run(body())


def test_audit_failed_mark_dispatching_renders_d27_line() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = AuditFailedModal(_WAVE, runtime="claude-code")
            app.push_screen(modal)
            await pilot.pause()
            modal.mark_dispatching("retry", 3)
            await pilot.pause()
            status = modal.query_one("#audit-failed-status", Static)
            assert "dispatching retry → claude-code · attempt 3" in str(status.render())

    asyncio.run(body())


def test_audit_failed_mark_closed_renders_terminal_line() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = AuditFailedModal(_WAVE)
            app.push_screen(modal)
            await pilot.pause()
            modal.mark_dispatching("split", 1)
            await pilot.pause()
            modal.mark_closed()
            await pilot.pause()
            status = modal.query_one("#audit-failed-status", Static)
            assert str(status.render()).strip() == "closed"

    asyncio.run(body())


def test_open_audit_failed_respects_cap() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(3):
                app.push_modal(AuditFailedModal(_WAVE))
                await pilot.pause()
            assert app.modal_depth() == 3
            open_audit_failed(app, _WAVE)
            await pilot.pause()
            assert app.modal_depth() == 3

    asyncio.run(body())
