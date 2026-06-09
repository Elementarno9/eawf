"""``svg_pixel_diff`` golden for the EA_CB status-tint legend (P30-I02-W01).

Pins the five-swatch lifecycle band legend (pending / claimed /
in_progress / closed / failed, EA_CB / IBM colourblind-safe hexes) as a
visual golden so a palette edit that recolours a band reds the gate:

* a structural assert that the committed ``status_legend.svg`` fixture is
  byte-identical to what :func:`render_status_legend_svg` emits today (a
  cheap, resvg-free guard so a palette edit reds even on a host without the
  renderer); and
* the T5 ``svg_pixel_diff`` byte-comparison of the resvg render against the
  committed golden PNG (skip-guarded on a host without the pinned resvg;
  CI runs the real diff).

Regenerate the golden after an intentional palette change with::

    EAWF_REFRESH_GOLDEN=1 uv run pytest tests/snapshots/svg/test_status_legend_golden.py

which also rewrites the committed fixture SVG so the two stay paired.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from eawf.surfaces.tui.widgets.cvd import render_status_legend_svg
from eawf.workflow.audit_dsl.kinds.svg_pixel_diff import check_svg_pixel_diff
from eawf.workflow.audit_dsl.models import CheckSpec

_SVG_DIR = Path(__file__).parent
_FIXTURES = _SVG_DIR / "fixtures"
_FONTS = _SVG_DIR / "fonts"
_GOLDEN = _SVG_DIR / "golden"
_REPO_ROOT = _SVG_DIR.parents[2]

_LEGEND_SVG = "tests/snapshots/svg/fixtures/status_legend.svg"
_LEGEND_GOLDEN = "tests/snapshots/svg/golden/status_legend.png"
_FONTS_REL = "tests/snapshots/svg/fonts"

_HAS_RESVG = shutil.which("resvg") is not None


def _render_legend_png() -> bytes:
    """Render the committed legend SVG to PNG bytes via the pinned resvg.

    Replicates the exact ``resvg`` argv ``check_svg_pixel_diff`` shells out
    (vendored ``--use-fonts-dir`` + ``--skip-system-fonts``) so a refreshed
    golden is byte-identical to what the T5 oracle produces at verify time.

    Raises:
        RuntimeError: When the resvg render exits non-zero or writes no PNG.
    """
    svg_path = _REPO_ROOT / _LEGEND_SVG
    with tempfile.TemporaryDirectory() as tmp:
        rendered = Path(tmp) / "rendered.png"
        argv = [
            "resvg",
            "--use-fonts-dir",
            str(_FONTS),
            "--skip-system-fonts",
            str(svg_path),
            str(rendered),
        ]
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        if completed.returncode != 0 or not rendered.is_file():
            diagnostic = completed.stderr.strip() or completed.stdout.strip() or "render failed"
            raise RuntimeError(f"resvg render failed: {diagnostic}")
        return rendered.read_bytes()


@pytest.mark.skipif(not _HAS_RESVG, reason="resvg not installed")
def test_refresh_status_legend_golden() -> None:
    """Rewrite the committed legend fixture + golden under a refresh env var.

    Driven with ``EAWF_REFRESH_GOLDEN=1`` (or ``EAWF_SNAPSHOT_REGEN=1``): it
    re-emits the fixture SVG from :func:`render_status_legend_svg` and
    re-renders the golden PNG so the two stay paired after a palette change.
    Without the env var the node skips, so a normal run never rewrites them.
    """
    if not (os.environ.get("EAWF_REFRESH_GOLDEN") or os.environ.get("EAWF_SNAPSHOT_REGEN")):
        pytest.skip("set EAWF_REFRESH_GOLDEN=1 (or EAWF_SNAPSHOT_REGEN=1) to refresh")
    (_FIXTURES / "status_legend.svg").write_text(render_status_legend_svg(), encoding="utf-8")
    (_GOLDEN / "status_legend.png").write_bytes(_render_legend_png())


def test_committed_legend_assets_present() -> None:
    """The committed legend SVG + golden PNG exist on disk."""
    assert (_FIXTURES / "status_legend.svg").is_file()
    assert (_GOLDEN / "status_legend.png").is_file()


def test_legend_fixture_matches_exporter() -> None:
    """The committed legend SVG is byte-identical to the exporter output.

    The resvg-free structural pin: a palette edit changes
    :func:`render_status_legend_svg`, so the committed fixture would drift
    from the live exporter and red this gate even on a host without resvg.
    """
    on_disk = (_FIXTURES / "status_legend.svg").read_text(encoding="utf-8")
    assert on_disk == render_status_legend_svg()


@pytest.mark.skipif(not _HAS_RESVG, reason="resvg not installed")
def test_status_legend_render_matches_golden() -> None:
    """The legend SVG renders bit-identically to its committed golden (T5)."""
    spec = CheckSpec(
        kind="svg_pixel_diff",
        name="legend-match",
        args={"svg": _LEGEND_SVG, "golden": _LEGEND_GOLDEN, "fonts_dir": _FONTS_REL},
    )
    result = check_svg_pixel_diff(spec, _REPO_ROOT)
    assert result.status == "pass", result.details


def test_status_legend_pixel_diff_blocked_without_resvg() -> None:
    """Without resvg the kind returns ``blocked`` (not fail / raise)."""
    if _HAS_RESVG:
        pytest.skip("resvg installed; blocked-path assertion is host-without-resvg only")
    spec = CheckSpec(
        kind="svg_pixel_diff",
        name="legend-blocked",
        args={"svg": _LEGEND_SVG, "golden": _LEGEND_GOLDEN, "fonts_dir": _FONTS_REL},
    )
    result = check_svg_pixel_diff(spec, _REPO_ROOT)
    assert result.status == "blocked"
    assert result.details == "resvg not installed"
