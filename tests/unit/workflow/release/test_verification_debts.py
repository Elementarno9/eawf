"""PLAN-006: verification debts are stored, and approval reads them back.

The collection keeps one row per debt revision; the reader returns the
newest row of each key, so a discharged debt stops blocking approval the
moment its discharge is recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.regime import (
    GateClass,
    VerificationDebt,
    VerificationDebtStatus,
    stable_release_blockers,
)
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.workflow.release.verification_debts import read_verification_debts

MILESTONE = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"
EVIDENCE = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _debt(key: str = "VDT-0001", **overrides: Any) -> VerificationDebt:
    fields: dict[str, Any] = {
        "key": key,
        "scope_ref": MILESTONE,
        "incident_ref": "INC-001",
        "deferred_gate": GateClass.REVIEW,
        "opened_at": T0,
    }
    fields.update(overrides)
    return VerificationDebt.model_validate(fields)


def _store(state_path: Path, debt: VerificationDebt, *, kind: StoreKind | None = None) -> None:
    append_envelope(
        store_path(state_path, StoreKind.VERIFICATION_DEBT),
        Envelope(
            id=f"{debt.key}@{debt.status.value}",
            kind=kind or StoreKind.VERIFICATION_DEBT,
            scope_id=debt.key,
            created_at=T0,
            summary=f"{debt.key} {debt.status.value}",
            payload=debt.model_dump(mode="json"),
        ),
    )


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    path = tmp_path / ".ea" / "state.json"
    path.parent.mkdir(parents=True)
    return path


def test_plan_006_no_collection_reads_as_no_debt(state_path: Path) -> None:
    assert read_verification_debts(state_path) == ()


def test_plan_006_a_stored_open_debt_reads_back_and_blocks(state_path: Path) -> None:
    _store(state_path, _debt())
    debts = read_verification_debts(state_path)
    assert [debt.key for debt in debts] == ["VDT-0001"]
    assert stable_release_blockers(debts) == ("VDT-0001",)


def test_plan_006_the_newest_revision_of_a_debt_wins(state_path: Path) -> None:
    opened = _debt()
    _store(state_path, opened)
    _store(
        state_path,
        opened.discharge(
            head="a" * 40,
            evidence_ref=parse_qualified_urn(EVIDENCE),
            at=T0 + timedelta(hours=1),
        ),
    )
    (debt,) = read_verification_debts(state_path)
    assert debt.status is VerificationDebtStatus.DISCHARGED
    assert stable_release_blockers((debt,)) == ()


def test_plan_006_each_key_keeps_its_own_row(state_path: Path) -> None:
    _store(state_path, _debt("VDT-0001"))
    _store(state_path, _debt("VDT-0002"))
    assert [debt.key for debt in read_verification_debts(state_path)] == ["VDT-0001", "VDT-0002"]


def test_plan_006_a_row_of_another_kind_refuses_the_read(state_path: Path) -> None:
    _store(state_path, _debt(), kind=StoreKind.EVIDENCE)
    with pytest.raises(ValueError, match="line 1 is not a debt"):
        read_verification_debts(state_path)


def test_plan_006_a_corrupt_line_refuses_rather_than_skips(state_path: Path) -> None:
    path = store_path(state_path, StoreKind.VERIFICATION_DEBT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="is not a debt"):
        read_verification_debts(state_path)
