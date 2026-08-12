"""Golden snapshot for the Evidence-mode close-readiness header.

The Evidence mode (digit ``6``) leads its body with a close-readiness
header row that tallies the passed-over-total typed-criterion count -- e.g.
``criteria: 3/4 ready`` -- so the operator reads how close the active
scope is to a clean close at a glance.

The render seam never calls
:func:`~eawf.workflow.verify.readiness.compute` (it spawns live gate
subprocesses, which would recurse pytest inside this test); the header is
painted from a typed :class:`~eawf.workflow.verify.models.CloseReadiness`
fixture pushed in via :meth:`EvidenceModeScreen.set_readiness`, so the
snapshot is deterministic and subprocess-free.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_evidence_header.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Static

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.evidence import (
    EvidenceModeScreen,
    close_readiness_header,
    criterion_ready_count,
)
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.workflow.verify.models import CloseReadiness, CriterionView, GateResult

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
    """Stub the workspace git probe to a deterministic clean tree."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


def _readiness_three_of_four() -> CloseReadiness:
    """Build a typed close-readiness view with 3 of 4 criteria ready.

    Three criteria pass (one via a waiver, the other two via passing gate
    results) and one fails, so the header tally reads ``3/4 ready``.
    """
    return CloseReadiness(
        ready=False,
        criteria=[
            CriterionView(
                id="CR-01",
                source="spec",
                status="pass",
                gate_results=[GateResult(gate_id="G-01", status="pass")],
            ),
            CriterionView(
                id="CR-02",
                source="spec",
                status="pass",
                gate_results=[GateResult(gate_id="G-02", status="pass")],
            ),
            CriterionView(
                id="CR-03",
                source="spec",
                status="waived",
                gate_results=[GateResult(gate_id="G-03", status="blocked")],
            ),
            CriterionView(
                id="CR-04",
                source="spec",
                status="fail",
                gate_results=[GateResult(gate_id="G-04", status="fail")],
            ),
        ],
    )


# --------------------------------------------------------------------------
# Pure helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_criterion_ready_count_tallies_pass_and_waived() -> None:
    """The ready tally counts pass + waived criteria over the total."""
    assert criterion_ready_count(_readiness_three_of_four()) == (3, 4)


def test_close_readiness_header_renders_passed_over_total() -> None:
    """The header renders the passed-over-total count."""
    assert close_readiness_header(_readiness_three_of_four()) == "criteria: 3/4 ready"


def test_close_readiness_header_empty_is_no_criteria_notice() -> None:
    """A view with no typed criteria yields the honest-empty notice."""
    empty = CloseReadiness(ready=True, criteria=[])
    assert close_readiness_header(empty) == "criteria: none"


# --------------------------------------------------------------------------
# Snapshot: the header row renders the passed/total count in the mode
# --------------------------------------------------------------------------


def test_evidence_header_snapshot() -> None:
    """The Evidence mode's close-readiness header shows ``criteria: 3/4 ready``.

    Mounts the evidence screen, pushes a typed 3-of-4 close-readiness view in
    via :meth:`EvidenceModeScreen.set_readiness`, and snapshots the frame so a
    layout regression on the header row is caught.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")  # -> evidence mode
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, EvidenceModeScreen)
            screen.set_readiness(_readiness_three_of_four())
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            # The close-readiness header row renders the passed/total count.
            assert "criteria: 3/4 ready" in frame
            assert screen.query_one("#evidence-readiness", Static) is not None
            assert_screen_snapshot(app, _GOLDEN / "evidence_header.txt")

    asyncio.run(body())
