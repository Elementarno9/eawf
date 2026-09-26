"""The canary reversible window closes on its journaled event, and seal faults surface.

An opted-in tree's canary window used to report ``open`` forever, because
the report never looked for the event that closes it. These cases drive a
real opted-in cutover through the production apply, then assert the window
reads ``open`` until a ``canary_window_closed`` row is journaled and
``closed`` after, so a report that hard-codes ``open`` fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.activation import (
    ActivationSealFault,
    RollbackRemedy,
    WindowState,
    read_seal_fault,
    rollback_boundary_report,
    seal_fault_path,
)
from eawf.kernel.migration.epoch2.canary import DisposableTarget
from eawf.kernel.migration.epoch2.journal import (
    CutoverJournal,
    CutoverStage,
    read_journal,
    require_chain_intact,
)
from eawf.kernel.migration.epoch2.manifest import RollbackBoundary
from eawf.workflow.evidence.migration_rehearsal import APPLY_JOURNAL_STAGES
from tests.integration.kernel.migration.test_epoch2_rollback import (
    applied_tree,
    mutate_natively,
    native_context,
    opted_in_applied,
)

pytestmark = pytest.mark.unit

CLOSED_AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def backup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the backup service's user home at this test's tmp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("EAWF_HOME", str(home))
    return home


def record_window_closed(target: DisposableTarget) -> None:
    """Journal the event that closes the canary reversible window."""
    journal = CutoverJournal(target.journal_path)
    journal.record(
        stage=CutoverStage.CANARY_WINDOW_CLOSED,
        boundary=RollbackBoundary.CROSSED,
        recorded_at=CLOSED_AT,
        detail="milestone MLS-0001 ran on epoch-2 authority and the re-run matched",
    )
    journal.flush()


def test_canary_window_stays_open_until_its_closing_event(
    tmp_path: Path, backup_home: Path
) -> None:
    target = opted_in_applied(tmp_path, home=backup_home)
    mutate_natively(native_context(target, tmp_path))

    window = rollback_boundary_report(target).canary_reversible_window

    assert window.state is WindowState.OPEN
    assert window.closed_at is None


def test_canary_window_closes_when_its_closing_event_is_journaled(
    tmp_path: Path, backup_home: Path
) -> None:
    target = opted_in_applied(tmp_path, home=backup_home)
    mutate_natively(native_context(target, tmp_path))
    record_window_closed(target)

    report = rollback_boundary_report(target)

    assert report.canary_reversible_window.state is WindowState.CLOSED
    assert report.canary_reversible_window.closed_at == CLOSED_AT
    assert report.canary_reversible_window.remedy is RollbackRemedy.FORWARD_REPAIR
    assert report.simple_rollback_window.state is WindowState.CLOSED


def test_closing_event_keeps_the_journal_chain_intact(tmp_path: Path, backup_home: Path) -> None:
    target = opted_in_applied(tmp_path, home=backup_home)
    mutate_natively(native_context(target, tmp_path))
    record_window_closed(target)

    rows = read_journal(target.journal_path)

    require_chain_intact(rows)
    assert rows[-1].stage is CutoverStage.CANARY_WINDOW_CLOSED


def test_disposable_tree_has_no_canary_window(tmp_path: Path) -> None:
    crashed = applied_tree(tmp_path)

    window = rollback_boundary_report(
        DisposableTarget.require(crashed.target_root)
    ).canary_reversible_window

    assert window.state is WindowState.NOT_APPLICABLE
    assert window.closed_at is None


def test_closing_stage_is_not_an_apply_stage() -> None:
    assert CutoverStage.CANARY_WINDOW_CLOSED.value not in APPLY_JOURNAL_STAGES


def test_report_names_a_recorded_seal_fault(tmp_path: Path, backup_home: Path) -> None:
    target = opted_in_applied(tmp_path, home=backup_home)
    assert rollback_boundary_report(target).seal_fault is None
    fault = ActivationSealFault(observed_at=CLOSED_AT, error="OSError")
    path = seal_fault_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fault.model_dump_json(), encoding="utf-8")

    assert rollback_boundary_report(target).seal_fault == fault


def test_read_seal_fault_absent_is_none(tmp_path: Path, backup_home: Path) -> None:
    assert read_seal_fault(opted_in_applied(tmp_path, home=backup_home)) is None


def test_read_seal_fault_rejects_a_malformed_record(tmp_path: Path, backup_home: Path) -> None:
    target = opted_in_applied(tmp_path, home=backup_home)
    path = seal_fault_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"observed_at": "2026-09-26T12:00:00Z"}', encoding="utf-8")

    with pytest.raises(ValueError, match="error"):
        read_seal_fault(target)
