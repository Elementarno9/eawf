"""Calibration-readiness tile in the Trust mode (P29-I13-W31).

The Trust mode (digit ``4``) gains a CALIBRATION READINESS tile: the count
of closed waves whose actual records a captured ``elapsed_eu`` against the
bucket re-fit floor, with a ready / not-ready verdict
(:func:`~eawf.surfaces.tui.modes.trust.compute_calibration_readiness`). The
captured elapsed EU is the B069 recalibration input the close path now
records from session runtime, so the tile answers "does the bucket re-fit
have enough captured data to act on yet".

The measurable signal is the snapshot assertion: the trust tile shows the
calibration-readiness state derived from captured elapsed_eu.

This module pins:

* the pure readiness helpers (no Textual mount) -- counting closed waves
  with positive captured elapsed EU, the ready / not-ready boundary, and
  the render verdict; and
* the Pilot-driven tile -- pushing a readiness tally via
  ``set_calibration_readiness`` surfaces the verdict.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_trust_tile_calibration.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    ActualStatus,
    EffortBucket,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    ActualSummary,
    CurrentPointers,
    Project,
    State,
)
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.trust import (
    CalibrationReadiness,
    TrustModeScreen,
    compute_calibration_readiness,
    render_calibration_readiness,
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
_NOW = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)


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


def _state_with_captured_waves(captured: int, *, bucket_count: int = 0) -> State:
    """Build a state with *captured* closed waves carrying positive elapsed EU.

    *bucket_count* extra closed waves carry a zero-EU auto-actual (no captured
    runtime) so the helper exercises the positive-elapsed filter.
    """
    waves: dict[str, dict[str, object]] = {}
    actuals: dict[str, dict[str, object]] = {}
    for index in range(captured + bucket_count):
        wave_id = f"P01-I01-W{index + 1:02d}"
        waves[wave_id] = {
            "id": wave_id,
            "iter_id": "P01-I01",
            "title": "w",
            "status": WaveStatus.CLOSED.value,
            "effort_bucket": EffortBucket.M.value,
            "file_scopes": ["src/"],
            "deps": [],
            "success_criteria": [],
            "opened_at": _NOW.isoformat(),
            "closed_at": _NOW.isoformat(),
        }
        elapsed = 1.0 if index < captured else 0.0
        actuals[wave_id] = ActualSummary(
            id=f"ACT-{wave_id}",
            scope_id=wave_id,
            status=ActualStatus.DONE,
            elapsed_eu=elapsed,
            actual_tokens=100,
            current_store_record_id=f"REC-{wave_id}",
            updated_at=_NOW,
        ).model_dump(mode="json")
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _NOW.isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": waves,
            "actuals": actuals,
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


# --------------------------------------------------------------------------
# Pure helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_compute_calibration_readiness_counts_captured_waves() -> None:
    """Three closed waves with positive elapsed EU -> captured 3."""
    state = _state_with_captured_waves(3)
    readiness = compute_calibration_readiness(state, threshold=5)
    assert readiness.captured_waves == 3
    assert readiness.threshold == 5
    assert readiness.ready is False


def test_compute_calibration_readiness_excludes_zero_elapsed() -> None:
    """A closed wave with a zero-EU auto-actual is not counted as captured."""
    state = _state_with_captured_waves(2, bucket_count=3)
    readiness = compute_calibration_readiness(state, threshold=5)
    # Only the two positive-elapsed waves count; the three zero-EU ones do not.
    assert readiness.captured_waves == 2


def test_compute_calibration_readiness_ready_at_threshold() -> None:
    """At the floor the readiness flips ready (boundary case)."""
    state = _state_with_captured_waves(5)
    readiness = compute_calibration_readiness(state, threshold=5)
    assert readiness.captured_waves == 5
    assert readiness.ready is True


def test_compute_calibration_readiness_empty_state() -> None:
    """No closed waves -> zero captured, not ready."""
    state = _state_with_captured_waves(0)
    readiness = compute_calibration_readiness(state, threshold=5)
    assert readiness.captured_waves == 0
    assert readiness.ready is False


def test_render_calibration_readiness_not_ready() -> None:
    """The tile shows the captured count, the floor, and the not-ready verdict."""
    body = render_calibration_readiness(CalibrationReadiness(captured_waves=2, threshold=5))
    assert "captured waves 2 / 5" in body
    assert "not-ready" in body


def test_render_calibration_readiness_ready() -> None:
    """A met floor renders the ready verdict."""
    body = render_calibration_readiness(CalibrationReadiness(captured_waves=5, threshold=5))
    assert "captured waves 5 / 5" in body
    assert "ready" in body


# --------------------------------------------------------------------------
# Pilot-driven tile -- pushed readiness surfaces the verdict
# --------------------------------------------------------------------------


def test_trust_pane_shows_calibration_readiness_tile() -> None:
    """The Trust mode renders the calibration-readiness state from elapsed EU."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, TrustModeScreen)
            screen.set_calibration_readiness(CalibrationReadiness(captured_waves=3, threshold=5))
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "CALIBRATION READINESS" in frame
            assert "captured waves 3 / 5" in frame
            assert "not-ready" in frame
            assert_screen_snapshot(app, _GOLDEN / "trust_calibration_readiness.txt")

    asyncio.run(body())
