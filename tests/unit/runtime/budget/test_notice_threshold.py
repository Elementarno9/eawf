"""One budget notice condition: a running Run reached its hard ceiling.

The rule is pinned at its source, the notice policy, and at the two producers
that call it: the token termination path and the daemon's wall-clock sweep. A
crossing of either hard ceiling opens at once, because the daemon stops the Run
there; nothing opens for a scope that is not a running Run. Rows
recorded under the retired estimate basis and approaching band still load for
display, a config still naming the retired fraction fails to load, and
replaying representative recorded rows through the rule opens nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.economics.governor import economics_policy_from
from eawf.kernel.economics.notice_policy import DEFAULT_NOTICE_POLICY, EstimatedTimeNotice
from eawf.kernel.runtime.budget_notice import BudgetNotice
from eawf.runtime.budget.notice_inbox import inbox_for
from eawf.runtime.budget.notices import (
    LOCAL_OPERATOR,
    BudgetCrossing,
    BudgetNoticeLedger,
    BudgetThresholdNotice,
    RecordedCrossing,
    apply_crossing,
    load_notice_ledger,
    notice_key_for,
    severity_for,
)
from eawf.runtime.budget.service import emit_termination_notice
from eawf.runtime.daemon.admission import EconomicsPolicyError, load_economics
from eawf.runtime.daemon.methods.run_liveness import detect_wall_ceiling_crossings
from tests.integration.runtime.daemon.test_run_stall_interval import RUN_KEY, RUN_URN
from tests.integration.runtime.daemon.test_run_wall_ceiling_notice import (
    STARTED,
    WALL,
    act,
    attached,
    bound_canary,
    daemon_for,
)

pytestmark = pytest.mark.unit

POLICY: Final = DEFAULT_NOTICE_POLICY.estimated_time
AT: Final = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
WAVE: Final = "P24-I02-W20"


# ---------- the rule ----------


def test_a_running_run_at_its_hard_ceiling_opens_at_once() -> None:
    assert POLICY.opens(running=True, ceiling_reached=True)


def test_a_non_running_scope_never_opens() -> None:
    assert not POLICY.opens(running=False, ceiling_reached=True)


def test_short_of_the_ceiling_never_opens() -> None:
    assert not POLICY.opens(running=True, ceiling_reached=False)


@pytest.mark.parametrize("value", [0, -1, "600", 600.0, None])
def test_the_grace_refuses_a_non_positive_or_non_integer_value(value: object) -> None:
    with pytest.raises(ValidationError):
        EstimatedTimeNotice.model_validate({"require_no_progress_for": value})


def test_a_crossing_admits_only_the_hard_limit_and_the_limit_band() -> None:
    fields: dict[str, Any] = {
        "scope_id": RUN_KEY,
        "basis": "hard_limit",
        "band": "limit_reached",
        "observed_value": 1,
        "budget_value": 1,
        "observed_at": AT,
    }
    assert BudgetCrossing.model_validate(fields).band == "limit_reached"
    with pytest.raises(ValidationError, match="basis"):
        BudgetCrossing.model_validate({**fields, "basis": "estimate"})
    with pytest.raises(ValidationError, match="band"):
        BudgetCrossing.model_validate({**fields, "band": "approaching"})
    with pytest.raises(ValidationError, match="observed_at"):
        BudgetCrossing.model_validate({key: v for key, v in fields.items() if key != "observed_at"})


# ---------- the rule wired at its producers ----------


def test_a_tokens_crossing_through_the_termination_path_opens_one_notice(tmp_path: Path) -> None:
    receipt = BudgetNotice.model_validate(
        {
            "run_ref": RUN_URN,
            "control_request_ref": "CTL-00000001",
            "observed_tokens": 1001,
            "cap_tokens": 1000,
            "noticed_at": AT.isoformat(),
        }
    )
    ledger = tmp_path / "budget_notices.json"

    result = emit_termination_notice(ledger, receipt, contract_digest=None, audience=("OP-0001",))

    assert result is not None
    assert (result.notice.axis, result.notice.basis, result.notice.highest_band) == (
        "tokens",
        "hard_limit",
        "limit_reached",
    )
    assert len(load_notice_ledger(ledger).notices) == 1


def test_the_wall_sweep_opens_at_the_ceiling_even_for_a_run_still_active(tmp_path: Path) -> None:
    canary = bound_canary(tmp_path / "repo")
    ctx = daemon_for(canary, tmp_path / "runtime")
    act(ctx, canary, 1)

    assert (
        detect_wall_ceiling_crossings(attached(ctx), now=STARTED + WALL - timedelta(seconds=1))
        == ()
    )
    assert detect_wall_ceiling_crossings(attached(ctx), now=STARTED + WALL) == (RUN_KEY,)


# ---------- legacy rows and the retired key ----------


def test_legacy_estimate_and_approaching_rows_still_load_for_display(tmp_path: Path) -> None:
    crossing = RecordedCrossing(
        scope_id=WAVE,
        axis="wall_seconds",
        basis="estimate",
        band="approaching",
        observed_value=1320,
        budget_value=1620,
        observed_at=AT,
        contract_digest=f"sha256:{'0' * 64}",
        audience=(LOCAL_OPERATOR,),
        provenance=("urn:p:1",),
    )
    imported, _upsert = apply_crossing(BudgetNoticeLedger(), crossing)
    path = tmp_path / "budget_notices.json"
    path.write_text(imported.model_dump_json(), encoding="utf-8")

    (notice,) = load_notice_ledger(path).notices.values()

    assert (notice.basis, notice.highest_band, notice.severity) == (
        "estimate",
        "approaching",
        "info",
    )
    inbox = inbox_for(load_notice_ledger(path), principal=LOCAL_OPERATOR, now=AT)
    assert [row.scope_id for row in inbox.active] == [WAVE]


@pytest.mark.parametrize("value", [1.0, "1.00", 0.8])
def test_a_policy_naming_notify_fraction_fails_with_the_removed_key(value: object) -> None:
    table = {"economics": {"notice_policy": {"estimated_time": {"notify_fraction": value}}}}
    with pytest.raises(ValidationError, match="notify_fraction was removed"):
        economics_policy_from(table)


def test_the_repository_loader_names_the_removed_key(tmp_path: Path) -> None:
    config = tmp_path / ".ea" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "economics:\n  notice_policy:\n    estimated_time:\n      notify_fraction: 1.0\n"
    )
    with pytest.raises(EconomicsPolicyError, match="notify_fraction was removed"):
        load_economics(tmp_path)


def test_an_empty_estimated_time_table_loads_the_default() -> None:
    table: dict[str, Any] = {"economics": {"notice_policy": {"estimated_time": {}}}}
    assert economics_policy_from(table).notice_policy == DEFAULT_NOTICE_POLICY


# ---------- replaying recorded notices ----------

#: Representative rows of a local notice ledger, stripped to the fields a row
#: carries: every recorded producer wrote the estimate basis, never a hard limit.
RECORDED: Final[tuple[dict[str, Any], ...]] = (
    {"scope_id": "RUN-00000155", "observed_value": 1440, "audience": ["OP-0001"]},
    {"scope_id": "RUN-00000322", "observed_value": 1455, "audience": ["OPERATOR"]},
    {"scope_id": "RUN-00000076", "observed_value": 65141, "audience": ["OP-0001"]},
    {"scope_id": "RUN-00000006", "observed_value": 244404, "audience": ["OPERATOR"]},
    {"scope_id": WAVE, "observed_value": 1334, "audience": ["OPERATOR"]},
)


def _recorded_ledger() -> BudgetNoticeLedger:
    """Return the representative rows as the stored ledger a reader loads."""
    rows: dict[str, dict[str, Any]] = {}
    for row in RECORDED:
        key = notice_key_for(scope_id=row["scope_id"], axis="wall_seconds", basis="estimate")
        rows[key] = {
            **row,
            "notice_key": key,
            "axis": "wall_seconds",
            "basis": "estimate",
            "highest_band": "limit_reached",
            "severity": severity_for("estimate", "limit_reached"),
            "budget_value": 1440,
            "opened_at": AT.isoformat(),
            "last_observed_at": AT.isoformat(),
        }
    return BudgetNoticeLedger.model_validate_json(json.dumps({"notices": rows}))


def _replay(notice: BudgetThresholdNotice, *, running: bool) -> bool:
    """Return whether *notice*'s recorded reading opens a notice under the one rule.

    An estimate was never a ceiling, so only a hard-limit row reached one.
    """
    reached = (
        notice.basis == "hard_limit"
        and notice.budget_value is not None
        and notice.observed_value >= notice.budget_value
    )
    return POLICY.opens(running=running, ceiling_reached=reached)


def test_replaying_recorded_notices_opens_none_for_an_epoch1_wave_scope() -> None:
    ledger = _recorded_ledger()
    assert len(ledger.notices) == len(RECORDED)

    as_wave = [_replay(notice, running=False) for notice in ledger.notices.values()]
    as_running_run = [_replay(notice, running=True) for notice in ledger.notices.values()]

    assert as_wave == [False] * len(RECORDED)
    assert as_running_run == [False] * len(RECORDED)
