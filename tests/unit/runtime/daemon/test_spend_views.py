"""The spend folds and the budget lines at their edges.

The live suites drive both through real dispatches; these hold the cases a live run
rarely reaches: an empty ledger, a crossing no confirmed effect answers, a Run the tree
does not hold, a captured share with nothing observed, and every budget line's
unread, unlimited and exhausted forms.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from eawf.kernel.economics.governor import DEFAULT_GOVERNOR, RunReservation
from eawf.kernel.identity import EntityKind, parse_qualified_urn
from eawf.kernel.runtime.budget_notice import BudgetNotice
from eawf.kernel.runtime.usage import UsageQuality
from eawf.kernel.state.epoch2.measurement import (
    CaptureSource,
    CounterName,
    MeasuredRuntime,
    Observed,
    Unobserved,
)
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import spend
from eawf.runtime.daemon.run_capture_updates import captured_usage
from eawf.runtime.daemon.spend import cost_ceiling_view, run_usage_view
from eawf.surfaces.tui.console.renderers.budget_lines import (
    cost_line,
    money,
    time_line,
    tokens_line,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed_row

AT = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
RUN = seed_row("run", "RUNNING")
URN = parse_qualified_urn(RUN["urn"], expected_kind=EntityKind.RUN)


def _document() -> dict[str, Any]:
    return {Epoch2Collection.RUN.value: {RUN["key"]: RUN}}


def _notice() -> LedgerRecord:
    notice = BudgetNotice(
        run_ref=URN,
        control_request_ref="CTL-0000000a",
        cap_tokens=1_000,
        observed_tokens=1_050,
        noticed_at=AT,
    )
    return LedgerRecord(
        collection=Epoch2Collection.RUN,
        record_key="BGT-CTL-0000000a",
        status="noticed",
        recorded_at=AT,
        payload=notice.model_dump(mode="json"),
    )


def _measured(quality: UsageQuality, **values: Decimal | None) -> MeasuredRuntime:
    counters = {
        name: Observed(value=values[name.value])
        if values.get(name.value) is not None
        else Unobserved(reason="not reported")
        for name in CounterName
    }
    return MeasuredRuntime(
        source=CaptureSource.TRANSCRIPT,
        harness="claude-code",
        model="claude-opus-4-1",
        measurement_version=1,
        divisor=1,
        derived=quality == "estimated",
        measurement_quality=quality,
        reconstruction_basis=None if quality == "measured" else "recorded_in_transcript",
        counters=counters,
        spans=Unobserved(reason="none"),
    )


# ---- the ceiling fold -------------------------------------------------------


def test_ui_046_an_empty_ledger_holds_nothing_in_flight_and_lists_nothing() -> None:
    """Boundary: nothing in flight is a known zero; no stop and no spend is invented."""
    view = cost_ceiling_view({}, (), governor=DEFAULT_GOVERNOR)

    assert (view.live_runs, view.spent_tokens, view.held_tokens) == (0, 0, 0)
    assert view.ceiling_tokens == DEFAULT_GOVERNOR.max_in_flight_tokens
    assert view.ceiling_cost_microusd is None
    assert (view.stopped, view.providers) == ((), ())


def test_ui_046_a_crossing_no_confirmed_effect_answers_is_not_called_stopped() -> None:
    """A notice proves the crossing, not the reap: without an effect it is unconfirmed."""
    view = cost_ceiling_view(_document(), (_notice(),), governor=DEFAULT_GOVERNOR)

    (stop,) = view.stopped
    assert (stop.run_key, stop.confirmed, stop.basis) == (RUN["key"], False, "hard_limit")
    assert (stop.observed_tokens, stop.cap_tokens) == (1_050, 1_000)


@pytest.mark.parametrize(
    ("accrued", "spent", "pricing"),
    [
        ((), 0, "priced"),
        ((4_000,), 4_000, "priced"),
        ((4_000, None), 4_000, "partial"),
        ((None, None), None, "unmetered"),
    ],
    ids=["none-live", "one-priced", "one-unpriced", "all-unpriced"],
)
def test_ui_046_the_ceiling_spend_states_how_much_of_it_was_priced(
    monkeypatch: pytest.MonkeyPatch,
    accrued: tuple[int | None, ...],
    spent: int | None,
    pricing: str,
) -> None:
    """A sum over the priced Runs alone is a floor, and the view says so."""
    live = tuple(RunReservation(run_ref=URN, accrued_cost_microusd=cost) for cost in accrued)
    monkeypatch.setattr(spend, "in_flight_reservations", lambda *_, **__: live)

    view = cost_ceiling_view(_document(), (), governor=DEFAULT_GOVERNOR)

    assert (view.spent_cost_microusd, view.spent_cost_pricing) == (spent, pricing)


# ---- the Run fold -----------------------------------------------------------


def test_con_077_a_run_with_no_reading_and_no_binding_states_none_not_zero() -> None:
    view = run_usage_view(_document(), (), urn=URN)

    assert (view.tokens, view.cost_microusd, view.quality) == (None, None, None)
    assert (view.cap_tokens, view.wall_seconds, view.typical_seconds) == (None, None, None)
    assert view.typical_runs == 0


def test_con_077_a_run_the_tree_does_not_hold_is_refused() -> None:
    with pytest.raises(KeyError):
        run_usage_view({}, (), urn=URN)


# ---- the captured share as a reading -----------------------------------------


def test_prx_041_a_measured_share_is_a_running_total_in_micro_dollars() -> None:
    reading = captured_usage(
        _measured(
            "measured",
            input_tokens=Decimal(5),
            output_tokens=Decimal(30),
            cache_read_input_tokens=Decimal(7),
            cache_creation_input_tokens=Decimal(3),
            cost_usd=Decimal("0.0421"),
        )
    )

    assert reading is not None
    assert (reading.input_tokens, reading.output_tokens, reading.cache_tokens) == (5, 30, 10)
    assert reading.cost_microusd == 42_100
    assert reading.is_cumulative and reading.measurement_quality == "measured"


def test_prx_041_a_share_with_nothing_observed_states_no_reading() -> None:
    assert captured_usage(_measured("measured")) is None


def test_prx_041_an_estimated_share_still_covers_the_whole_run() -> None:
    reading = captured_usage(_measured("estimated", output_tokens=Decimal(9)))

    assert reading is not None
    assert (reading.measurement_quality, reading.coverage_fraction) == ("estimated", 1.0)


# ---- the budget lines --------------------------------------------------------


@pytest.mark.parametrize(
    ("spent", "limit", "held", "expected"),
    [
        (999, 1_000, None, "tokens 999 of 1,000 · ≈1 left"),
        (1_050, 1_000, None, "tokens 1,050 of 1,000 · ≈0 left"),
        (0, 4_000_000, 1_000, "tokens 0 of 4,000,000 · ≈3,999,000 left"),
        (None, 1_000, None, "tokens ∅ no reading yet of 1,000"),
        (12, None, None, "tokens 12 · no limit is set"),
    ],
    ids=["under", "over", "held", "unread", "unlimited"],
)
def test_con_077_a_token_budget_reads_spent_limit_and_an_estimated_remainder(
    spent: int | None, limit: int | None, held: int | None, expected: str
) -> None:
    assert tokens_line(spent, limit, held=held) == expected


def test_con_077_a_derived_spend_carries_the_approximate_marker() -> None:
    assert tokens_line(10, 20, estimated=True) == "tokens ~10 of 20 · ≈10 left"


def test_con_077_an_unpriced_cost_reads_unmetered_never_zero() -> None:
    assert cost_line(None, None) == "cost ∅ unmetered · no limit is set"
    assert cost_line(None, 20_000_000) == "cost ∅ unmetered of 20.00"
    assert cost_line(4_620_000, 20_000_000) == "cost 4.62 of 20.00 · ≈15.38 left"
    assert money(0) == "0.00"


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (20_000_000, "cost ≥4.62 of 20.00 · partly unmetered"),
        (None, "cost ≥4.62 · no limit is set · partly unmetered"),
    ],
    ids=["limited", "unlimited"],
)
def test_ui_046_a_partly_priced_cost_reads_as_a_floor_with_no_remainder(
    limit: int | None, expected: str
) -> None:
    assert cost_line(4_620_000, limit, partial=True) == expected


def test_con_077_a_time_budget_never_reads_a_remaining_time() -> None:
    assert time_line("elapsed 18m 04s", 3_600, 1_320) == "elapsed 18m 04s of 1h · typical ~22m"
    unseen = time_line("elapsed 4m", None, None)
    assert unseen == "elapsed 4m · typical ∅ no Run of this kind has completed"
    assert "left" not in unseen
