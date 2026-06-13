"""P30-I20-W17: bind ``mockup_golden_diff`` to a real mockup vs real render."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml
from textual.app import App
from textual.reactive import reactive
from textual.screen import ModalScreen

from eawf.kernel.spec.common import GateSpec
from eawf.kernel.spec.wave_body import WaveSpecBody
from eawf.surfaces.tui.screens.overlays.init_wizard import (
    InitWizardContext,
    InitWizardModal,
)
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckResult, CheckSpec

_REPO_ROOT = Path(__file__).resolve().parents[2]
_THEME = _REPO_ROOT / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_FIXTURES_REL = "tests/fixtures/mockup_image_diff"
_MOCKUP_REL = f"{_FIXTURES_REL}/init_wizard_j1_redesign_mockup.png"
_MOCKUP_SVG_REL = f"{_FIXTURES_REL}/init_wizard_j1_redesign_mockup.svg"
_COMMITTED_SCREENSHOT_REL = f"{_FIXTURES_REL}/init_wizard_j1_actual_tui_screenshot.png"
_SPEC = _REPO_ROOT / ".ea" / "specs" / "P30" / "P30-I20" / "P30-I20-W17.md"
_SIZE = (120, 44)
_RESVG = "resvg"
_HAS_RESVG = shutil.which(_RESVG) is not None


class _HostApp(App[None]):
    """Bare themed host for the live init-wizard Pilot capture."""

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


def _render_svg_to_png(svg: str) -> bytes:
    """Rasterize an SVG through the same ``resvg`` path as visual gates."""
    with tempfile.TemporaryDirectory() as tmp:
        svg_path = Path(tmp) / "input.svg"
        png_path = Path(tmp) / "output.png"
        svg_path.write_text(svg, encoding="utf-8")
        subprocess.run(
            [_RESVG, str(svg_path), str(png_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return png_path.read_bytes()


def _capture_live_init_wizard_svg() -> str:
    """Mount the real init wizard under Pilot and return Textual's SVG screenshot."""

    async def body() -> str:
        modal = InitWizardModal(
            InitWizardContext(
                scope="user",
                target_dir=Path("fixture-repo"),
                init_needed=True,
                git_root_found=True,
            )
        )
        app = _HostApp(modal)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            return app.export_screenshot(title="init wizard", simplify=True)

    return asyncio.run(body())


def _capture_live_init_wizard_png() -> bytes:
    """Return the real Textual screenshot rasterized to PNG."""
    return _render_svg_to_png(_capture_live_init_wizard_svg())


def _load_spec_gate() -> GateSpec:
    """Parse and return the committed P30-I20-W17 mockup gate row."""
    body = _SPEC.read_text(encoding="utf-8")
    block = body.split("```eawf-wave-body", 1)[1].split("```", 1)[0]
    parsed = WaveSpecBody.from_mapping(yaml.safe_load(block))
    return next(gate for gate in parsed.gates if gate.id == "G-01")


def _run_gate(gate: GateSpec) -> CheckResult:
    spec = CheckSpec(kind=gate.kind, name=gate.id, args=gate.args)
    return CHECK_REGISTRY["mockup_golden_diff"](spec, _REPO_ROOT)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.skipif(not _HAS_RESVG, reason="resvg not installed")
def test_actual_tui_screenshot_fixture_matches_textual_export() -> None:
    committed = (_REPO_ROOT / _COMMITTED_SCREENSHOT_REL).read_bytes()
    regenerated = _capture_live_init_wizard_png()

    assert _sha256(committed) == _sha256(regenerated)


@pytest.mark.skipif(not _HAS_RESVG, reason="resvg not installed")
def test_redesign_mockup_fixture_matches_svg_source() -> None:
    committed = (_REPO_ROOT / _MOCKUP_REL).read_bytes()
    regenerated = _render_svg_to_png((_REPO_ROOT / _MOCKUP_SVG_REL).read_text(encoding="utf-8"))

    assert _sha256(committed) == _sha256(regenerated)


def test_structured_gate_row_diffs_real_mockup_against_actual_tui_screenshot() -> None:
    gate = _load_spec_gate()
    mockup = (_REPO_ROOT / gate.args["mockup_png"]).read_bytes()
    screenshot = (_REPO_ROOT / gate.args["tui_png"]).read_bytes()

    assert _sha256(mockup) != _sha256(screenshot)
    result = _run_gate(gate)

    assert result.status == "pass"
    assert result.passed is True
    assert result.details is not None
    assert "border_shape_mismatch=False" in result.details
    assert "column_count_mismatch=False" in result.details
    assert f"mockup={_MOCKUP_REL}" in result.details


@pytest.mark.skipif(not _HAS_RESVG, reason="resvg not installed")
def test_real_binding_blocks_square_corner_drift(tmp_path: Path) -> None:
    gate = _load_spec_gate()
    tui_png = tmp_path / "init-wizard-square-drift.png"
    drift_svg = (
        (_REPO_ROOT / _MOCKUP_SVG_REL)
        .read_text(encoding="utf-8")
        .replace(
            'rx="11" ry="11"',
            'rx="0" ry="0"',
        )
    )
    drift_svg = drift_svg.replace('stroke-width="6"', 'stroke-width="80"', 1)
    tui_png.write_bytes(_render_svg_to_png(drift_svg))

    args = dict(gate.args)
    args["golden_path"] = str(tui_png)
    args["tui_png"] = str(tui_png)
    result = CHECK_REGISTRY["mockup_golden_diff"](
        CheckSpec(kind=gate.kind, name="G-01-square-drift", args=args),
        _REPO_ROOT,
    )

    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert "border_shape_mismatch=True" in result.details
    assert "square-vs-round" in result.details


def test_wave_spec_carries_structured_real_mockup_gate_row() -> None:
    gate = _load_spec_gate()

    assert gate.kind == "mockup_golden_diff"
    assert gate.args["mockup_png"] == _MOCKUP_REL
    assert gate.args["tui_png"] == _COMMITTED_SCREENSHOT_REL
    assert "mockup_round_1col" not in str(gate.args)
