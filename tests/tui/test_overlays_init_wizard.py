"""Pilot + unit tests for the TUI ``InitWizardModal``.

Covers the pure command-plan builders, the modal selection contract, the
``/init`` palette verb, the user-scope auto-open path, and the
workspace-link action.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal
from eawf.surfaces.tui.screens.overlays.init_wizard import (
    INIT_ACTION_WORKSPACE_LINK,
    InitWizardContext,
    InitWizardModal,
    InitWizardResult,
    build_init_wizard_options,
    quick_init_command,
    register_repo_command,
    workspace_link_command,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


def _push_init(
    app: EaApp,
    context: InitWizardContext,
    sink: list[InitWizardResult | None],
) -> InitWizardModal:
    modal = InitWizardModal(context)
    app.push_screen(modal, callback=lambda result: sink.append(result))
    return modal


def test_init_wizard_command_builders(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace_state = tmp_path / "workspace" / ".ea" / "state.json"
    assert quick_init_command(repo) == ("eawf", "init", "--quick", "--target", str(repo))
    assert register_repo_command(repo) == (
        "eawf",
        "repo",
        "add",
        str(repo),
        "--set-active",
        "--yes",
    )
    assert workspace_link_command(
        workspace_code="MAIN",
        repo_code="ABC",
        workspace_state_path=workspace_state,
        repo_path=repo,
    ) == (
        "eawf",
        "repo",
        "link-workspace",
        "MAIN",
        "ABC",
        "--workspace-state",
        str(workspace_state),
        "--target",
        str(repo),
    )


def test_init_wizard_options_include_workspace_link_when_context_has_both_sides(
    tmp_path: Path,
) -> None:
    context = InitWizardContext(
        scope="workspace",
        target_dir=tmp_path / "repo",
        workspace_code="MAIN",
        workspace_state_path=tmp_path / "workspace" / ".ea" / "state.json",
        repo_code="ABC",
        repo_path=tmp_path / "repo",
    )
    options = build_init_wizard_options(context)
    actions = [option.action for option in options]
    assert actions == ["quick-init", "register-repo", "workspace-link"]
    assert options[-1].command[0:3] == ("eawf", "repo", "link-workspace")


def test_init_wizard_defaults_to_quick_init() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_init(
                app,
                InitWizardContext(scope="repo", target_dir=Path("/abs/path/repo")),
                [],
            )
            await pilot.pause()
            assert modal.selected == 0

    asyncio.run(body())


def test_init_wizard_enter_returns_selected_action() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[InitWizardResult | None] = []
        context = InitWizardContext(
            scope="workspace",
            target_dir=Path("/abs/path/repo"),
            workspace_code="MAIN",
            workspace_state_path=Path("/abs/path/workspace/.ea/state.json"),
            repo_code="ABC",
            repo_path=Path("/abs/path/repo"),
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_init(app, context, sink)
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
        assert sink
        assert sink[0] is not None
        assert sink[0].action == INIT_ACTION_WORKSPACE_LINK

    asyncio.run(body())


def test_init_wizard_esc_returns_none() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[InitWizardResult | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_init(
                app,
                InitWizardContext(scope="repo", target_dir=Path("/abs/path/repo")),
                sink,
            )
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert sink == [None]

    asyncio.run(body())


def test_init_wizard_routes_through_push_modal_cap() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.screens.overlays.init_wizard import open_init_wizard

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(EaApp.MAX_MODAL_DEPTH):
                app.push_modal(ConfirmModal("continue?"))
                await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH
            assert open_init_wizard(app) is False
            await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())


def test_init_palette_verb_opens_wizard() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.press("i", "n", "i", "t")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, InitWizardModal)
            assert app.modal_depth() == 1

    asyncio.run(body())


def test_user_scope_auto_opens_init_wizard_when_registry_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    async def body() -> None:
        app = EaApp(scope="user", state_path=None)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert app.state is not None
            assert app.state.indexes.get("init_needed") is True
            assert isinstance(app.screen, InitWizardModal)

    asyncio.run(body())
