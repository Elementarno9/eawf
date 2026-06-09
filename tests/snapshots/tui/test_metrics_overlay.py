"""Golden snapshot for the reskinned ``/metrics`` dashboard (P30-I02-W23).

The :class:`~eawf.surfaces.tui.screens.overlays.metrics.MetricsModal`
dashboard migrates to the cosmic-terminal reskin language: the title leads
with the shared overview sigil tinted ``$accent`` green, and the footer pins
the frozen honest-negative literal
``honest-negative · lights up after EU capture`` (a real middle dot,
U+00B7). The literal is honest until EU capture lands (I04) -- the outer eawf
harness does not yet instrument EU, so every telemetry-backed tile stays
honestly dark; the dashboard never fabricates a metric value.

This module pins the wave's close-gate bar for the metrics overlay:

* **frozen literal** -- the rendered overlay carries the exact
  ``METRICS_HONEST_NEGATIVE`` literal (asserted both against the captured
  frame and against the golden snapshot).

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_metrics_overlay.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.metrics import (
    METRICS_HONEST_NEGATIVE,
    MetricsModal,
)
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields

_SIZE = (120, 40)
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every git probe so the rendered chrome is deterministic."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )
    monkeypatch.setattr("eawf.surfaces.tui.widgets.git_pane._git_run", lambda *a, **k: None)


def test_metrics_honest_negative_literal_is_frozen() -> None:
    """The honest-negative literal carries the exact frozen text + middle dot."""
    assert METRICS_HONEST_NEGATIVE == "honest-negative · lights up after EU capture"


def test_metrics_overlay_snapshot_pins_honest_negative() -> None:
    """The reskinned dashboard renders + pins the frozen honest-negative line."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(MetricsModal())
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert METRICS_HONEST_NEGATIVE in frame
            assert "Metrics" in frame
            assert_screen_snapshot(app, _GOLDEN / "metrics_overlay.txt")

    asyncio.run(body())
