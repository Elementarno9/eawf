"""Golden snapshots for the stepped init wizard (P30-I16-W09, INIT-2).

Pins the four-journey wizard chassis against the ratified W08 pin-strip
(``handoff/2026-06-11-init-entry/pinned-literals.md``): the J1 first-run hero,
the J2 repo-init configure / preview / execute(streaming) / error states, the
J3 workspace repo-select, and the J4 done card — plus the 80-column
narrow-variant golden every journey ships per CR-03.

Each frame is captured from the :class:`InitWizardModal` mounted IN ISOLATION
on a bare themed host (mirroring ``test_overlays_reskin_w21.py``) so the frame
is a pure function of the constructed :class:`WizardModel` with no off-disk
daemon read and no live worker. The seal kill switch is set so the hero uses
the glyph fallback (deterministic, no terminal-dependent raster image).

Regenerate after an intentional layout change with::

    EAWF_DAEMONLESS=1 EAWF_SEAL_DISABLE=1 EAWF_SNAPSHOT_REGEN=1 \\
        uv run pytest tests/tui/test_init_wizard_snapshots.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from textual.app import App
from textual.reactive import reactive
from textual.screen import ModalScreen

from eawf.surfaces.tui.screens.overlays import init_wizard_render as render
from eawf.surfaces.tui.screens.overlays.init_wizard import (
    InitWizardContext,
    InitWizardModal,
    Step,
    SubstepState,
)
from eawf.surfaces.tui.snapshot import assert_screen_snapshot, settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.surfaces.tui.widgets.seal import SEAL_DISABLE_ENV, seal_capable

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_GOLDEN = Path(__file__).resolve().parents[2] / "tests" / "snapshots" / "tui" / "golden"

_WIDE = (120, 44)
_NARROW = (80, 40)

assert _THEME.is_file(), f"missing theme: {_THEME}"


@pytest.fixture(autouse=True)
def _force_seal_glyph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the glyph-fallback seal path so goldens never embed a raster."""
    monkeypatch.setenv(SEAL_DISABLE_ENV, "1")
    seal_capable.cache_clear()


class _HostApp(App[None]):
    """Bare themed host carrying the unicode ``render_mode`` the wizard reads."""

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self, modal: ModalScreen[object]) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self._modal = modal

    def on_mount(self) -> None:
        self.push_screen(self._modal)


def _first_run() -> InitWizardModal:
    ctx = InitWizardContext(
        scope="user", target_dir=Path("/x"), init_needed=True, git_root_found=True
    )
    return InitWizardModal(ctx)


def _repo_configure(*, code: str = "ABC", profiles: set[str] | None = None) -> InitWizardModal:
    ctx = InitWizardContext(scope="repo", target_dir=Path("/x"), git_root_found=True)
    modal = InitWizardModal(ctx)
    modal.model.project_code = code
    modal.model.project_title = "Alpha control bus"
    modal.model.profiles = profiles if profiles is not None else {"core", "python"}
    return modal


def _repo_step(step: Step) -> InitWizardModal:
    modal = _repo_configure()
    modal.model.step = step
    return modal


def _ref(code: str, path: str) -> render.WorkspaceRepoRef:
    from eawf.kernel.state.enums import ProjectStatus
    from eawf.kernel.state.models import WorkspaceRepoRef
    from eawf.kernel.state.urn import build as build_urn

    return WorkspaceRepoRef(
        code=code,
        path=path,
        state_urn=build_urn("repo", owner=code),
        project_code=code,
        title=code,
        status=ProjectStatus.ACTIVE,
    )


def _workspace_select() -> InitWizardModal:
    refs = (
        _ref("ABC", "/code/abc"),
        _ref("DEF", "/work/def"),
        _ref("GHI", "/lab/ghi"),
    )
    modal = InitWizardModal(
        InitWizardContext(scope="workspace", target_dir=Path("/x"), registry_repos=refs)
    )
    modal.model.project_code = "CONSTELLATION"
    modal.model.selected_repos = {"ABC", "DEF"}
    return modal


def _capture(modal: InitWizardModal, golden: str, *, size: tuple[int, int]) -> None:
    async def body() -> None:
        app = _HostApp(modal)
        async with app.run_test(size=size) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / golden)

    asyncio.run(body())


# ---- J1 first-run hero ------------------------------------------------------


def test_j1_first_run_hero_snapshot() -> None:
    _capture(_first_run(), "init_wizard_j1_hero.txt", size=_WIDE)


def test_j1_first_run_hero_narrow_snapshot() -> None:
    _capture(_first_run(), "init_wizard_j1_hero_narrow.txt", size=_NARROW)


# ---- J2 repo init -----------------------------------------------------------


def test_j2_configure_valid_snapshot() -> None:
    _capture(_repo_configure(), "init_wizard_j2_configure.txt", size=_WIDE)


def test_j2_configure_invalid_snapshot() -> None:
    _capture(
        _repo_configure(code="abc-1"),
        "init_wizard_j2_configure_invalid.txt",
        size=_WIDE,
    )


def test_j2_preview_snapshot() -> None:
    _capture(_repo_step(Step.PREVIEW), "init_wizard_j2_preview.txt", size=_WIDE)


def test_j2_execute_streaming_snapshot() -> None:
    modal = _repo_step(Step.EXECUTE)
    modal.model.substeps = [
        render.Substep(render.J2_SUBSTEPS[0], SubstepState.DONE),
        render.Substep(render.J2_SUBSTEPS[1], SubstepState.DONE),
        render.Substep(render.J2_SUBSTEPS[2], SubstepState.RUNNING),
        render.Substep(render.J2_SUBSTEPS[3], SubstepState.QUEUED),
        render.Substep(render.J2_SUBSTEPS[4], SubstepState.QUEUED),
    ]
    _capture(modal, "init_wizard_j2_execute.txt", size=_WIDE)


def test_j2_error_card_snapshot() -> None:
    modal = _repo_step(Step.ERROR)
    modal.model.substeps = [
        render.Substep(render.J2_SUBSTEPS[0], SubstepState.DONE),
        render.Substep(render.J2_SUBSTEPS[1], SubstepState.DONE),
        render.Substep(render.J2_SUBSTEPS[2], SubstepState.FAILED),
        render.Substep(render.J2_SUBSTEPS[3], SubstepState.QUEUED),
        render.Substep(render.J2_SUBSTEPS[4], SubstepState.QUEUED),
    ]
    modal.model.error_step_index = 3
    modal.model.error_stderr = "profile 'research' template missing key 'roundtable'"
    _capture(modal, "init_wizard_j2_error.txt", size=_WIDE)


def test_j2_configure_narrow_snapshot() -> None:
    _capture(_repo_configure(), "init_wizard_j2_configure_narrow.txt", size=_NARROW)


# ---- J3 workspace bootstrap -------------------------------------------------


def test_j3_select_snapshot() -> None:
    _capture(_workspace_select(), "init_wizard_j3_select.txt", size=_WIDE)


def test_j3_select_narrow_snapshot() -> None:
    _capture(_workspace_select(), "init_wizard_j3_select_narrow.txt", size=_NARROW)


# ---- J4 done card -----------------------------------------------------------


def _done(*, warn: bool) -> InitWizardModal:
    modal = _repo_step(Step.DONE)
    modal.model.duration_s = 1.4
    modal.model.artifacts = [
        (".ea/state.json", "project ABC"),
        (".ea/profile.yaml", "core · python"),
        ("AGENTS.md", "rendered"),
        (".claude/ plugin", "preview"),
    ]
    if warn:
        modal.model.doctor = [
            render.DoctorCheck("state", True),
            render.DoctorCheck("daemon", True),
            render.DoctorCheck("profile", True),
            render.DoctorCheck("plugin", False, ".claude/ preview missing — run eawf doctor --fix"),
        ]
    else:
        modal.model.doctor = [
            render.DoctorCheck("state", True),
            render.DoctorCheck("daemon", True),
            render.DoctorCheck("profile", True),
            render.DoctorCheck("plugin", True),
        ]
    return modal


def test_j4_done_all_green_snapshot() -> None:
    _capture(_done(warn=False), "init_wizard_j4_done.txt", size=_WIDE)


def test_j4_done_doctor_warn_snapshot() -> None:
    _capture(_done(warn=True), "init_wizard_j4_done_warn.txt", size=_WIDE)


def test_j4_done_narrow_snapshot() -> None:
    _capture(_done(warn=False), "init_wizard_j4_done_narrow.txt", size=_NARROW)


# ---- guard: the seal kill switch is honored for deterministic goldens -------


def test_seal_kill_switch_forces_glyph_fallback() -> None:
    assert os.environ.get(SEAL_DISABLE_ENV) == "1"
    assert seal_capable() is False
