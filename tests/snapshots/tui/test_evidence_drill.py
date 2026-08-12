"""Golden snapshot + drill-key resolution for the why-peek modal.

The why-peek :class:`~eawf.surfaces.tui.modals.evidence_drill.EvidenceDrillModal`
renders one close-readiness criterion's evidence chain -- the criterion id +
status, each gate outcome (``G-01 pass`` / ``G-02 fail``), and the joined
evidence rows -- so the operator sees WHY the criterion landed at its status.

This module pins both criteria of the wave:

* **CR-01 (snapshot)** -- the modal shows the selected criterion's evidence
  chain with each gate outcome (golden snapshot).
* **CR-02 (affordance parity)** -- the Evidence mode advertises a ``p peek``
  footer key that resolves to a live :class:`~textual.binding.Binding` opening
  the drill modal. Driven here through the real key->Binding probe + a Pilot
  keypress so a green test proves the advertised key is not dead.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_evidence_drill.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modals.evidence_drill import (
    NO_EVIDENCE_NOTICE,
    NO_GATES_NOTICE,
    EvidenceDrillModal,
    evidence_chain_lines,
    gate_outcome_lines,
    render_evidence_chain,
)
from eawf.surfaces.tui.modes.evidence import EvidenceModeScreen
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.snapshot.behaviour_probe import ProbeStatus, record_keypress_transcript
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.workflow.verify.models import CloseReadiness, CriterionView, GateResult

_SIZE = (120, 40)
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"
_NOW = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
_COMMIT = "evidence-drill-test"


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


def _failing_criterion() -> CriterionView:
    """A criterion with two gate outcomes (one pass, one fail)."""
    return CriterionView(
        id="CR-02",
        source="spec",
        status="fail",
        gate_results=[
            GateResult(gate_id="G-01", status="pass"),
            GateResult(gate_id="G-02", status="fail"),
        ],
    )


def _records() -> tuple[EvidenceRecord, ...]:
    """Evidence rows joined to CR-02."""
    return (
        EvidenceRecord(
            id="EV-bbbbbbbbbbbb",
            scope_id="P01-I01-W01",
            produced_by="agent",
            evidence_kind="jury",
            status="fail",
            summary="gate G-02 failed under pytest",
            refs=["G-02", "CR-02"],
            metrics={"criterion_id": "CR-02"},
            created_at=_NOW,
        ),
    )


def _readiness() -> CloseReadiness:
    """A close-readiness view whose first ledger row is the failing CR-02."""
    return CloseReadiness(
        ready=False,
        criteria=[
            _failing_criterion(),
            CriterionView(
                id="CR-03",
                source="legacy",
                status="pending",
                gate_results=None,
            ),
        ],
    )


# --------------------------------------------------------------------------
# Pure chain helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_gate_outcome_lines_one_per_gate() -> None:
    """Each gate outcome renders one ``<gate> <status>`` line."""
    assert gate_outcome_lines(_failing_criterion()) == ("G-01 pass", "G-02 fail")


def test_gate_outcome_lines_no_gates_notice() -> None:
    """A criterion with no gate results renders the honest-empty notice."""
    legacy = CriterionView(id="CR-9", source="legacy", status="pending", gate_results=None)
    assert gate_outcome_lines(legacy) == (NO_GATES_NOTICE,)


def test_evidence_chain_lines_one_per_record() -> None:
    """Each evidence row renders a producer/status/summary line."""
    lines = evidence_chain_lines(_records())
    assert lines == ("agent fail -- gate G-02 failed under pytest",)


def test_evidence_chain_lines_empty_notice() -> None:
    """No evidence rows renders the honest-empty notice."""
    assert evidence_chain_lines(()) == (NO_EVIDENCE_NOTICE,)


def test_render_evidence_chain_shows_criterion_gates_and_evidence() -> None:
    """The full chain block carries the header, gate lines, and evidence lines."""
    block = render_evidence_chain(_failing_criterion(), _records())
    assert "CR-02 :: fail" in block
    assert "G-01 pass" in block
    assert "G-02 fail" in block
    assert "agent fail -- gate G-02 failed under pytest" in block


# --------------------------------------------------------------------------
# CR-01: snapshot -- the modal shows the criterion evidence chain
# --------------------------------------------------------------------------


def test_evidence_drill_snapshot() -> None:
    """The why-peek modal renders CR-02's evidence chain with each gate outcome."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(EvidenceDrillModal(_failing_criterion(), _records()))
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "CR-02" in frame
            assert "G-01 pass" in frame
            assert "G-02 fail" in frame
            assert "agent fail" in frame
            assert_screen_snapshot(app, _GOLDEN / "evidence_drill.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# CR-02: the advertised ``p peek`` key resolves + opens the drill modal
# --------------------------------------------------------------------------


def test_drill_key_advertised_in_evidence_footer() -> None:
    """The Evidence mode footer advertises the ``p peek`` drill key."""
    from eawf.surfaces.tui.modes.evidence import _EVIDENCE_HINTS

    assert any(hint.startswith("p ") for hint in _EVIDENCE_HINTS)


def test_drill_key_resolves_to_live_binding() -> None:
    """The advertised ``p`` key resolves to a live binding in the evidence mode.

    Drives ``p`` through the real key->Binding probe with NO readiness bound
    (the same data-starved mount the affordance matrix uses), so the key must
    resolve (not classify UNRESOLVED) for the advertised affordance to be live.
    """

    async def body() -> ProbeStatus:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await app.switch_mode("evidence")
            await settle_screen(pilot)
            transcript = await record_keypress_transcript(pilot, ["p"], source_commit=_COMMIT)
            return transcript.outcomes[0].status

    status = asyncio.run(body())
    assert status is not ProbeStatus.UNRESOLVED


def test_drill_key_opens_modal_for_selected_criterion() -> None:
    """Pressing ``p`` over a selected ledger criterion opens the drill modal."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")  # -> evidence mode
            await settle_screen(pilot)
            screen = cast(EvidenceModeScreen, app.screen)
            screen.set_readiness(_readiness(), _records())
            await settle_screen(pilot)
            depth_before = app.modal_depth()
            await pilot.press("p")  # drill into the selected criterion
            await settle_screen(pilot)
            assert app.modal_depth() == depth_before + 1
            assert isinstance(app.screen, EvidenceDrillModal)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "CR-02" in frame
            assert "G-02 fail" in frame

    asyncio.run(body())
