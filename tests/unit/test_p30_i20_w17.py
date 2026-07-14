"""P30-I20-W17: bind ``mockup_golden_diff`` to a real mockup vs real render."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from textual.app import App
from textual.reactive import reactive
from textual.screen import ModalScreen

from eawf.kernel.spec.common import GateSpec
from eawf.surfaces.tui.screens.overlays.init_wizard import (
    InitWizardContext,
    InitWizardModal,
)
from eawf.surfaces.tui.snapshot import pilot_harness, settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckResult, CheckSpec
from eawf.workflow.audit_dsl.kinds.mockup_image_diff import LIVE_CAPTURE_SENTINEL

_REPO_ROOT = Path(__file__).resolve().parents[2]
_THEME = _REPO_ROOT / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_FIXTURES_REL = "tests/fixtures/mockup_image_diff"
_MOCKUP_REL = f"{_FIXTURES_REL}/init_wizard_j1_redesign_mockup.png"
_MOCKUP_SVG_REL = f"{_FIXTURES_REL}/init_wizard_j1_redesign_mockup.svg"
_COMMITTED_SCREENSHOT_REL = f"{_FIXTURES_REL}/init_wizard_j1_actual_tui_screenshot.png"
#: Small committed fixture pair reused by the live-mode wiring tests: a round
#: single-column mockup and two committed renders (faithful + square-drift)
#: standing in for the live capture the monkeypatch returns.
_ROUND_MOCKUP_REL = f"{_FIXTURES_REL}/mockup_round_1col.png"
_FAITHFUL_TUI_REL = f"{_FIXTURES_REL}/tui_round_1col_faithful.png"
_SQUARE_TUI_REL = f"{_FIXTURES_REL}/tui_square_1col_divergent.png"
#: Dotted path to the live PNG capture the image gate calls in live mode.
_CAPTURE_PNG_SYNC = "eawf.surfaces.tui.snapshot.pilot_harness.capture_mockup_golden_screen_png_sync"
_SIZE = (120, 44)
_RESVG = "resvg"
_HAS_RESVG = shutil.which(_RESVG) is not None
#: Opt-in gate for the byte-exact screenshot regeneration check. The live
#: init-wizard render carries box-drawing + sigil glyphs that the vendored
#: EawfTestMono test font does not cover, so a bit-identical screenshot can
#: only be reproduced under the FULL system-font environment the committed
#: fixture was generated in. Pinning the test font (the svg_pixel_diff trick)
#: renders the glyphs as tofu, so it is not an option here. Byte-comparing a
#: fresh render against the fixture is therefore host-font-coupled and would
#: red-light CI on any host whose fonts differ from the fixture origin. The
#: real, host-independent gate is the structured layout-diff over the
#: committed PNGs below; this regen check stays opt-in for the author who
#: refreshes the fixture (``EAWF_W17_SCREENSHOT_REGEN=1``).
_SCREENSHOT_REGEN_OPT_IN = os.environ.get("EAWF_W17_SCREENSHOT_REGEN") == "1"


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


#: The mockup gate this module exercises, as its own fixture.
#:
#: It used to be parsed out of the wave's spec markdown in ``.ea/specs/``. That
#: coupled a permanent test to a TRANSIENT artifact: a wave spec is the
#: authoring vehicle for a wave, and the spec lifecycle archives it out of the
#: tree once its phase closes (the typed criteria + gates persist in
#: ``state.json``; the body stays recoverable from git history). A test that
#: reads one therefore breaks the moment the phase it belongs to is archived --
#: which is a property of the calendar, not of the code under test. The gate is
#: three fixture paths, so the test simply owns them.
_GATE_BODY: dict[str, object] = {
    "id": "G-01",
    "criterion_id": "CR-01",
    "kind": "mockup_golden_diff",
    "args": {
        "golden_path": f"{_FIXTURES_REL}/init_wizard_j1_actual_tui_screenshot.png",
        "mockup_png": f"{_FIXTURES_REL}/init_wizard_j1_redesign_mockup.png",
        "tui_png": f"{_FIXTURES_REL}/init_wizard_j1_actual_tui_screenshot.png",
    },
    "policy": "block",
    "cadence": "every-wave",
}


def _load_spec_gate() -> GateSpec:
    """Return the mockup gate row this module scores."""
    return GateSpec.model_validate(_GATE_BODY)


def _run_gate(gate: GateSpec) -> CheckResult:
    spec = CheckSpec(kind=gate.kind, name=gate.id, args=gate.args)
    return CHECK_REGISTRY["mockup_golden_diff"](spec, _REPO_ROOT)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.skipif(
    not (_HAS_RESVG and _SCREENSHOT_REGEN_OPT_IN),
    reason="host-font-coupled byte-exact regen check; opt in with EAWF_W17_SCREENSHOT_REGEN=1",
)
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


# --- W30 (CR-01): live-render mode captures a Pilot render, not a frozen PNG ---


def _run_live_gate(args: dict[str, object]) -> CheckResult:
    spec = CheckSpec(kind="mockup_golden_diff", name="G-01-live", args=args)
    return CHECK_REGISTRY["mockup_golden_diff"](spec, _REPO_ROOT)


def test_live_mode_captures_pilot_render_not_committed_tui_png(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def _fake_capture(**kwargs: object) -> bytes:
        calls.update(kwargs)
        # Stand-in "live render": a square-corner frame that diverges from the
        # round mockup, so the gate must fail on the CAPTURED bytes.
        return (_REPO_ROOT / _SQUARE_TUI_REL).read_bytes()

    monkeypatch.setattr(_CAPTURE_PNG_SYNC, _fake_capture)

    result = _run_live_gate(
        {
            "golden_path": _ROUND_MOCKUP_REL,
            "mockup_png": _ROUND_MOCKUP_REL,
            "tui_png": LIVE_CAPTURE_SENTINEL,
            "scope": "repo",
            "mode": "home",
            "key_sequence": ["down"],
            "size": [100, 30],
        }
    )

    # The live-capture path was taken (the committed tui_png was NOT read):
    # the reused text-mode selectors are forwarded to the capture.
    assert calls["scope"] == "repo"
    assert calls["mode"] == "home"
    assert calls["key_sequence"] == ["down"]
    assert calls["size"] == (100, 30)
    # And the gate fails because the live render diverges from the mockup golden.
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert "tui=<live>" in result.details
    assert "border_shape_mismatch=True" in result.details
    assert "square-vs-round" in result.details


def test_live_mode_faithful_render_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _CAPTURE_PNG_SYNC,
        lambda **_kwargs: (_REPO_ROOT / _FAITHFUL_TUI_REL).read_bytes(),
    )
    result = _run_live_gate(
        {
            "golden_path": _ROUND_MOCKUP_REL,
            "mockup_png": _ROUND_MOCKUP_REL,
            "tui_png": LIVE_CAPTURE_SENTINEL,
            "scope": "repo",
            "size": [100, 30],
        }
    )
    assert result.status == "pass"
    assert result.passed is True
    assert result.details is not None
    assert "tui=<live>" in result.details
    assert "border_shape_mismatch=False" in result.details


def test_live_mode_blocks_cleanly_when_resvg_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_resvg(**_kwargs: object) -> bytes:
        raise FileNotFoundError("resvg")

    monkeypatch.setattr(_CAPTURE_PNG_SYNC, _no_resvg)
    result = _run_live_gate(
        {
            "golden_path": _ROUND_MOCKUP_REL,
            "mockup_png": _ROUND_MOCKUP_REL,
            "tui_png": LIVE_CAPTURE_SENTINEL,
            "scope": "repo",
        }
    )
    assert result.status == "blocked"
    assert result.passed is False
    assert "resvg not installed" in (result.details or "")


def test_live_capture_bad_state_path_fails_not_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A missing state_path in live mode degrades to a failed check; the capture
    # is never reached.
    monkeypatch.setattr(
        _CAPTURE_PNG_SYNC,
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("capture must not run")),
    )
    result = _run_live_gate(
        {
            "golden_path": _ROUND_MOCKUP_REL,
            "mockup_png": _ROUND_MOCKUP_REL,
            "tui_png": LIVE_CAPTURE_SENTINEL,
            "state_path": f"{_FIXTURES_REL}/does_not_exist_state.json",
            "scope": "repo",
        }
    )
    assert result.status == "fail"
    assert "state_path" in (result.details or "")


@pytest.mark.skipif(not _HAS_RESVG, reason="resvg not installed")
def test_live_capture_real_resvg_render_runs_end_to_end() -> None:
    # Exercise the true capture -> export_screenshot -> resvg -> decode chain.
    png = pilot_harness.capture_mockup_golden_screen_png_sync(
        scope="user",
        state_path=None,
        mode=None,
        key_sequence=[],
        size=(100, 30),
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000

    result = _run_live_gate(
        {
            "golden_path": _MOCKUP_REL,
            "mockup_png": _MOCKUP_REL,
            "tui_png": LIVE_CAPTURE_SENTINEL,
            "scope": "user",
            "size": [100, 30],
        }
    )
    # A live render was captured + diffed (not blocked, not an error path).
    assert result.status in {"pass", "fail"}
    assert result.details is not None
    assert "tui=<live>" in result.details
