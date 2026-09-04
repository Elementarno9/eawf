"""Gate over the dev1 turn-cost baseline artifact.

The artifact is the first turn-cost measurement of this repository and the
frozen tolerance it is guarded by. Three things have to stay true about it,
and each is a test here.

The embedded record has to be a real record: it is parsed out of the
markdown and validated against :class:`TurnCostRecord`, whose
``extra="forbid"`` rejects a hand-edited field. It also has to still be the
measurement it claims to be, so the record is *recomputed* from the sample
roster the artifact publishes and compared field by field. A baseline that
only agrees with itself proves nothing; one that agrees with a re-run over
named rows is reproducible.

The threshold has to be the one frozen before the first measurement, and
the recorded baseline has to pass against the record it was taken from --
the library seam ``eawf bench turn-cost --check --baseline`` calls. The
live CLI path cannot stand in for it here: that path sources its corpus
from the telemetry cache, which holds no rows, so it refuses before any
comparison happens.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.state.models import Wave
from eawf.observability.bench.turn_cost import (
    TURN_COST_HARNESS_REVISION,
    TurnCostBaseline,
    TurnCostVerdict,
    compare_turn_cost,
)
from eawf.observability.telemetry.turn_cost import (
    CompletedUnitRun,
    PriceSource,
    TurnCostRecord,
    build_turn_cost_record,
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_ARTIFACT_PATH: Final[Path] = (
    _REPO_ROOT / ".ea" / "artifacts" / "research" / "2026-09-04-dev1-turn-cost-baseline.md"
)
_STATE_PATH: Final[Path] = _REPO_ROOT / ".ea" / "state.json"

_JSON_BLOCK: Final[re.Pattern[str]] = re.compile(r"```json\n(.*?)\n```", re.DOTALL)

_EU_MILLISECONDS: Final[Decimal] = Decimal(1_800_000)
"""Milliseconds in one effort unit at the 30-minute api-duration basis."""

_MICRO_USD: Final[Decimal] = Decimal("0.000001")
"""Quantum the float cost of a state row is rounded onto before summing."""

_PRICE_SOURCE: Final[PriceSource] = PriceSource(kind="session_rollup")


def _embedded_payloads() -> list[dict[str, Any]]:
    """Return every JSON object fenced into the baseline artifact."""
    text = _ARTIFACT_PATH.read_text(encoding="utf-8")
    return [json.loads(match.group(1)) for match in _JSON_BLOCK.finditer(text)]


def _one(payloads: list[dict[str, Any]], *, key: str) -> dict[str, Any]:
    """Return the single payload carrying *key*, failing when it is not unique."""
    matches = [payload for payload in payloads if key in payload]
    assert len(matches) == 1, f"want exactly one embedded payload with {key!r}, got {len(matches)}"
    return matches[0]


def _run_from_row(*, scope_id: str, wave: Wave, row: dict[str, Any]) -> CompletedUnitRun:
    """Return the completed-unit run one priced ``state.actuals`` row contributes."""
    api_duration_ms = Decimal(str(row["elapsed_eu"])) * _EU_MILLISECONDS
    return CompletedUnitRun(
        run_id=row["id"],
        wave_id=scope_id,
        role=wave.agent_role,
        runtime="claude",
        model=row["model"],
        wall_clock_ms=int(api_duration_ms.to_integral_value()),
        input_tokens=row["actual_tokens"],
        cost_usd=Decimal(str(row["actual_cost_usd"])).quantize(_MICRO_USD),
        price_source=_PRICE_SOURCE,
    )


def _recompute_record(sample: dict[str, Any]) -> TurnCostRecord:
    """Rebuild the record from the state rows the artifact's roster names."""
    state = json.loads(_STATE_PATH.read_bytes())
    waves: list[Wave] = []
    runs: list[CompletedUnitRun] = []
    for scope_id in sample["corpus_scope_ids"]:
        wave = Wave.model_validate(state["waves"][scope_id])
        waves.append(wave)
        runs.append(_run_from_row(scope_id=scope_id, wave=wave, row=state["actuals"][scope_id]))
    return build_turn_cost_record(
        waves=waves,
        runs=runs,
        fixture_id=sample["fixture_id"],
        harness_revision=TURN_COST_HARNESS_REVISION,
        runtime="claude",
        model=sample["model"],
    )


@pytest.fixture(scope="module")
def payloads() -> list[dict[str, Any]]:
    """Every JSON object the baseline artifact embeds."""
    return _embedded_payloads()


def test_dev1_baseline_artifact_record_and_sample_sizes(payloads: list[dict[str, Any]]) -> None:
    """The embedded record validates, matches the roster, and recomputes."""
    sample = _one(payloads, key="corpus_scope_ids")
    record = TurnCostRecord.model_validate(_one(payloads, key="unit_count"))

    assert record.fixture_id == sample["fixture_id"]
    assert record.harness_revision == TURN_COST_HARNESS_REVISION
    assert record.runtime == "claude"
    assert record.model == sample["model"]
    assert record.unit_count == len(sample["corpus_scope_ids"])

    assert sample["priced_row_count"] == 28
    assert sample["priced_row_count"] + sample["unpriced_row_count"] == sample["state_row_count"]
    assert record.unit_count + sample["unlabelled_priced_row_count"] == sample["priced_row_count"]
    assert sample["reasoning_summand_unsettled_row_count"] == 0
    assert record.reasoning_summand_unsettled is False
    assert record.reasoning_summand_unsettled_run_count == 0
    assert record.unattributed_run_count == 0
    assert record.unpriced_run_count == 0

    assert _recompute_record(sample) == record


def test_dev1_baseline_artifact_threshold_frozen(payloads: list[dict[str, Any]]) -> None:
    """The recorded tolerance is 0.20 and the record passes its own check."""
    record = TurnCostRecord.model_validate(_one(payloads, key="unit_count"))
    baseline = TurnCostBaseline.model_validate(_one(payloads, key="threshold"))

    assert baseline.threshold == Decimal("0.20")
    assert baseline.p90_cost_usd == record.p90_cost_usd
    assert baseline.p90_wall_clock_ms == record.p90_wall_clock_ms

    comparison = compare_turn_cost(baseline=baseline, record=record)
    assert comparison.verdict is TurnCostVerdict.OK
    assert comparison.mismatched_fields == ()
    assert comparison.threshold == Decimal("0.20")
    assert not comparison.cost_regressed
    assert not comparison.wall_clock_regressed


def test_dev1_baseline_reds_on_a_cost_rise_past_the_tolerance(
    payloads: list[dict[str, Any]],
) -> None:
    """A p90 cost inflated past 0.20 regresses, so the tolerance can fire."""
    record = TurnCostRecord.model_validate(_one(payloads, key="unit_count"))
    baseline = TurnCostBaseline.model_validate(_one(payloads, key="threshold"))
    inflated_p90 = record.p90_cost_usd * Decimal("1.21")
    payload = _one(payloads, key="unit_count").copy()
    payload["p90_cost_usd"] = str(inflated_p90)
    inflated = TurnCostRecord.model_validate(payload)

    comparison = compare_turn_cost(baseline=baseline, record=inflated)

    assert comparison.verdict is TurnCostVerdict.REGRESSED
    assert comparison.cost_regressed
    assert not comparison.wall_clock_regressed
