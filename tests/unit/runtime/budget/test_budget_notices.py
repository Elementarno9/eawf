"""Budget crossings upsert one escalating notice and never become a hold.

Three properties carry the file. A budget event handed to an open
question, a pending action or a hold is refused with a message naming the
mistake, so a crossing can never be modelled as something that gates
work. Two identical crossings leave one notice row at one revision, even
when they race. Crossing the next band escalates that same row, and a
late lower band never regresses it.

The last section drives the production producer -- the daemon's live
token accrual -- against a real ``state.json`` under ``tmp_path``. It opens
a notice only once the one ceiling is reached, never on the way there.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.budget_signal import BUDGET_SIGNAL_FIELDS, refuse_budget_signal
from eawf.kernel.state.epoch2 import Hold
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.kernel.state.models import OpenQuestion, State
from eawf.runtime.budget.notices import (
    BudgetCrossing,
    BudgetNoticeLedger,
    BudgetThresholdNotice,
    UpsertOutcome,
    apply_crossing,
    load_notice_ledger,
    notice_key_for,
    notices_path,
    upsert_notice,
)
from eawf.runtime.budget.policy import PromptBudgetCeiling
from eawf.runtime.budget.service import emit_budget_notice
from eawf.runtime.daemon.dispatch_runner import DispatchTokens, accrue_tokens_consumed
from tests.integration.runtime.daemon.test_dispatch_cost_accrual import (
    _WAVE_ID,
    _ctx,
    _state_payload,
)

pytestmark = pytest.mark.unit

AT: Final = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
SCOPE: Final = "P35-I03-W06"
CEILING: Final = PromptBudgetCeiling(tokens=100, enforce="soft")


def crossing(**overrides: Any) -> BudgetCrossing:
    """Return one crossing of the approaching band, with *overrides* applied."""
    fields: dict[str, Any] = {
        "scope_id": SCOPE,
        "basis": "estimate",
        "band": "approaching",
        "observed_value": 80,
        "budget_value": 100,
        "observed_at": AT,
    }
    fields.update(overrides)
    return BudgetCrossing(**fields)


def question_fields() -> dict[str, Any]:
    """Return a valid open-question payload."""
    return {
        "id": "Q-0001",
        "scope_id": "CMP-0001",
        "title": "Which cache backend fits the replay path",
        "status": "open",
        "created_at": AT.isoformat(),
    }


def action_fields() -> dict[str, Any]:
    """Return a valid pending-action payload."""
    container = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
    return {
        "id": "ACT-0001",
        "kind": "operator_decision",
        "subject_ref": f"{container}/milestone/MLS-0030",
        "question": "Continue past the budget?",
        "options": [
            {"option_id": "go", "label": "Continue", "effect": "approve"},
            {"option_id": "stop", "label": "Stop", "effect": "decline"},
        ],
        "idempotency_key": "req-budget-0001",
        "status": "WAITING",
        "requested_by": {
            "principal_kind": "agent",
            "principal_id": "AG-0001",
            "run_ref": f"{container}/run/RUN-00000010",
        },
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
    }


def hold_fields() -> dict[str, Any]:
    """Return a valid hold payload."""
    return {
        "hold_id": "5b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "scope": {"kind": "track", "urn": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME"},
        "reason": {"code": "provider-outage", "message": "The provider stopped responding."},
        "created_by": {"principal_kind": "operator", "principal_id": "OP-0001"},
        "created_at": AT.isoformat(),
    }


# ---------- a budget event is never a question, an action or a hold ----------


def test_open_question_from_budget_event_is_refused() -> None:
    event = crossing().model_dump(mode="json")
    with pytest.raises(ValidationError, match="budget signal is never an open question"):
        OpenQuestion.model_validate({**question_fields(), **event})


def test_open_question_from_budget_event_instance_is_refused() -> None:
    with pytest.raises(ValidationError, match="budget signal is never an open question"):
        OpenQuestion.model_validate(crossing())


def test_pending_action_from_budget_event_is_refused() -> None:
    event = crossing(band="limit_reached", observed_value=100).model_dump(mode="json")
    with pytest.raises(ValidationError, match="budget signal is never a pending action"):
        PendingAction.model_validate({**action_fields(), **event})


def test_pending_action_from_budget_notice_is_refused() -> None:
    _, result = apply_crossing(BudgetNoticeLedger(), crossing())
    with pytest.raises(ValidationError, match="budget signal is never a pending action"):
        PendingAction.model_validate(result.notice)


def test_hold_from_budget_event_is_refused() -> None:
    event = crossing().model_dump(mode="json")
    with pytest.raises(ValidationError, match="budget signal is never a hold"):
        Hold.model_validate({**hold_fields(), **event})


def test_lifecycle_records_without_budget_fields_still_validate() -> None:
    assert OpenQuestion.model_validate(question_fields()).id == "Q-0001"
    assert PendingAction.model_validate(action_fields()).id == "ACT-0001"
    assert Hold.model_validate(hold_fields()).released_at is None


@pytest.mark.parametrize("model", [BudgetCrossing, BudgetThresholdNotice])
def test_refuse_budget_signal_covers_every_budget_record(model: type) -> None:
    assert set(model.model_fields) & BUDGET_SIGNAL_FIELDS


@pytest.mark.parametrize("data", [None, "band", 42, [], {}, {1: "x"}])
def test_refuse_budget_signal_ignores_non_budget_input(data: object) -> None:
    refuse_budget_signal(data, target="a hold")


def test_refuse_budget_signal_names_a_single_field() -> None:
    with pytest.raises(ValueError, match="budget fields: band"):
        refuse_budget_signal({"band": "approaching"}, target="a hold")


# ---------- the notice record ----------


def test_budget_threshold_notice_blocking_cannot_be_set() -> None:
    _, result = apply_crossing(BudgetNoticeLedger(), crossing())
    with pytest.raises(ValidationError):
        BudgetThresholdNotice.model_validate({**result.notice.model_dump(), "blocking": True})


def test_budget_threshold_notice_refuses_underived_severity() -> None:
    _, result = apply_crossing(BudgetNoticeLedger(), crossing())
    with pytest.raises(ValidationError, match="not derived"):
        BudgetThresholdNotice.model_validate({**result.notice.model_dump(), "severity": "critical"})


def test_budget_threshold_notice_refuses_foreign_key() -> None:
    _, result = apply_crossing(BudgetNoticeLedger(), crossing())
    foreign = notice_key_for(scope_id="P01-I01-W01", axis="tokens", basis="estimate")
    with pytest.raises(ValidationError, match="does not address"):
        BudgetThresholdNotice.model_validate({**result.notice.model_dump(), "notice_key": foreign})


def test_budget_threshold_notice_refuses_resolved_at_while_open() -> None:
    _, result = apply_crossing(BudgetNoticeLedger(), crossing())
    with pytest.raises(ValidationError, match="resolved_at"):
        BudgetThresholdNotice.model_validate({**result.notice.model_dump(), "resolved_at": AT})


def test_budget_crossing_refuses_negative_observation() -> None:
    with pytest.raises(ValidationError):
        crossing(observed_value=-1)


def test_budget_crossing_refuses_unknown_band() -> None:
    with pytest.raises(ValidationError):
        crossing(band="exhausted")


def test_budget_crossing_refuses_empty_scope() -> None:
    with pytest.raises(ValidationError):
        crossing(scope_id="")


def test_budget_notice_ledger_refuses_misfiled_row() -> None:
    _, result = apply_crossing(BudgetNoticeLedger(), crossing())
    with pytest.raises(ValidationError, match="filed under"):
        BudgetNoticeLedger(notices={"sha256:" + "0" * 64: result.notice})


def test_notice_key_for_ignores_band_but_not_basis() -> None:
    assert crossing().notice_key == crossing(band="limit_reached").notice_key
    assert crossing().notice_key != crossing(basis="hard_limit").notice_key
    assert crossing().notice_key != crossing(scope_id="P35-I03-W07").notice_key


# ---------- upsert, dedupe and escalation ----------


def test_upsert_notice_two_identical_crossings_leave_one_row(tmp_path: Path) -> None:
    path = tmp_path / "budget_notices.json"
    first = upsert_notice(path, crossing())
    second = upsert_notice(path, crossing(observed_at=AT + timedelta(seconds=5)))
    assert first.outcome is UpsertOutcome.CREATED
    assert second.outcome is UpsertOutcome.UNCHANGED
    notices = load_notice_ledger(path).notices
    assert list(notices) == [crossing().notice_key]
    assert notices[crossing().notice_key].revision == 1


def test_upsert_notice_same_band_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "budget_notices.json"
    upsert_notice(path, crossing())
    before = path.read_bytes()
    upsert_notice(path, crossing(observed_value=90, observed_at=AT + timedelta(seconds=5)))
    assert path.read_bytes() == before


def test_upsert_notice_next_threshold_escalates_same_row(tmp_path: Path) -> None:
    path = tmp_path / "budget_notices.json"
    upsert_notice(path, crossing())
    later = AT + timedelta(minutes=1)
    result = upsert_notice(
        path, crossing(band="limit_reached", observed_value=101, observed_at=later)
    )
    assert result.outcome is UpsertOutcome.ESCALATED
    notices = load_notice_ledger(path).notices
    assert len(notices) == 1
    notice = notices[crossing().notice_key]
    assert notice.revision == 2
    assert notice.highest_band == "limit_reached"
    assert notice.severity == "warning"
    assert notice.observed_value == 101
    assert notice.opened_at == AT
    assert notice.last_observed_at == later
    assert notice.status == "OPEN"
    assert notice.blocking is False


def test_upsert_notice_late_lower_band_never_regresses(tmp_path: Path) -> None:
    path = tmp_path / "budget_notices.json"
    upsert_notice(path, crossing(band="limit_reached", observed_value=100))
    result = upsert_notice(path, crossing(band="approaching", observed_value=80))
    assert result.outcome is UpsertOutcome.RETAINED
    notice = load_notice_ledger(path).notices[crossing().notice_key]
    assert notice.highest_band == "limit_reached"
    assert notice.revision == 1


def test_apply_crossing_terminal_notice_is_never_reopened() -> None:
    ledger, created = apply_crossing(BudgetNoticeLedger(), crossing())
    resolved = created.notice.model_copy(update={"status": "RESOLVED", "resolved_at": AT})
    ledger = BudgetNoticeLedger(notices={resolved.notice_key: resolved})
    after, result = apply_crossing(ledger, crossing(band="limit_reached"))
    assert result.outcome is UpsertOutcome.RETAINED
    assert after is ledger


def test_apply_crossing_hard_limit_severity_is_critical_at_limit() -> None:
    _, result = apply_crossing(
        BudgetNoticeLedger(), crossing(basis="hard_limit", band="limit_reached")
    )
    assert result.notice.severity == "critical"


def test_upsert_notice_concurrent_producers_create_one_row(tmp_path: Path) -> None:
    path = tmp_path / "budget_notices.json"
    outcomes: list[UpsertOutcome] = []
    barrier = threading.Barrier(8)

    def produce() -> None:
        barrier.wait()
        outcomes.append(upsert_notice(path, crossing()).outcome)

    threads = [threading.Thread(target=produce) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count(UpsertOutcome.CREATED) == 1
    assert outcomes.count(UpsertOutcome.UNCHANGED) == 7
    assert load_notice_ledger(path).notices[crossing().notice_key].revision == 1


def test_load_notice_ledger_absent_file_is_empty(tmp_path: Path) -> None:
    assert load_notice_ledger(tmp_path / "missing.json").notices == {}


def test_load_notice_ledger_corrupt_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "budget_notices.json"
    path.write_text('{"notices": {}, "extra": 1}', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_notice_ledger(path)


def test_notices_path_sits_under_local_beside_state(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    assert notices_path(state_path) == tmp_path / ".ea" / "local" / "budget_notices.json"


# ---------- the producer ----------


@pytest.mark.parametrize(
    ("consumed", "opened"), [(0, False), (75, False), (99, False), (100, True)]
)
def test_emit_budget_notice_opens_only_at_the_ceiling(
    tmp_path: Path, consumed: int, opened: bool
) -> None:
    path = tmp_path / "budget_notices.json"
    result = emit_budget_notice(
        path, scope_id=SCOPE, consumed=consumed, ceiling=CEILING, observed_at=AT
    )
    if not opened:
        assert result is None
        assert not path.exists()
    else:
        assert result is not None
        assert (result.notice.highest_band, result.notice.basis) == ("limit_reached", "estimate")


def test_emit_budget_notice_hard_ceiling_is_a_hard_limit(tmp_path: Path) -> None:
    result = emit_budget_notice(
        tmp_path / "budget_notices.json",
        scope_id=SCOPE,
        consumed=100,
        ceiling=PromptBudgetCeiling(tokens=100, enforce="hard"),
        observed_at=AT,
    )
    assert result is not None
    assert (result.notice.basis, result.notice.severity) == ("hard_limit", "critical")


def test_emit_budget_notice_no_ceiling_records_nothing(tmp_path: Path) -> None:
    path = tmp_path / "budget_notices.json"
    result = emit_budget_notice(path, scope_id=SCOPE, consumed=10**9, ceiling=None, observed_at=AT)
    assert result is None
    assert not path.exists()


def test_emit_budget_notice_negative_consumption_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        emit_budget_notice(
            tmp_path / "n.json", scope_id=SCOPE, consumed=-1, ceiling=CEILING, observed_at=AT
        )


def test_emit_budget_notice_corrupt_ledger_degrades_to_none(tmp_path: Path) -> None:
    path = tmp_path / "budget_notices.json"
    path.write_text("not json", encoding="utf-8")
    result = emit_budget_notice(path, scope_id=SCOPE, consumed=100, ceiling=CEILING, observed_at=AT)
    assert result is None


def _write_budgeted_state(tmp_path: Path, *, budget: int) -> Path:
    payload = _state_payload()
    payload["waves"][_WAVE_ID]["token_budget"] = budget
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(State.model_validate(payload).model_dump_json(), encoding="utf-8")
    return path


def _tokens(total: int) -> DispatchTokens:
    return DispatchTokens(
        input_tokens=total,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def test_accrue_tokens_consumed_opens_one_notice_at_the_ceiling(tmp_path: Path) -> None:
    state_path = _write_budgeted_state(tmp_path, budget=1000)
    ctx = _ctx(state_path)
    accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=_tokens(1400))
    assert not notices_path(state_path).exists()
    accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=_tokens(100))
    accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=_tokens(200))
    ledger = load_notice_ledger(notices_path(state_path))
    assert len(ledger.notices) == 1
    notice = next(iter(ledger.notices.values()))
    assert (notice.scope_id, notice.highest_band, notice.revision, notice.budget_value) == (
        _WAVE_ID,
        "limit_reached",
        1,
        1500,
    )
    wave = State.model_validate_json(state_path.read_bytes()).waves[_WAVE_ID]
    assert wave.status == "in_progress"


def test_accrue_tokens_consumed_below_ceiling_writes_no_notice(tmp_path: Path) -> None:
    state_path = _write_budgeted_state(tmp_path, budget=1000)
    accrue_tokens_consumed(_ctx(state_path), wave_id=_WAVE_ID, tokens=_tokens(1499))
    assert not notices_path(state_path).exists()
