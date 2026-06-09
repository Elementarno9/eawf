"""Pilot + unit tests for the C06 ``AuditRunningModal`` overlay (P26-W20).

Covers the pure :class:`AuditProgress` snapshot (done/total tally,
``with_check`` immutability + unknown-check no-op) and the overlay's
read-only progress contract: the title tally render, the live
``update_progress`` repaint seam, the per-check glyph rows, and ``Esc``
minimise.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Static

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.audit_running import (
    AuditProgress,
    AuditRunningModal,
    CheckRow,
    CheckState,
    open_audit_running,
)
from eawf.surfaces.tui.widgets import sigils
from eawf.surfaces.tui.widgets.sigils import Sigil

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

_PROGRESS = AuditProgress(
    audit_id="A19-P14",
    scope_label="P14",
    checks=(
        CheckRow("file_exists", CheckState.PASS),
        CheckRow("pytest_pass", CheckState.RUNNING),
        CheckRow("coverage_min", CheckState.RUNNING),
    ),
)


def test_audit_progress_tally() -> None:
    assert _PROGRESS.done() == 1
    assert _PROGRESS.total() == 3


def test_audit_progress_with_check_updates_immutably() -> None:
    updated = _PROGRESS.with_check("pytest_pass", CheckState.PASS)
    assert updated is not _PROGRESS
    assert updated.done() == 2
    # Original snapshot is unchanged.
    assert _PROGRESS.done() == 1


def test_audit_progress_with_check_fail() -> None:
    updated = _PROGRESS.with_check("pytest_pass", CheckState.FAIL)
    assert updated.checks[1].state is CheckState.FAIL
    # A fail still counts toward "done" (it reported).
    assert updated.done() == 2


def test_audit_progress_unknown_check_is_noop() -> None:
    assert _PROGRESS.with_check("not_a_check", CheckState.PASS) is _PROGRESS


def test_audit_progress_empty_checks() -> None:
    empty = AuditProgress(audit_id="A1", scope_label="s", checks=())
    assert empty.done() == 0
    assert empty.total() == 0


def test_audit_running_renders_title_tally() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = AuditRunningModal(_PROGRESS)
            app.push_screen(modal)
            await pilot.pause()
            title = modal.query_one("#audit-running-title", Static)
            text = str(title.render())
            assert "A19-P14" in text
            assert "[1/3]" in text

    asyncio.run(body())


def test_audit_running_update_progress_repaints() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = AuditRunningModal(_PROGRESS)
            app.push_screen(modal)
            await pilot.pause()
            modal.update_progress(_PROGRESS.with_check("pytest_pass", CheckState.PASS))
            await pilot.pause()
            title = modal.query_one("#audit-running-title", Static)
            assert "[2/3]" in str(title.render())

    asyncio.run(body())


def test_audit_running_rows_render_glyphs() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = AuditRunningModal(_PROGRESS)
            app.push_screen(modal)
            await pilot.pause()
            rows = modal.query_one("#audit-running-rows-inner", Static)
            text = str(rows.render())
            # The per-check marks now draw the shared lifecycle sigils: a pass
            # folds onto the closed sigil, a running check onto the running
            # sigil (migrated off the old dot / check / cross).
            assert sigils.glyph(Sigil.CLOSED, mode="unicode") in text  # the passed check
            assert sigils.glyph(Sigil.RUNNING, mode="unicode") in text  # a still-running check
            assert "file_exists" in text

    asyncio.run(body())


def test_audit_running_renders_block_progress_bar() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = AuditRunningModal(_PROGRESS)
            app.push_screen(modal)
            await pilot.pause()
            bar = modal.query_one("#audit-running-bar", Static)
            text = str(bar.render())
            # One of three checks reported, so the block-progress bar shows a
            # partial fill + a 1/3 counter in the unicode render mode.
            assert "█" in text
            assert "▒" in text
            assert "1/3" in text

    asyncio.run(body())


def test_audit_running_esc_minimises() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_modal(AuditRunningModal(_PROGRESS))
            await pilot.pause()
            assert app.modal_depth() == 1
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_open_audit_running_respects_cap() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(EaApp.MAX_MODAL_DEPTH):
                app.push_modal(AuditRunningModal(_PROGRESS))
                await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH
            open_audit_running(app, _PROGRESS)
            await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())
