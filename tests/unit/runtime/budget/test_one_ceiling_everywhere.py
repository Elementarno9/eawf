"""One notice row per budget crossing.

A native Run's budget termination lands its crossing as the one row of the
notice ledger, and a second observation of the same crossing leaves that row
alone.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.identity import EntityKind, parse_qualified_urn
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.budget.notices import load_notice_ledger, notices_path
from eawf.runtime.budget.service import TerminationResult
from eawf.runtime.daemon.budget_interlock import InFlightBudgetOutcome, guard_in_flight_budget
from eawf.runtime.daemon.methods import run_budget
from eawf.runtime.daemon.methods.run_budget import InFlightRunMeter
from eawf.runtime.runtimes.metering import UsageSample
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    provision,
    root_context,
    seed,
    seed_row,
)
from tests.unit.runtime.budget.test_prompt_budget_ceiling import (
    RUN_KEY,
    RUN_URN,
    stored_run_status,
)

pytestmark = pytest.mark.unit

HARD_EXACT = "flow:\n  budget:\n    enforce: hard\n    multiplier: 1.0\n"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's global config layer out of every resolution."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def ledger_rows(state_path: Path) -> list[Any]:
    """Return every notice row beside *state_path*."""
    return list(load_notice_ledger(notices_path(state_path)).notices.values())


# ---------- consume decides against the ceiling ----------


# ---------- the consume verb notices, and one crossing is one row ----------


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
