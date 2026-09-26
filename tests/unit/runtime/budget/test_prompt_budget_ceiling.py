"""One prompt-budget ceiling: metered, noticed, enforced and shown at the same number.

The ``flow.budget`` table is the only place a ceiling is derived from. A
second ceiling declared beside it is refused rather than silently ignored,
the daemon's live accrual stops a hard-enforced wave at exactly the ceiling
the statusline draws, the notice opens there and never before it, and a
native Run metered past its sealed ceiling ends cancelled.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import EntityKind, parse_qualified_urn
from eawf.kernel.state.models import State
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.budget.notices import load_notice_ledger, notices_path
from eawf.runtime.budget.policy import (
    SEALED_BUDGET,
    BudgetConfig,
    DuplicateCeilingError,
    PromptBudgetCeiling,
    budget_config_from,
)
from eawf.runtime.budget.service import TerminationResult, load_budget_config
from eawf.runtime.daemon import dispatch_runner
from eawf.runtime.daemon.budget_interlock import (
    InFlightBudgetOutcome,
    InterlockOutcome,
    enforce_token_cap,
    guard_in_flight_budget,
)
from eawf.runtime.daemon.dispatch_runner import DispatchTokens, accrue_tokens_consumed
from eawf.runtime.daemon.methods import run_budget
from eawf.runtime.daemon.methods.run_budget import InFlightRunMeter
from eawf.runtime.runtimes.claude import statusline as statusline_orchestrator
from eawf.runtime.runtimes.claude.plugin_install import _patch_settings_json
from eawf.runtime.runtimes.claude.statusline_modules import budget as budget_module
from eawf.runtime.runtimes.metering import UsageSample
from eawf.surfaces.render.statusline import budget_segment, budget_unavailable_segment
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
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

pytestmark = pytest.mark.unit

#: The spent-over-limit shape the statusline draws, e.g. ``budget:1.5k/2.0k``.
SPENT_OVER_LIMIT: Final = r"^budget:\d+(\.\d)?[km]?/\d+(\.\d)?[km]?( !limit)?$"

RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's global config layer out of every resolution."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def write_repo(tmp_path: Path, *, budget: int | None, config: str | None = None) -> Path:
    """Write a repo whose active wave carries *budget*; return its state path."""
    payload = _state_payload()
    payload["waves"][_WAVE_ID]["token_budget"] = budget
    state_dir = tmp_path / "repo" / ".ea"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "state.json"
    state_path.write_text(State.model_validate(payload).model_dump_json(), encoding="utf-8")
    if config is not None:
        (state_dir / "config.yaml").write_text(config, encoding="utf-8")
    return state_path


def tokens(total: int) -> DispatchTokens:
    """Return a dispatch tally of *total* input tokens."""
    return DispatchTokens(
        input_tokens=total,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def fake_ladder(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the kill ladder under the accrual; return the pgids it was driven at."""
    reaped: list[int] = []

    def cancel(pgid: int) -> TerminationResult:
        reaped.append(pgid)
        return TerminationResult(
            sigterm_sent=True, sigkill_sent=False, exited_on_term=True, waited_seconds=0.0
        )

    def interlock(**kwargs: Any) -> InterlockOutcome:
        return enforce_token_cap(**kwargs, cancel=cancel)

    monkeypatch.setattr(dispatch_runner, "enforce_token_cap", interlock)
    return reaped


def render_budget(state_path: Path) -> str:
    """Render the budget segment the way the statusline orchestrator does."""
    return budget_module.build({}, state_path).text


# ---------- the one ceiling and its duplicate refusal ----------


def test_budget_config_from_second_ceiling_is_refused() -> None:
    merged = {"flow": {"budget": {"enforce": "hard", "multiplier": 1.5, "max_tokens": 5000}}}
    with pytest.raises(DuplicateCeilingError, match=r"duplicate_ceiling: flow\.budget\.max_tokens"):
        budget_config_from(merged)


@pytest.mark.parametrize("key", ["cap", "token_ceiling", "hard_limit", "warn_fraction"])
def test_budget_config_from_every_ceiling_spelling_is_refused(key: str) -> None:
    with pytest.raises(DuplicateCeilingError) as caught:
        budget_config_from({"flow": {"budget": {key: 1}}})
    assert caught.value.keys == [key]


def test_budget_config_from_unknown_non_ceiling_key_fails_validation() -> None:
    with pytest.raises(ValidationError):
        budget_config_from({"flow": {"budget": {"enforse": "hard"}}})


@pytest.mark.parametrize("merged", [{}, {"flow": {}}, {"flow": {"budget": None}}])
def test_budget_config_from_absent_table_is_default(merged: dict[str, Any]) -> None:
    assert budget_config_from(merged) == BudgetConfig()


@pytest.mark.parametrize("merged", [{"flow": 3}, {"flow": {"budget": "hard"}}])
def test_budget_config_from_non_mapping_raises(merged: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        budget_config_from(merged)


def test_budget_config_multiplier_below_one_is_refused() -> None:
    with pytest.raises(ValidationError):
        BudgetConfig(multiplier=0.99)


@pytest.mark.parametrize(
    ("budget", "multiplier", "tokens"), [(None, 1.5, None), (0, 1.5, 0), (1000, 1.0, 1000)]
)
def test_budget_config_ceiling_boundaries(
    budget: int | None, multiplier: float, tokens: int | None
) -> None:
    ceiling = BudgetConfig(multiplier=multiplier).ceiling(budget)
    assert (ceiling.tokens if ceiling is not None else None) == tokens


@pytest.mark.parametrize(("consumed", "reached"), [(0, False), (999, False), (1000, True)])
def test_prompt_budget_ceiling_reached_off_by_one(consumed: int, reached: bool) -> None:
    ceiling = PromptBudgetCeiling(tokens=1000, enforce="hard")
    assert ceiling.reached(consumed) is reached
    assert ceiling.decide(consumed).over_cap is reached


def test_prompt_budget_ceiling_refuses_negative_tokens() -> None:
    with pytest.raises(ValidationError):
        PromptBudgetCeiling(tokens=-1, enforce="soft")


def test_load_budget_config_reads_the_repo_layer(tmp_path: Path) -> None:
    state_path = write_repo(tmp_path, budget=1000, config="flow:\n  budget:\n    multiplier: 2.0\n")
    assert load_budget_config(state_path.parent.parent) == BudgetConfig(multiplier=2.0)


def test_load_budget_config_second_ceiling_in_a_layer_is_refused(tmp_path: Path) -> None:
    state_path = write_repo(tmp_path, budget=1000, config="flow:\n  budget:\n    cap: 900\n")
    with pytest.raises(DuplicateCeilingError):
        load_budget_config(state_path.parent.parent)


# ---------- the governor stops the metered work at the ceiling ----------


def test_accrue_tokens_consumed_stops_at_the_configured_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = write_repo(
        tmp_path, budget=1000, config="flow:\n  budget:\n    enforce: hard\n    multiplier: 2.0\n"
    )
    reaped = fake_ladder(monkeypatch)
    budget = load_budget_config(state_path.parent.parent)
    ctx = _ctx(state_path)

    below = accrue_tokens_consumed(
        ctx, wave_id=_WAVE_ID, tokens=tokens(1999), pgid=77, budget=budget
    )
    assert below is not None and not below.terminated
    assert not notices_path(state_path).exists()
    assert render_budget(state_path) == "budget:2.0k/2.0k"

    at = accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=tokens(1), pgid=77, budget=budget)
    assert at is not None and at.terminated
    assert reaped == [77]
    notice = next(iter(load_notice_ledger(notices_path(state_path)).notices.values()))
    assert (notice.highest_band, notice.basis, notice.budget_value) == (
        "limit_reached",
        "hard_limit",
        2000,
    )
    assert render_budget(state_path) == "budget:2.0k/2.0k !limit"


def test_accrue_tokens_consumed_soft_ceiling_notices_but_never_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = write_repo(tmp_path, budget=1000)
    reaped = fake_ladder(monkeypatch)
    outcome = accrue_tokens_consumed(
        _ctx(state_path), wave_id=_WAVE_ID, tokens=tokens(1500), pgid=77, budget=BudgetConfig()
    )
    assert outcome is not None and not outcome.terminated
    assert reaped == []
    notice = next(iter(load_notice_ledger(notices_path(state_path)).notices.values()))
    assert (notice.basis, notice.budget_value) == ("estimate", 1500)


@pytest.fixture
def canary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CanaryProvision:
    """A provisioned canary holding one running Run."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    provisioned = provision(tmp_path / "canary")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


def stored_run_status(canary: CanaryProvision) -> str:
    """Return the Run's status from the document or, once compacted, its ledger."""
    rows = read_document(document_path(canary)).get("run", {})
    if RUN_KEY in rows:
        return str(rows[RUN_KEY]["status"])
    ledger = ledger_path(document_path(canary), Epoch2Collection.RUN)
    for item in effective_records(read_ledger_records(ledger)):
        if item.record_key == RUN_KEY and "payload_kind" not in item.payload:
            return str(item.payload["status"])
    raise AssertionError(f"no run record keyed {RUN_KEY!r}")


def test_in_flight_run_meter_crossing_the_sealed_ceiling_cancels_the_run(
    tmp_path: Path, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    reaped: list[int] = []

    def cancel(pgid: int) -> TerminationResult:
        reaped.append(pgid)
        return TerminationResult(
            sigterm_sent=True, sigkill_sent=False, exited_on_term=True, waited_seconds=0.0
        )

    def guard(**kwargs: Any) -> InFlightBudgetOutcome:
        return guard_in_flight_budget(**kwargs, cancel=cancel)

    monkeypatch.setattr(run_budget, "guard_in_flight_budget", guard)
    meter = InFlightRunMeter(
        root_context(canary, tmp_path / "runtime"),
        urn=parse_qualified_urn(RUN_URN, expected_kind=EntityKind.RUN),
        actor="OP-0001",
        control_request_ref="CTL-0000000a",
        idempotency_key="budget-ceiling",
        cap_tokens=1000,
    )
    pgid = 4242
    under = UsageSample(input_tokens=100, output_tokens=899)
    assert asyncio.run(meter.observe(under, pgid)) is False
    assert stored_run_status(canary) == "RUNNING"
    crossing = UsageSample(input_tokens=100, output_tokens=900)
    assert asyncio.run(meter.observe(crossing, pgid)) is True
    assert stored_run_status(canary) == "CANCELLED"
    assert reaped == [pgid]
    sealed = SEALED_BUDGET.ceiling(1000)
    assert meter.outcome is not None and sealed is not None
    assert meter.outcome.decision.cap == sealed.tokens


# ---------- the statusline draws spent over limit ----------


def test_statusline_render_pipeline_shows_spent_over_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = write_repo(tmp_path, budget=40_000)
    monkeypatch.setattr(statusline_orchestrator, "_safe_resolve_state_path", lambda _w: state_path)
    line = statusline_orchestrator.render_pipeline({}, workspace=None, theme_name="ascii-fallback")
    assert "budget:0/60.0k" in line


@pytest.mark.parametrize(
    ("spent", "limit", "notice", "text"),
    [
        (0, 0, False, "budget:0/0"),
        (999, 1000, False, "budget:999/1.0k"),
        (1000, 1000, True, "budget:1.0k/1.0k !limit"),
        (2_500_000, 1_000_000, True, "budget:2.5m/1.0m !limit"),
    ],
)
def test_budget_segment_matches_spent_over_limit(
    spent: int, limit: int, notice: bool, text: str
) -> None:
    segment = budget_segment(spent=spent, limit=limit, notice_open=notice)
    assert segment.text == text
    assert segment.status == ("warn" if notice else "ok")
    assert re.match(SPENT_OVER_LIMIT, segment.text)


def test_budget_segment_negative_spend_raises() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        budget_segment(spent=-1, limit=10, notice_open=False)


@pytest.mark.parametrize("reason", ["", "two words"])
def test_budget_unavailable_segment_refuses_a_bad_reason(reason: str) -> None:
    with pytest.raises(ValueError, match="one non-empty token"):
        budget_unavailable_segment(reason)


def test_budget_module_names_why_it_cannot_draw(tmp_path: Path) -> None:
    assert budget_module.build({}, None).text == "budget:n/a(no-state)"
    assert render_budget(write_repo(tmp_path / "a", budget=None)) == "budget:n/a(no-budget)"
    duplicate = write_repo(tmp_path / "b", budget=10, config="flow:\n  budget:\n    cap: 5\n")
    assert render_budget(duplicate) == "budget:n/a(duplicate-ceiling)"
    corrupt = tmp_path / "c.json"
    corrupt.write_text("{", encoding="utf-8")
    assert render_budget(corrupt) == "budget:n/a(state-unreadable)"


def test_plugin_settings_wire_the_statusline_and_keep_a_foreign_one(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.json"
    rendered = _patch_settings_json(fresh, {})
    assert b'"command": "eawf cc statusline"' in rendered
    foreign = tmp_path / "foreign.json"
    foreign.write_text('{"statusLine": {"type": "command", "command": "mine"}}', encoding="utf-8")
    assert b'"command": "mine"' in _patch_settings_json(foreign, {})
