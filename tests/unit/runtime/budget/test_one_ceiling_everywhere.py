"""One ceiling at every budget boundary, and one notice row per crossing.

The wave claim gate (the library guard, the daemon mutation and the CLI
fallback), ``eawf wave budget consume`` and the daemon accrual all measure a
wave against the ceiling ``flow.budget`` derives, never against the raw
``token_budget``. Every producer that observes a crossing -- the consume
verb, the daemon accrual and a native Run's budget termination -- lands it
as the one row of the notice ledger, and a second observation of the same
crossing leaves that row alone.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import typer

from eawf.kernel.identity import EntityKind, parse_qualified_urn
from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.budget.notices import load_notice_ledger, notices_path
from eawf.runtime.budget.policy import BudgetAction, BudgetConfig
from eawf.runtime.budget.service import TerminationResult, consume_against_ceiling
from eawf.runtime.daemon.budget_interlock import InFlightBudgetOutcome, guard_in_flight_budget
from eawf.runtime.daemon.dispatch_runner import accrue_tokens_consumed
from eawf.runtime.daemon.methods import DaemonValidationError, run_budget
from eawf.runtime.daemon.methods.run_budget import InFlightRunMeter
from eawf.runtime.daemon.methods.state import mutate
from eawf.runtime.runtimes.metering import UsageSample
from eawf.surfaces.cli.commands.lifecycle_wave_read import wave_budget_consume_cmd
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.workflow.lifecycle._claim_guards import (
    CLAIM_BUDGET_CEILING_REACHED,
    validate_claim_budget,
)
from eawf.workflow.lifecycle._errors import LifecycleGuardError
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    provision,
    root_context,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_dispatch_cost_accrual import (
    _WAVE_ID,
    _ctx,
    _state_payload,
)
from tests.unit.runtime.budget.test_prompt_budget_ceiling import (
    RUN_KEY,
    RUN_URN,
    stored_run_status,
    tokens,
    write_repo,
)
from tests.unit.runtime.daemon.test_state_methods import (
    _build_ctx,
    _build_state_payload,
    _now,
)

pytestmark = pytest.mark.unit

HARD_EXACT = "flow:\n  budget:\n    enforce: hard\n    multiplier: 1.0\n"

CLAIM_WAVE = "P24-I01-W09"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's global config layer out of every resolution."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def wave_at(budget: int | None, consumed: int) -> Any:
    """Return the accrual fixture's wave carrying *budget* and *consumed*."""
    payload = _state_payload(tokens_consumed=consumed)
    payload["waves"][_WAVE_ID]["token_budget"] = budget
    return State.model_validate(payload).waves[_WAVE_ID]


def ledger_rows(state_path: Path) -> list[Any]:
    """Return every notice row beside *state_path*."""
    return list(load_notice_ledger(notices_path(state_path)).notices.values())


# ---------- the claim guard measures the ceiling ----------


def test_validate_claim_budget_no_budget_always_passes() -> None:
    validate_claim_budget(wave_at(None, 10**9), budget=BudgetConfig())


def test_validate_claim_budget_raw_budget_is_not_the_limit() -> None:
    validate_claim_budget(wave_at(1000, 1000), budget=BudgetConfig())


@pytest.mark.parametrize(("consumed", "refused"), [(1499, False), (1500, True), (1501, True)])
def test_validate_claim_budget_off_by_one_at_the_ceiling(consumed: int, refused: bool) -> None:
    wave = wave_at(1000, consumed)
    if not refused:
        validate_claim_budget(wave, budget=BudgetConfig())
        return
    with pytest.raises(LifecycleGuardError, match="over token budget") as caught:
        validate_claim_budget(wave, budget=BudgetConfig())
    assert caught.value.code == CLAIM_BUDGET_CEILING_REACHED
    assert "1500 ceiling" in caught.value.message


def test_validate_claim_budget_zero_budget_is_reached_at_zero() -> None:
    with pytest.raises(LifecycleGuardError):
        validate_claim_budget(wave_at(0, 0), budget=BudgetConfig())


def claimable(tmp_path: Path, *, consumed: int, config: str | None) -> Any:
    """Return a daemon context over a pending wave of budget 1000 at *consumed*."""
    payload = _build_state_payload(wave_status="pending")
    wave = cast("dict[str, Any]", payload["waves"])[CLAIM_WAVE]
    wave.update(
        {
            "effort_bucket": "M",
            "file_scopes": ["src/"],
            "token_budget": 1000,
            "tokens_consumed": consumed,
        }
    )
    payload["agent_sessions"]["SES-2"] = {  # type: ignore[index]
        "id": "SES-2",
        "role": "executor",
        "runtime": "test",
        "scope_id": CLAIM_WAVE,
        "status": "active",
        "started_at": _now().isoformat(),
    }
    payload["current"]["active_session_ids"] = ["SES-2"]  # type: ignore[index]
    ctx, _state, _events, _wal = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    if config is not None:
        (tmp_path / ".ea").mkdir(exist_ok=True)
        (tmp_path / ".ea" / "config.yaml").write_text(config, encoding="utf-8")
    return ctx


def claim(ctx: Any) -> dict[str, Any]:
    """Send one WAVE_CLAIM mutation through the daemon."""
    mutation = Mutation(
        kind=MutationKind.WAVE_CLAIM,
        scope_id=CLAIM_WAVE,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": CLAIM_WAVE, "session_id": "SES-2", "out_of_order": False},
    )

    async def send() -> dict[str, Any]:
        return await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})

    return asyncio.run(send())


def test_mutate_wave_claim_refuses_at_the_ceiling(tmp_path: Path) -> None:
    ctx = claimable(tmp_path, consumed=1500, config=None)
    with pytest.raises(DaemonValidationError, match=CLAIM_BUDGET_CEILING_REACHED):
        claim(ctx)


def test_mutate_wave_claim_measures_the_configured_ceiling(tmp_path: Path) -> None:
    ctx = claimable(tmp_path, consumed=1500, config="flow:\n  budget:\n    multiplier: 2.0\n")
    assert claim(ctx)["event"]["payload"]["event_kind"] == "wave_claimed"


def test_mutate_wave_claim_duplicate_ceiling_is_refused(tmp_path: Path) -> None:
    ctx = claimable(tmp_path, consumed=0, config="flow:\n  budget:\n    cap: 900\n")
    with pytest.raises(ValueError, match="duplicate_ceiling"):
        claim(ctx)


# ---------- consume decides against the ceiling ----------


def test_consume_against_ceiling_no_budget_continues() -> None:
    state = State.model_validate(_state_payload())
    outcome = consume_against_ceiling(state, _WAVE_ID, 10**6, budget=BudgetConfig())
    assert (outcome.ceiling, outcome.decision.action) == (None, BudgetAction.CONTINUE)


@pytest.mark.parametrize(
    ("delta", "enforce", "action"),
    [
        (0, "soft", BudgetAction.CONTINUE),
        (1499, "soft", BudgetAction.CONTINUE),
        (1500, "soft", BudgetAction.WARN),
        (1499, "hard", BudgetAction.CONTINUE),
        (1500, "hard", BudgetAction.HALT),
    ],
)
def test_consume_against_ceiling_boundaries(delta: int, enforce: Any, action: BudgetAction) -> None:
    payload = _state_payload()
    payload["waves"][_WAVE_ID]["token_budget"] = 1000
    state = State.model_validate(payload)
    outcome = consume_against_ceiling(state, _WAVE_ID, delta, budget=BudgetConfig(enforce=enforce))
    assert outcome.decision.action is action
    assert (outcome.tokens_before, outcome.wave.tokens_consumed) == (0, delta)
    assert outcome.decision.cap == 1500


def test_consume_against_ceiling_negative_tokens_raises() -> None:
    state = State.model_validate(_state_payload())
    with pytest.raises(ValueError, match="non-negative"):
        consume_against_ceiling(state, _WAVE_ID, -1, budget=BudgetConfig())


def test_consume_against_ceiling_unknown_wave_raises() -> None:
    state = State.model_validate(_state_payload())
    with pytest.raises(KeyError):
        consume_against_ceiling(state, "P99-I99-W99", 1, budget=BudgetConfig())


# ---------- the consume verb notices, and one crossing is one row ----------


def consume(state_path: Path, delta: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the ``wave budget consume`` handler against *state_path*."""
    monkeypatch.setenv("EA_STATE", str(state_path))
    ctx = cast("typer.Context", SimpleNamespace(obj=GlobalFlags(json_output=True)))
    wave_budget_consume_cmd(ctx, _WAVE_ID, delta)


def test_wave_budget_consume_below_the_ceiling_writes_no_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = write_repo(tmp_path, budget=1000)
    consume(state_path, 1000, monkeypatch)
    assert (
        State.model_validate_json(state_path.read_bytes()).waves[_WAVE_ID].tokens_consumed == 1000
    )
    assert ledger_rows(state_path) == []


def test_wave_budget_consume_crossing_is_one_row_across_producers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = write_repo(tmp_path, budget=1000)
    consume(state_path, 1500, monkeypatch)
    [notice] = ledger_rows(state_path)
    assert (notice.scope_id, notice.basis, notice.highest_band) == (
        _WAVE_ID,
        "estimate",
        "limit_reached",
    )
    assert (notice.observed_value, notice.budget_value, notice.revision) == (1500, 1500, 1)

    consume(state_path, 10, monkeypatch)
    accrue_tokens_consumed(
        _ctx(state_path), wave_id=_WAVE_ID, tokens=tokens(10), pgid=None, budget=BudgetConfig()
    )
    [again] = ledger_rows(state_path)
    assert again == notice


def test_wave_budget_consume_hard_ceiling_rolls_back_without_a_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = write_repo(tmp_path, budget=1000, config=HARD_EXACT)
    with pytest.raises(typer.Exit) as exited:
        consume(state_path, 1000, monkeypatch)
    assert exited.value.exit_code == 2
    assert State.model_validate_json(state_path.read_bytes()).waves[_WAVE_ID].tokens_consumed == 0
    assert ledger_rows(state_path) == []


# ---------- a Run's budget termination is the same one row ----------


@pytest.fixture
def canary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CanaryProvision:
    """A provisioned canary holding one running Run."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    provisioned = provision(tmp_path / "canary")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


def test_in_flight_run_meter_termination_is_one_notice_row(
    tmp_path: Path, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cancel(pgid: int) -> TerminationResult:
        return TerminationResult(
            sigterm_sent=True, sigkill_sent=False, exited_on_term=True, waited_seconds=0.0
        )

    def guard(**kwargs: Any) -> InFlightBudgetOutcome:
        return guard_in_flight_budget(**kwargs, cancel=cancel)

    monkeypatch.setattr(run_budget, "guard_in_flight_budget", guard)

    def meter() -> InFlightRunMeter:
        return InFlightRunMeter(
            root_context(canary, tmp_path / "runtime"),
            urn=parse_qualified_urn(RUN_URN, expected_kind=EntityKind.RUN),
            actor="OP-0001",
            control_request_ref="CTL-0000000b",
            idempotency_key="budget-one-row",
            cap_tokens=1000,
        )

    crossing = UsageSample(input_tokens=100, output_tokens=900)
    assert asyncio.run(meter().observe(crossing, 4242)) is True
    assert stored_run_status(canary) == "CANCELLED"
    state_path = canary.root / ".ea" / "state.json"
    [notice] = ledger_rows(state_path)
    assert (notice.scope_id, notice.basis, notice.severity) == (RUN_KEY, "hard_limit", "critical")
    assert (notice.observed_value, notice.budget_value) == (1000, 1000)

    asyncio.run(meter().observe(crossing, 4242))
    assert ledger_rows(state_path) == [notice]
