"""Golden snapshot + drill-key resolution for the calibration drill (P29-I13-W11).

The jury-calibration
:class:`~eawf.surfaces.tui.modals.calibration_drill.CalibrationDrillModal`
renders the Brier score and the expected calibration error (ECE) over the
jury's graded predictions. The jury is idle in v0.5 (no graded predictions
land), so the COMMON path is honest-empty -- ``no calibration set yet`` --
and the numbers appear only once a calibration set is supplied.

This module pins both criteria of the wave:

* **CR-01 (snapshot)** -- the modal shows the Brier score + ECE when a
  calibration set is bound, and the honest-empty notice when none is (golden
  snapshots of both states).
* **CR-02 (affordance parity)** -- the Trust mode advertises a ``K
  calibration`` footer key that resolves to a live
  :class:`~textual.binding.Binding` opening the calibration-detail modal,
  even in the honest-empty (no-calibration) mount the affordance gate probes.
  Driven here through the real key->Binding probe + a Pilot keypress so a
  green test proves the advertised key is not dead.

Regenerate the goldens after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_calibration_drill.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modals.calibration_drill import (
    NO_CALIBRATION_NOTICE,
    CalibrationDrillModal,
    CalibrationSet,
    render_calibration_lines,
)
from eawf.surfaces.tui.modes.trust import TrustModeScreen
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.snapshot.behaviour_probe import ProbeStatus, record_keypress_transcript
from eawf.surfaces.tui.widgets.git_pane import GitFields

_SIZE = (120, 40)
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"
_COMMIT = "calibration-drill-test"


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


def _calibration() -> CalibrationSet:
    """A jury calibration set with a Brier score, ECE, and sample count."""
    return CalibrationSet(brier_score=0.125, ece=0.0625, sample_count=20)


# --------------------------------------------------------------------------
# Pure helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_render_calibration_lines_shows_brier_ece_and_samples() -> None:
    """A bound set renders the Brier score, ECE, and sample count."""
    lines = render_calibration_lines(_calibration())
    assert lines == ("Brier score 0.125", "ECE 0.062", "samples 20")


def test_render_calibration_lines_none_is_honest_empty() -> None:
    """No calibration set renders the honest-empty notice (the common path)."""
    assert render_calibration_lines(None) == (NO_CALIBRATION_NOTICE,)


def test_render_calibration_lines_zero_metrics_still_render() -> None:
    """A real zero-Brier set renders the numbers, not the absence notice."""
    lines = render_calibration_lines(CalibrationSet(brier_score=0.0, ece=0.0, sample_count=1))
    assert "Brier score 0.000" in lines
    assert NO_CALIBRATION_NOTICE not in lines


# --------------------------------------------------------------------------
# CR-01: snapshot -- the modal shows Brier + ECE (and the empty notice)
# --------------------------------------------------------------------------


def test_calibration_drill_snapshot_with_set() -> None:
    """The calibration drill renders the Brier score + ECE when bound."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(CalibrationDrillModal(_calibration()))
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "Brier score 0.125" in frame
            assert "ECE 0.062" in frame
            assert "samples 20" in frame
            assert_screen_snapshot(app, _GOLDEN / "calibration_drill.txt")

    asyncio.run(body())


def test_calibration_drill_snapshot_honest_empty() -> None:
    """The calibration drill renders the honest-empty notice with no set."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(CalibrationDrillModal(None))
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert NO_CALIBRATION_NOTICE in frame
            assert_screen_snapshot(app, _GOLDEN / "calibration_drill_empty.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# CR-02: the advertised ``K calibration`` key resolves + opens the modal
# --------------------------------------------------------------------------


def test_calibration_key_advertised_in_trust_footer() -> None:
    """The Trust mode footer advertises the ``K calibration`` drill key."""
    from eawf.surfaces.tui.modes.trust import _TRUST_HINTS

    assert any(hint.startswith("K ") for hint in _TRUST_HINTS)


def test_calibration_key_resolves_in_honest_empty_trust_mode() -> None:
    """The advertised ``K`` key resolves to a live binding with NO set bound.

    Drives ``K`` through the real key->Binding probe in the same data-starved
    mount the affordance matrix uses (no calibration pushed), so the key must
    resolve (not classify UNRESOLVED) for the advertised affordance to be live.
    """

    async def body() -> ProbeStatus:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await app.switch_mode("trust")
            await settle_screen(pilot)
            transcript = await record_keypress_transcript(pilot, ["K"], source_commit=_COMMIT)
            return transcript.outcomes[0].status

    status = asyncio.run(body())
    assert status is not ProbeStatus.UNRESOLVED


def test_calibration_key_opens_modal() -> None:
    """Pressing ``K`` in the Trust mode opens the calibration drill modal."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust
            await settle_screen(pilot)
            screen = cast(TrustModeScreen, app.screen)
            screen.set_calibration(_calibration())
            await settle_screen(pilot)
            depth_before = app.modal_depth()
            await pilot.press("K")  # open the calibration drill
            await settle_screen(pilot)
            assert app.modal_depth() == depth_before + 1
            assert isinstance(app.screen, CalibrationDrillModal)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "Brier score 0.125" in frame

    asyncio.run(body())
