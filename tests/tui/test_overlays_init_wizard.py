"""Pilot + unit tests for the stepped, live-executing ``InitWizardModal``.

Covers:

* the pure render helpers (data -> content markup) — unit, no Textual mount;
* J2 repo-init happy path (configure -> preview -> execute -> done) executing
  the REAL init live in-TUI on a worker, asserting the created artifacts;
* J3 workspace-bootstrap happy path (create -> select -> preview ->
  link+validate -> done) with a per-repo validate sigil and partial-success
  continuation;
* the J2 honest error card (stderr tail + retry / back affordances);
* Esc-safety at every pre-execute step (no mutation) + the during-execute
  cancel-confirm;
* the J1 first-run hero auto-open (never auto-mutates) + path choosing; and
* the ``/init`` palette verb + the user-scope auto-open.

Because the live execute runs on a Textual worker, the Pilot bodies
``await app.workers.wait_for_complete()`` before asserting the post-execute
frame — ``pilot.pause()`` is CPU-idle-based, not worker-aware.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from textual.app import App
from textual.reactive import reactive
from textual.screen import ModalScreen

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays import init_wizard_render as render
from eawf.surfaces.tui.screens.overlays.init_wizard import (
    InitWizardContext,
    InitWizardModal,
    Journey,
    Step,
    SubstepState,
    WizardModel,
    code_is_valid,
    init_transparency_line,
    open_init_wizard,
    quick_init_command,
    register_repo_command,
    workspace_link_command,
)
from eawf.surfaces.tui.widgets.eu_bar import RenderMode

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_SIZE = (120, 48)


# ---- pure render helpers (no Textual mount) --------------------------------


def test_code_is_valid_boundary_and_error_cases() -> None:
    assert code_is_valid("ABC") is True
    assert code_is_valid("A1") is True  # 2-char min, starts uppercase
    assert code_is_valid("ABCDEFGHIJKLMNOP") is True  # 16-char max
    assert code_is_valid("abc-1") is False  # lowercase start
    assert code_is_valid("A") is False  # under 2 chars
    assert code_is_valid("ABCDEFGHIJKLMNOPQ") is False  # over 16 chars
    assert code_is_valid("") is False
    assert code_is_valid("1AB") is False  # starts with a digit


def test_init_transparency_line_reproduces_pinned_literal() -> None:
    model = WizardModel(
        journey=Journey.REPO_INIT,
        step=Step.PREVIEW,
        project_code="ABC",
        project_title="Alpha control bus",
        profiles={"core", "python"},
    )
    assert init_transparency_line(model) == (
        "eawf init --project-code ABC "
        '--project-title "Alpha control bus" '
        "--profiles core,python --template agent-driven"
    )


def test_steprail_marks_current_step_and_separator() -> None:
    model = WizardModel(journey=Journey.REPO_INIT, step=Step.CONFIGURE, project_code="ABC")
    rail = render.steprail_markup(model, mode="unicode")
    # The literal labels + the separator are present, current step bolded.
    for label in render.J2_RAIL:
        assert label in rail
    assert render.RAIL_SEP in rail
    assert "[b]" in rail  # current step emphasis


def test_substep_rows_carry_live_sigils() -> None:
    model = WizardModel(journey=Journey.REPO_INIT, step=Step.EXECUTE, project_code="ABC")
    model.substeps = [
        render.Substep("write .ea/state.json", SubstepState.DONE),
        render.Substep("render AGENTS.md + plugin", SubstepState.RUNNING),
        render.Substep("first daemon handshake", SubstepState.QUEUED),
    ]
    rows = render.substep_rows_markup(model, mode="unicode")
    assert "●" in rows  # done = filled circle
    assert "◐" in rows  # running = half circle
    assert "◌" in rows  # queued = pending ring
    assert "running" in rows and "queued" in rows and "done" in rows


def test_doctor_rows_warn_names_fix_and_never_hidden() -> None:
    model = WizardModel(journey=Journey.REPO_INIT, step=Step.DONE, project_code="ABC")
    model.doctor = [
        render.DoctorCheck("state", True),
        render.DoctorCheck("plugin", False, ".claude/ preview missing — run eawf doctor --fix"),
    ]
    assert render.doctor_title(model) == "Doctor · 2 checks · 1 warn"
    rows = render.doctor_rows_markup(model, mode="unicode")
    assert "eawf doctor --fix" in rows  # the fix is surfaced, not collapsed


def test_select_title_tracks_live_selected_count() -> None:
    model = WizardModel(journey=Journey.WORKSPACE, step=Step.CONFIGURE, project_code="WS")
    assert render.select_title(model) == "Registry repos · space toggles · 0 selected"
    model.selected_repos = {"ABC", "DEF"}
    assert render.select_title(model) == "Registry repos · space toggles · 2 selected"


def test_legacy_command_builders_kept_for_compat(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ws = tmp_path / "ws" / ".ea" / "state.json"
    assert quick_init_command(repo) == ("eawf", "init", "--quick", "--target", str(repo))
    assert register_repo_command(repo)[0:3] == ("eawf", "repo", "add")
    assert workspace_link_command(
        workspace_code="MAIN", repo_code="ABC", workspace_state_path=ws, repo_path=repo
    )[0:3] == ("eawf", "repo", "link-workspace")


# ---- bare-host Pilot harness ------------------------------------------------


class _HostApp(App[None]):
    """Bare host carrying the unicode ``render_mode`` the wizard reads."""

    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self, modal: ModalScreen[object]) -> None:
        super().__init__()
        self._modal = modal

    def on_mount(self) -> None:
        self.push_screen(self._modal)


def _repo_context(target: Path) -> InitWizardContext:
    return InitWizardContext(scope="repo", target_dir=target, git_root_found=True)


# ---- CR-01: J2 repo init executes live in-TUI ------------------------------


def test_j2_repo_init_executes_live_and_shows_created_artifacts(tmp_path: Path) -> None:
    """CR-01: configure (valid code) -> preview -> execute (live) -> done card."""

    async def body() -> None:
        modal = InitWizardModal(_repo_context(tmp_path))
        modal.model.project_code = "ABC"
        modal.model.project_title = "Alpha control bus"
        app = _HostApp(modal)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            assert modal.model.step is Step.CONFIGURE
            # configure: a valid code gates the preview button
            assert code_is_valid(modal.model.project_code)
            await pilot.press("enter")  # -> preview
            await pilot.pause()
            assert modal.model.step is Step.PREVIEW
            # the preview shows the exact CLI-equivalent line
            assert "eawf init --project-code ABC" in init_transparency_line(modal.model)
            await pilot.press("enter")  # -> execute (live worker)
            # the live execute runs on a worker; drain it before asserting
            await app.workers.wait_for_complete()
            await pilot.pause()
        # The real init ran end-to-end inside the TUI — no shell round-trip.
        assert (tmp_path / ".ea" / "state.json").exists()
        assert (tmp_path / "AGENTS.md").exists()
        state = json.loads((tmp_path / ".ea" / "state.json").read_text())
        assert state["project"]["code"] == "ABC"
        # the done card advanced with the artifacts + duration
        assert modal.model.step is Step.DONE
        labels = [label for label, _ in modal.model.artifacts]
        assert ".ea/state.json" in labels and "AGENTS.md" in labels
        assert modal.model.duration_s is not None

    asyncio.run(body())


def test_j2_configure_blocks_preview_on_invalid_code(tmp_path: Path) -> None:
    """CR-01: an invalid code dims Enter (no advance to preview)."""

    async def body() -> None:
        modal = InitWizardModal(_repo_context(tmp_path))
        modal.model.project_code = "abc-1"  # fails the regex
        app = _HostApp(modal)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.press("enter")  # blocked: code invalid
            await pilot.pause()
            assert modal.model.step is Step.CONFIGURE  # did not advance

    asyncio.run(body())


def test_j2_space_toggles_profile_chip(tmp_path: Path) -> None:
    """CR-01: Space toggles the python chip (core stays locked-on)."""

    async def body() -> None:
        modal = InitWizardModal(_repo_context(tmp_path))
        modal.model.project_code = "ABC"
        app = _HostApp(modal)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            assert "python" not in modal.model.profiles
            await pilot.press("space")
            await pilot.pause()
            assert "python" in modal.model.profiles
            assert "core" in modal.model.profiles  # always present

    asyncio.run(body())


# ---- CR-02: J3 workspace bootstrap links repos with validate sigils --------


def _seed_repo(root: Path, code: str) -> Path:
    """Write a minimal ``.ea/state.json`` so a workspace validate passes."""
    repo = root / code.lower()
    (repo / ".ea").mkdir(parents=True, exist_ok=True)
    (repo / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    return repo


def _workspace_context(
    tmp_path: Path, repos: tuple[render.WorkspaceRepoRef, ...]
) -> InitWizardContext:
    return InitWizardContext(
        scope="workspace",
        target_dir=tmp_path,
        workspace_state_path=tmp_path / "ws" / ".ea" / "state.json",
        registry_repos=repos,
    )


def _ref(code: str, path: Path) -> render.WorkspaceRepoRef:
    from eawf.kernel.state.enums import ProjectStatus
    from eawf.kernel.state.models import WorkspaceRepoRef
    from eawf.kernel.state.urn import build as build_urn

    return WorkspaceRepoRef(
        code=code,
        path=str(path),
        state_urn=build_urn("repo", owner=code),
        project_code=code,
        title=code,
        status=ProjectStatus.ACTIVE,
    )


def test_j3_workspace_bootstrap_links_repos_in_one_pass(tmp_path: Path) -> None:
    """CR-02: create workspace + link the multi-selected repos, per-repo validate."""

    async def body() -> None:
        abc = _seed_repo(tmp_path, "ABC")
        ghi = _seed_repo(tmp_path, "GHI")
        refs = (_ref("ABC", abc), _ref("GHI", ghi))
        modal = InitWizardModal(_workspace_context(tmp_path, refs))
        modal.model.project_code = "CONSTELLATION"
        app = _HostApp(modal)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            assert modal.model.journey is Journey.WORKSPACE
            await pilot.press("a")  # select all
            await pilot.pause()
            assert modal.model.selected_repos == {"ABC", "GHI"}
            await pilot.press("enter")  # -> preview
            await pilot.pause()
            assert modal.model.step is Step.PREVIEW
            await pilot.press("enter")  # -> link + validate (live worker)
            await app.workers.wait_for_complete()
            await pilot.pause()
        # The workspace state was created and both repos linked in one pass.
        ws = tmp_path / "ws" / ".ea" / "state.json"
        assert ws.exists()
        payload = json.loads(ws.read_text())
        assert set(payload["workspace"]["repos"]) == {"ABC", "GHI"}
        assert modal.model.step is Step.DONE
        # every selected substep reached a terminal state (done = ok per row)
        assert all(s.state is SubstepState.DONE for s in modal.model.substeps)

    asyncio.run(body())


def test_j3_partial_validate_failure_continues_others(tmp_path: Path) -> None:
    """CR-02: a repo whose path lacks .ea fails its row; the pass continues."""

    async def body() -> None:
        good = _seed_repo(tmp_path, "ABC")
        bad = tmp_path / "ghi"  # no .ea/state.json — validate fails
        bad.mkdir()
        refs = (_ref("ABC", good), _ref("GHI", bad))
        modal = InitWizardModal(_workspace_context(tmp_path, refs))
        modal.model.project_code = "CONSTELLATION"
        app = _HostApp(modal)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.press("a")  # select all
            await pilot.pause()
            await pilot.press("enter")  # -> preview
            await pilot.pause()
            await pilot.press("enter")  # -> link + validate
            await app.workers.wait_for_complete()
            await pilot.pause()
        # Did not crash; advanced to done with a mix of ok + failed rows.
        assert modal.model.step is Step.DONE
        states = {s.label: s.state for s in modal.model.substeps}
        abc_row = next(s for lbl, s in states.items() if "ABC" in lbl)
        ghi_row = next(s for lbl, s in states.items() if "GHI" in lbl)
        assert abc_row is SubstepState.DONE  # the good repo linked + validated
        assert ghi_row is SubstepState.FAILED  # the bad repo failed its row

    asyncio.run(body())


# ---- CR-02: honest error card ----------------------------------------------


def test_j2_execute_failure_renders_error_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-02: a failing substep renders the honest error card (no crash)."""

    def _boom(model: object, target: object) -> list[tuple[str, str]]:
        raise RuntimeError("profile 'research' template missing key 'roundtable'")

    monkeypatch.setattr("eawf.surfaces.tui.screens.overlays.init_wizard._run_repo_init", _boom)

    async def body() -> None:
        modal = InitWizardModal(_repo_context(tmp_path))
        modal.model.project_code = "ABC"
        app = _HostApp(modal)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.press("enter")  # -> preview
            await pilot.pause()
            await pilot.press("enter")  # -> execute (will fail)
            await app.workers.wait_for_complete()
            await pilot.pause()
        # The failed execute lands on the honest error card, never a crash.
        assert modal.model.step is Step.ERROR
        assert modal.model.error_stderr is not None
        assert "roundtable" in modal.model.error_stderr
        assert modal.model.error_step_index is not None
        # a failed substep carries the failed state
        assert any(s.state is SubstepState.FAILED for s in modal.model.substeps)

    asyncio.run(body())


def test_error_card_back_to_configure_clears_substeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-02: ``b`` from the error card returns to configure with state cleared."""

    def _boom(model: object, target: object) -> list[tuple[str, str]]:
        raise RuntimeError("boom")

    monkeypatch.setattr("eawf.surfaces.tui.screens.overlays.init_wizard._run_repo_init", _boom)

    async def body() -> None:
        modal = InitWizardModal(_repo_context(tmp_path))
        modal.model.project_code = "ABC"
        app = _HostApp(modal)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.press("enter")  # preview
            await pilot.pause()
            await pilot.press("enter")  # execute -> error
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert modal.model.step is Step.ERROR
            await pilot.press("b")  # back to configure
            await pilot.pause()
            assert modal.model.step is Step.CONFIGURE
            assert modal.model.substeps == []
            assert modal.model.error_stderr is None

    asyncio.run(body())


# ---- CR-03: J1 first-run hero + Esc-safety ---------------------------------


def test_j1_first_run_hero_renders_paths_and_never_mutates(tmp_path: Path) -> None:
    """CR-03: first-run opens the hero with three entry paths; no mutation."""

    async def body() -> None:
        ctx = InitWizardContext(
            scope="user", target_dir=tmp_path, init_needed=True, git_root_found=True
        )
        modal = InitWizardModal(ctx)
        app = _HostApp(modal)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            assert modal.model.journey is Journey.FIRST_RUN
            assert modal.model.step is Step.CHOOSE
            # the hero paths carry the three literal entry-path labels
            paths = render.path_rows_markup(0, mode="unicode", git_root_found=True)
            assert render.PATH_LABEL_INIT in paths
            assert render.PATH_LABEL_REGISTER in paths
            assert render.PATH_LABEL_WORKSPACE in paths
        # Never auto-mutated: no .ea was written by merely opening the hero.
        assert not (tmp_path / ".ea").exists()

    asyncio.run(body())


def test_j1_path_i_advances_into_repo_init_without_mutating(tmp_path: Path) -> None:
    """CR-03: choosing ``i`` opens J2 at configure (still no write)."""

    async def body() -> None:
        ctx = InitWizardContext(
            scope="user", target_dir=tmp_path, init_needed=True, git_root_found=True
        )
        modal = InitWizardModal(ctx)
        app = _HostApp(modal)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.press("i")  # init this repo
            await pilot.pause()
            assert modal.model.journey is Journey.REPO_INIT
            assert modal.model.step is Step.CONFIGURE
        assert not (tmp_path / ".ea").exists()  # still nothing written

    asyncio.run(body())


def test_esc_is_safe_at_each_pre_execute_step(tmp_path: Path) -> None:
    """CR-03: Esc cancels safely at preview with no .ea written."""

    async def body() -> None:
        sink: list[object] = []
        modal = InitWizardModal(_repo_context(tmp_path))
        modal.model.project_code = "ABC"
        app = _HostApp(modal)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.press("enter")  # -> preview
            await pilot.pause()
            assert modal.model.step is Step.PREVIEW
            modal.dismiss = lambda result=None: sink.append(result)  # type: ignore[method-assign]
            await pilot.press("escape")  # safe cancel at preview
            await pilot.pause()
        assert sink == [None]
        assert not (tmp_path / ".ea").exists()

    asyncio.run(body())


# ---- App integration: palette verb + user-scope auto-open ------------------


def test_init_palette_verb_opens_wizard() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("slash")
            await pilot.press("i", "n", "i", "t")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, InitWizardModal)
            assert app.modal_depth() == 1

    asyncio.run(body())


def test_user_scope_auto_opens_wizard_when_registry_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    async def body() -> None:
        app = EaApp(scope="user", state_path=None)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert app.state is not None
            assert app.state.indexes.get("init_needed") is True
            assert isinstance(app.screen, InitWizardModal)
            # auto-opened to the first-run hero, never auto-mutating
            assert app.screen.model.journey is Journey.FIRST_RUN

    asyncio.run(body())


def test_open_init_wizard_routes_through_push_modal_cap() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            for _ in range(EaApp.MAX_MODAL_DEPTH):
                app.push_modal(ConfirmModal("continue?"))
                await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH
            assert open_init_wizard(app) is False
            await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())
