"""A Run's budget notice is one row per Run, compiled contract and axis."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.runtime.budget.notices import (
    BudgetCrossing,
    BudgetNoticeLedger,
    BudgetThresholdNotice,
    UpsertOutcome,
    apply_crossing,
    load_notice_ledger,
    notice_key_for,
    upsert_notice,
)

AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
RUN: Final = "RUN-00000010"
CONTRACT: Final = f"sha256:{'a' * 64}"
RECOMPILED: Final = f"sha256:{'b' * 64}"


def crossing(**overrides: Any) -> BudgetCrossing:
    """Return a hard-limit crossing of the Run's token ceiling."""
    fields: dict[str, Any] = {
        "scope_id": RUN,
        "basis": "hard_limit",
        "band": "limit_reached",
        "observed_value": 1_050,
        "budget_value": 1_000,
        "observed_at": AT,
        "contract_digest": CONTRACT,
    }
    fields.update(overrides)
    return BudgetCrossing.model_validate(fields)


def test_run_025_the_contract_is_part_of_the_notice_identity() -> None:
    bound = notice_key_for(
        scope_id=RUN, axis="tokens", basis="hard_limit", contract_digest=CONTRACT
    )
    unbound = notice_key_for(scope_id=RUN, axis="tokens", basis="hard_limit")
    assert bound != unbound
    assert crossing().notice_key == bound


def test_run_025_a_scope_without_a_contract_keeps_its_earlier_identity() -> None:
    assert crossing(contract_digest=None).notice_key == notice_key_for(
        scope_id=RUN, axis="tokens", basis="hard_limit"
    )


def test_run_025_a_recompiled_contract_opens_its_own_notice() -> None:
    ledger, _ = apply_crossing(BudgetNoticeLedger(), crossing())
    ledger, result = apply_crossing(ledger, crossing(contract_digest=RECOMPILED))
    assert result.outcome is UpsertOutcome.CREATED
    assert len(ledger.notices) == 2


def test_run_025_an_identical_repeat_returns_the_standing_notice_without_writing() -> None:
    ledger, first = apply_crossing(BudgetNoticeLedger(), crossing())
    again, result = apply_crossing(ledger, crossing(observed_at=AT + timedelta(seconds=5)))
    assert result.outcome is UpsertOutcome.UNCHANGED
    assert again is ledger
    assert result.notice == first.notice


def test_run_025_a_late_lower_band_never_regresses_the_notice() -> None:
    ledger, _ = apply_crossing(BudgetNoticeLedger(), crossing())
    kept, result = apply_crossing(ledger, crossing(band="approaching", observed_value=900))
    assert result.outcome is UpsertOutcome.RETAINED
    assert kept.notices[crossing().notice_key].highest_band == "limit_reached"


def test_run_025_a_higher_band_increments_the_same_notice() -> None:
    ledger, _ = apply_crossing(
        BudgetNoticeLedger(), crossing(band="approaching", observed_value=900)
    )
    escalated, result = apply_crossing(ledger, crossing())
    assert result.outcome is UpsertOutcome.ESCALATED
    assert result.notice.revision == 2
    assert len(escalated.notices) == 1


def test_run_025_concurrent_producers_leave_one_row(tmp_path: Path) -> None:
    path = tmp_path / "local" / "budget_notices.json"
    path.parent.mkdir()
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: upsert_notice(path, crossing()).outcome, range(8)))
    assert outcomes.count(UpsertOutcome.CREATED) == 1
    assert len(load_notice_ledger(path).notices) == 1


def test_run_025_a_row_filed_under_another_contract_fails_validation() -> None:
    _, result = apply_crossing(BudgetNoticeLedger(), crossing())
    with pytest.raises(ValidationError, match="does not address this condition"):
        BudgetThresholdNotice.model_validate(
            {**result.notice.model_dump(mode="json"), "contract_digest": RECOMPILED}
        )


def test_run_026_a_run_notice_is_never_blocking() -> None:
    _, result = apply_crossing(BudgetNoticeLedger(), crossing())
    assert result.notice.blocking is False
    with pytest.raises(ValidationError):
        BudgetThresholdNotice.model_validate(
            {**result.notice.model_dump(mode="json"), "blocking": True}
        )
