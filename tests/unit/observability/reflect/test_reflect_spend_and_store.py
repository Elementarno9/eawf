"""Reflect spend, totals and stored reports keep one meaning per figure and file.

A Run that reported no usage is neither priced nor unpriced; cache tokens are the
prompt-cache reads whichever source counted them; a named ``.json`` output keeps the
text and the machine form apart; and a report stored before the schema-2 upgrade still
loads.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.measurement import (
    CaptureSource,
    CounterName,
    MeasuredRuntime,
    Observed,
    Unobserved,
)
from eawf.kernel.state.epoch2.run import Run
from eawf.observability.reflect.report import (
    FillManifest,
    ReflectReport,
    load_report,
    render_text,
    store_report,
)
from eawf.observability.reflect.runs import RunReading
from eawf.observability.reflect.totals import RunSpend, run_spend, scope_totals
from tests.contract.surfaces.cli._reflect_tree import run_row

_COUNTS = {
    CounterName.DURATION_MS: 1000,
    CounterName.INPUT_TOKENS: 10,
    CounterName.OUTPUT_TOKENS: 20,
    CounterName.CACHE_CREATION_INPUT_TOKENS: 300,
    CounterName.CACHE_READ_INPUT_TOKENS: 4000,
    CounterName.COST_USD: 0,
}


def _reading(key: str, **run_fields: Any) -> RunReading:
    return RunReading(
        run=Run.model_validate(run_row(key) | run_fields), events=(), canonical_sequence=1
    )


def _measured(counts: dict[CounterName, int]) -> dict[str, Any]:
    return MeasuredRuntime(
        source=CaptureSource.TRANSCRIPT,
        harness="claude-code",
        model="claude-opus-5-5",
        measurement_version=1,
        divisor=1,
        derived=False,
        measurement_quality="measured",
        counters={name: Observed(value=Decimal(value)) for name, value in counts.items()},
        spans=Unobserved(reason="no spans"),
    ).model_dump(mode="json")


def _report() -> ReflectReport:
    return ReflectReport(
        projects=("RFL",), projection_revision=3, title_fill="disabled", sessions=()
    )


def _manifest() -> FillManifest:
    return FillManifest(
        provider=None,
        local_only=True,
        sessions_listed=0,
        digests_sent=0,
        cache_hits=0,
        unknown_uncached=0,
        titles_by_source={},
    )


# ---- unpriced counts only Runs with tokens nobody priced ----------------------------


def test_totals_count_a_run_that_reported_no_usage_neither_priced_nor_unpriced() -> None:
    readings = [_reading("RUN-00000001"), _reading("RUN-00000002"), _reading("RUN-00000003")]
    spends = [
        RunSpend(cost_microusd=500, input_tokens=10),
        RunSpend(input_tokens=7),
        RunSpend(),
    ]

    (row,) = scope_totals(readings, spends)

    assert (row.priced_runs, row.unpriced_runs) == (1, 1)
    assert row.cost_microusd == 500


def test_totals_of_runs_that_reported_nothing_count_none_unpriced() -> None:
    """Boundary: one Run, no usage at all."""
    (row,) = scope_totals([_reading("RUN-00000001")], [RunSpend()])

    assert (row.priced_runs, row.unpriced_runs, row.cost_microusd) == (0, 0, None)


def test_totals_count_a_run_with_only_cache_tokens_unpriced() -> None:
    """Off by one: a single unpriced counter is enough to count the Run unpriced."""
    (row,) = scope_totals([_reading("RUN-00000001")], [RunSpend(cache_tokens=1)])

    assert row.unpriced_runs == 1


def test_totals_refuse_a_spend_list_of_another_length() -> None:
    """Error path: readings and spends are zipped strictly."""
    with pytest.raises(ValueError, match="zip"):
        scope_totals([_reading("RUN-00000001")], [])


# ---- cache tokens are prompt-cache reads from either source -------------------------


def test_a_captured_runs_cache_tokens_are_its_prompt_cache_reads() -> None:
    reading = _reading("RUN-00000001", captured_runtime=_measured(_COUNTS))

    spend = run_spend(reading)

    assert spend.cache_tokens == 4000
    assert (spend.input_tokens, spend.output_tokens) == (10, 20)


def test_a_captured_run_that_read_no_cache_states_zero_not_the_creation_count() -> None:
    """Boundary: cache creation alone adds no cache read."""
    counts = _COUNTS | {CounterName.CACHE_READ_INPUT_TOKENS: 0}
    spend = run_spend(_reading("RUN-00000001", captured_runtime=_measured(counts)))

    assert spend.cache_tokens == 0


# ---- a named output keeps the text and the machine form apart -----------------------


@pytest.mark.parametrize("name", ["report.json", "report.txt", "report"])
def test_a_named_output_keeps_the_text_and_the_machine_form_apart(
    name: str, tmp_path: Path
) -> None:
    out = tmp_path / name

    stored = store_report(tmp_path, _report(), _manifest(), on=date(2026, 10, 1), out=out)

    assert stored.text == out
    assert stored.document != stored.text
    assert stored.text.read_text(encoding="utf-8") == render_text(_report())
    assert load_report(stored.document) == _report()
    assert len({stored.text, stored.document, stored.manifest}) == 3


# ---- a report stored before schema 2 still loads ------------------------------------


def _schema_1(tmp_path: Path, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "projects": ["RFL"],
        "projection_revision": 7,
        "title_fill": "disabled",
        "sessions": [
            {
                "run_key": "RUN-00000001",
                "status": "completed",
                "runtime": "claude-code",
                "model_family": "opus",
                "wall_hours": "2",
                "model_steps": 3,
                "tool_work": 1,
                "subagents": 0,
                "title": None,
                "title_source": "unavailable",
                "quotable": True,
            }
        ],
    } | overrides
    path = tmp_path / "2026-10-01-reflect-report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_report_stored_at_schema_1_loads_as_schema_2(tmp_path: Path) -> None:
    report = load_report(_schema_1(tmp_path))

    assert report.schema_version == 2
    assert (report.projection_revision, report.totals, report.issues) == (7, (), ())
    assert report.sessions[0].cost_microusd is None


def test_a_stored_report_of_an_unknown_schema_is_refused(tmp_path: Path) -> None:
    """Error path: only the two shapes ever written are read."""
    with pytest.raises(ValidationError):
        load_report(_schema_1(tmp_path, schema_version=3))
