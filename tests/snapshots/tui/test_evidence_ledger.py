"""Golden snapshot for the Evidence-mode close-readiness ledger.

The Evidence mode renders a close-readiness ledger over the active scope's
typed criteria: one row per :class:`~eawf.workflow.verify.models.CloseReadiness`
criterion carrying the criterion id, its rolled-up gate status, the
``produced_by`` of the joined evidence row, and the criterion status.

The render seam never calls
:func:`~eawf.workflow.verify.readiness.compute` (it spawns live gate
subprocesses); the ledger is built from a typed ``CloseReadiness`` +
:class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` fixtures pushed in
via :meth:`EvidenceModeScreen.set_readiness`, so the snapshot is
deterministic and subprocess-free.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_evidence_ledger.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.widgets import DataTable

from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.evidence import (
    EvidenceModeScreen,
    LedgerRow,
    build_evidence_ledger,
    gate_status_label,
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
_NOW = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)


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


def _readiness() -> CloseReadiness:
    """Build a typed close-readiness view with three mixed-status criteria."""
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
                status="fail",
                gate_results=[GateResult(gate_id="G-02", status="fail")],
            ),
            CriterionView(
                id="CR-03",
                source="legacy",
                status="pending",
                gate_results=None,
            ),
        ],
    )


def _records() -> tuple[EvidenceRecord, ...]:
    """Build evidence rows joining producers to CR-01 and CR-02."""
    return (
        EvidenceRecord(
            id="EV-aaaaaaaaaaaa",
            scope_id="P01-I01-W01",
            produced_by="tool",
            evidence_kind="deterministic",
            status="pass",
            summary="gate G-01 passed",
            refs=["G-01", "CR-01"],
            metrics={"criterion_id": "CR-01"},
            created_at=_NOW,
        ),
        EvidenceRecord(
            id="EV-bbbbbbbbbbbb",
            scope_id="P01-I01-W01",
            produced_by="agent",
            evidence_kind="jury",
            status="fail",
            summary="gate G-02 failed",
            refs=["G-02", "CR-02"],
            metrics={"criterion_id": "CR-02"},
            created_at=_NOW,
        ),
    )


# --------------------------------------------------------------------------
# Pure helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_build_evidence_ledger_one_row_per_criterion() -> None:
    """The ledger emits exactly one row per close-readiness criterion."""
    rows = build_evidence_ledger(_readiness(), _records())
    assert len(rows) == 3
    assert [row.criterion_id for row in rows] == ["CR-01", "CR-02", "CR-03"]


def test_build_evidence_ledger_carries_gate_status_and_producer() -> None:
    """Each row carries the rolled-up gate status + the joined producer."""
    rows = build_evidence_ledger(_readiness(), _records())
    by_id = {row.criterion_id: row for row in rows}
    assert by_id["CR-01"].gate_status == "pass"
    assert by_id["CR-01"].produced_by == "tool"
    assert by_id["CR-02"].gate_status == "fail"
    assert by_id["CR-02"].produced_by == "agent"


def test_build_evidence_ledger_unjoined_criterion_dashes_producer() -> None:
    """A criterion with no joined evidence + no gates dashes both cells."""
    rows = build_evidence_ledger(_readiness(), _records())
    cr03 = next(row for row in rows if row.criterion_id == "CR-03")
    assert cr03.gate_status == "-"
    assert cr03.produced_by == "-"
    assert cr03.status == "pending"


def test_build_evidence_ledger_no_records_dashes_all_producers() -> None:
    """With no evidence rows every produced_by cell shows a dash."""
    rows = build_evidence_ledger(_readiness())
    assert all(row.produced_by == "-" for row in rows)


def test_build_evidence_ledger_empty_view_yields_no_rows() -> None:
    """A view with no criteria yields no ledger rows (the boundary case)."""
    assert build_evidence_ledger(CloseReadiness(ready=True, criteria=[])) == ()


def test_gate_status_label_rolls_fail_over_blocked_over_pass() -> None:
    """The gate-status rollup prefers fail, then blocked, then pass."""
    failing = CriterionView(
        id="CR-9",
        source="spec",
        status="fail",
        gate_results=[
            GateResult(gate_id="G-a", status="pass"),
            GateResult(gate_id="G-b", status="blocked"),
            GateResult(gate_id="G-c", status="fail"),
        ],
    )
    blocked = CriterionView(
        id="CR-8",
        source="spec",
        status="blocked",
        gate_results=[
            GateResult(gate_id="G-a", status="pass"),
            GateResult(gate_id="G-b", status="blocked"),
        ],
    )
    assert gate_status_label(failing) == "fail"
    assert gate_status_label(blocked) == "blocked"


def test_gate_status_label_no_gates_dashes() -> None:
    """A criterion with no gate results dashes the gate cell."""
    legacy = CriterionView(id="CR-7", source="legacy", status="pending", gate_results=None)
    assert gate_status_label(legacy) == "-"


def test_ledger_row_is_frozen() -> None:
    """LedgerRow is a frozen value object."""
    row = LedgerRow(criterion_id="CR-01", gate_status="pass", produced_by="tool", status="pass")
    with pytest.raises((AttributeError, TypeError)):
        row.criterion_id = "CR-02"  # type: ignore[misc]


# --------------------------------------------------------------------------
# Snapshot: the ledger renders one row per criterion in the mode
# --------------------------------------------------------------------------


def test_evidence_ledger_snapshot() -> None:
    """The Evidence-mode ledger renders one row per typed criterion.

    Mounts the evidence screen, pushes a typed close-readiness view + evidence
    rows in via :meth:`EvidenceModeScreen.set_readiness`, and snapshots the
    frame so a layout regression on the ledger table is caught.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")  # -> evidence mode
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, EvidenceModeScreen)
            screen.set_readiness(_readiness(), _records())
            await settle_screen(pilot)
            ledger = screen.query_one("#evidence-ledger", DataTable)
            assert ledger.display is True
            assert ledger.row_count == 3
            frame = normalize_snapshot(capture_screen_text(app))
            # One row per criterion, with id / gate status / produced_by visible.
            assert "CR-01" in frame
            assert "CR-02" in frame
            assert "CR-03" in frame
            assert "produced_by" in frame
            assert "tool" in frame
            assert "agent" in frame
            assert_screen_snapshot(app, _GOLDEN / "evidence_ledger.txt")

    asyncio.run(body())
