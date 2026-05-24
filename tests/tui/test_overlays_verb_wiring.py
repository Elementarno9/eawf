"""Pilot tests for the C06 W20 overlay palette-verb wiring (P26-W20).

Proves the two W20 palette verbs open their overlays through the App's
modal-cap-aware ``push_modal``: ``/audit`` opens the live
:class:`AuditRunningModal`, and ``/roadmap propose <P##>`` opens the
:class:`PlanPreviewModal` built from the bound state. The needs_user and
audit-failed overlays are daemon-push only (no palette verb, per C06
§5.6); their cap-checked ``open_*`` helpers are covered in their own test
modules.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.palette.verbs import _handle_audit, _handle_roadmap
from eawf.surfaces.tui.screens.overlays.audit_running import AuditRunningModal
from eawf.surfaces.tui.screens.overlays.plan_preview import PlanPreviewModal

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


def test_audit_verb_opens_audit_running_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # /audit takes no id — it audits the current scope.
            _handle_audit(app, "")
            await pilot.pause()
            assert isinstance(app.screen, AuditRunningModal)

    asyncio.run(body())


def test_audit_verb_with_arg_is_rejected_no_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # A trailing free-text token is rejected (no URN-lookup
            # contract); the overlay must not open.
            _handle_audit(app, "urn:eawf:v1:repo:eawf")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_audit_verb_titles_with_scope_derived_id() -> None:
    async def body() -> None:
        from textual.widgets import Static

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _handle_audit(app, "")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, AuditRunningModal)
            title = modal.query_one("#audit-running-title", Static)
            text = str(title.render())
            # The overlay surfaces the scope-derived audit id (from state),
            # not any operator-typed string, plus the resolved scope name.
            from eawf.surfaces.tui.palette.verbs import _active_audit_id

            assert _active_audit_id(app) in text
            assert "repo" in text

    asyncio.run(body())


def test_roadmap_propose_opens_plan_preview_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            phase_id = next(iter(app.state.phases))  # type: ignore[union-attr]
            _handle_roadmap(app, f"propose {phase_id}")
            await pilot.pause()
            assert isinstance(app.screen, PlanPreviewModal)

    asyncio.run(body())


def test_roadmap_non_propose_subverb_does_not_open_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _handle_roadmap(app, "revise P26")
            await pilot.pause()
            # ``revise`` is unwired this wave — no modal pushed.
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_roadmap_propose_without_phase_does_not_open_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _handle_roadmap(app, "propose")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_audit_verb_routes_through_push_modal_cap() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.screens.overlays.audit_running import AuditProgress

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            empty = AuditProgress(audit_id="a", scope_label="s", checks=())
            for _ in range(3):
                app.push_modal(AuditRunningModal(empty))
                await pilot.pause()
            assert app.modal_depth() == 3
            _handle_audit(app, "")
            await pilot.pause()
            # Cap holds — the verb's push is rejected, not stacked.
            assert app.modal_depth() == 3

    asyncio.run(body())
