"""The capture producer reads a Run's counters only through its vendor session.

Each test writes a Claude transcript or a statusline counter sidecar under
a temporary root, points the producer at it through the environment, and
reads the Run's start and stop the way the daemon does.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.measurement import (
    UNKNOWN_ATTRIBUTION,
    CaptureSource,
    CounterName,
    CounterSnapshot,
    ExcludedRuntime,
    ExclusionReason,
    MeasuredRuntime,
    Observed,
    SpanSummary,
    UncapturedReason,
    UncapturedRuntime,
    Unobserved,
    VendorSessionRef,
    measure_run,
)
from eawf.kernel.state.epoch2.run import Run
from eawf.observability.measurement.capture import capture_run_start, capture_run_terminal
from eawf.runtime.runtime_counter_sidecar import RuntimeCounterSidecar
from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters
from eawf.runtime.runtimes.claude.transcript_counters import MEASURE_VERSION
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed_row

SESSION = "5f0c6a8e-1111-4222-8333-944455556666"
OTHER = "0a1b2c3d-1111-4222-8333-944455556666"
MODEL = "claude-opus-4-1"
T0 = datetime(2026, 9, 8, 1, 0, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _stamp(seconds: float) -> str:
    return _at(seconds).isoformat().replace("+00:00", "Z")


def _message(second: float, ident: str, *, output: int, model: str = MODEL) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": _stamp(second),
        "message": {
            "id": ident,
            "model": model,
            "usage": {
                "input_tokens": 10,
                "output_tokens": output,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "content": [{"type": "text", "text": "x"}],
        },
    }


def _turn(second: float, duration_ms: int) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": duration_ms,
        "timestamp": _stamp(second),
    }


def _prompt(second: float) -> dict[str, Any]:
    return {"type": "user", "timestamp": _stamp(second), "message": {"content": "go"}}


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point the transcript and sidecar lookups at empty temporary roots."""
    projects = tmp_path / "projects"
    cache = tmp_path / "statusline-cache"
    (projects / "-repo").mkdir(parents=True)
    cache.mkdir()
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(cache))
    return projects, cache


def _transcript(
    roots: tuple[Path, Path], rows: list[dict[str, Any]], *, session: str = SESSION
) -> Path:
    path = roots[0] / "-repo" / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _sidecar(
    roots: tuple[Path, Path], counters: RuntimeCounters, *, session: str = SESSION
) -> None:
    RuntimeCounterSidecar(roots[1] / f"{session}.runtime-counters.json").write(counters)


def _ref(session: str = SESSION) -> VendorSessionRef:
    return VendorSessionRef(harness="claude-code", session_digest=session)


def _observed(snapshot: CounterSnapshot | MeasuredRuntime, name: CounterName) -> Decimal:
    reading = snapshot.counters[name]
    assert isinstance(reading, Observed)
    return reading.value


# ---- MEAS-000: the typed vendor session reference ---------------------------


def test_meas_000_vendor_session_ref_hashes_the_raw_id() -> None:
    ref = _ref()

    assert ref.session_digest == hash_vendor_session_id(SESSION)
    assert SESSION not in ref.model_dump_json()


def test_meas_000_vendor_session_ref_refuses_empty_id_and_bad_harness() -> None:
    with pytest.raises(ValidationError):
        VendorSessionRef(harness="claude-code", session_digest="")
    with pytest.raises(ValidationError):
        VendorSessionRef(harness="Claude Code", session_digest=SESSION)


def test_meas_000_run_carries_vendor_session_distinct_from_its_key() -> None:
    fields = Run.model_fields

    assert "vendor_session" in fields
    assert fields["vendor_session"].annotation == VendorSessionRef | None


def test_meas_000_capture_reads_only_the_referenced_session(roots: tuple[Path, Path]) -> None:
    _transcript(roots, [_message(1, "m", output=99)], session=OTHER)

    assert capture_run_start(_ref(), at=_at(5), concurrent_run_count=1) == UncapturedRuntime(
        reason=UncapturedReason.NO_SOURCE
    )


def test_meas_000_run_without_vendor_session_records_that(roots: tuple[Path, Path]) -> None:
    start = capture_run_start(None, at=_at(0), concurrent_run_count=1)
    stop = capture_run_terminal(None, baseline=start, started_at=_at(0), at=_at(9))

    assert start == UncapturedRuntime(reason=UncapturedReason.NO_VENDOR_SESSION)
    assert stop == UncapturedRuntime(reason=UncapturedReason.NO_VENDOR_SESSION)


def test_meas_000_harness_without_a_reader_has_no_source(roots: tuple[Path, Path]) -> None:
    _transcript(roots, [_message(1, "m", output=5)])
    ref = VendorSessionRef(harness="codex", session_digest=SESSION)

    assert capture_run_start(ref, at=_at(5), concurrent_run_count=1) == UncapturedRuntime(
        reason=UncapturedReason.NO_SOURCE
    )


# ---- MEAS-001: start and terminal snapshots, delta against the baseline ------


def test_meas_001_terminal_records_delta_against_start_baseline(roots: tuple[Path, Path]) -> None:
    path = _transcript(roots, [_prompt(0), _message(1, "m1", output=100), _turn(2, 2000)])
    baseline = capture_run_start(_ref(), at=_at(3), concurrent_run_count=1)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    _transcript(roots, [*rows, _prompt(4), _message(6, "m2", output=30), _turn(7, 3000)])

    captured = capture_run_terminal(_ref(), baseline=baseline, started_at=_at(3), at=_at(8))

    assert isinstance(baseline, CounterSnapshot)
    assert _observed(baseline, CounterName.OUTPUT_TOKENS) == 100
    assert isinstance(captured, MeasuredRuntime)
    assert _observed(captured, CounterName.OUTPUT_TOKENS) == 30
    assert _observed(captured, CounterName.DURATION_MS) == 3000
    assert _observed(captured, CounterName.INPUT_TOKENS) == 10


def test_meas_001_measured_row_is_typed_apart_from_a_cumulative_snapshot() -> None:
    assert "captured_at" in CounterSnapshot.model_fields
    assert "captured_at" not in MeasuredRuntime.model_fields
    with pytest.raises(ValidationError):
        MeasuredRuntime.model_validate({"outcome": "measured", "captured_at": _at(0)})


def test_meas_001_run_refuses_baseline_before_start_and_capture_before_stop() -> None:
    uncaptured = {"outcome": "uncaptured", "reason": "no_source"}

    with pytest.raises(ValidationError, match="QUEUED"):
        Run.model_validate({**seed_row("run", "QUEUED"), "counter_baseline": uncaptured})
    with pytest.raises(ValidationError, match="stopped Run"):
        Run.model_validate({**seed_row("run", "RUNNING"), "captured_runtime": uncaptured})
    running = Run.model_validate({**seed_row("run", "RUNNING"), "counter_baseline": uncaptured})
    assert running.counter_baseline == UncapturedRuntime(reason=UncapturedReason.NO_SOURCE)


# ---- MEAS-002: transcript first, sidecar otherwise, source recorded ----------


def test_meas_002_transcript_preferred_over_sidecar(roots: tuple[Path, Path]) -> None:
    _transcript(roots, [_message(1, "m", output=4)])
    _sidecar(roots, RuntimeCounters(output_tokens=999, measure_version=101))

    snapshot = capture_run_start(_ref(), at=_at(5), concurrent_run_count=1)

    assert isinstance(snapshot, CounterSnapshot)
    assert snapshot.source is CaptureSource.TRANSCRIPT
    assert snapshot.measurement_version == MEASURE_VERSION


def test_meas_002_sidecar_read_when_no_transcript(roots: tuple[Path, Path]) -> None:
    _sidecar(roots, RuntimeCounters(output_tokens=7, measure_version=101, harness="claude-code"))

    snapshot = capture_run_start(_ref(), at=_at(5), concurrent_run_count=1)

    assert isinstance(snapshot, CounterSnapshot)
    assert snapshot.source is CaptureSource.SIDECAR
    assert _observed(snapshot, CounterName.OUTPUT_TOKENS) == 7


def test_meas_002_sidecar_without_measure_version_is_no_source(roots: tuple[Path, Path]) -> None:
    _sidecar(roots, RuntimeCounters(output_tokens=7))

    assert capture_run_start(_ref(), at=_at(5), concurrent_run_count=1) == UncapturedRuntime(
        reason=UncapturedReason.NO_SOURCE
    )


# ---- MEAS-003: neither source records no captured runtime --------------------


def test_meas_003_neither_source_records_no_captured_runtime(roots: tuple[Path, Path]) -> None:
    start = capture_run_start(_ref(), at=_at(0), concurrent_run_count=1)
    stop = capture_run_terminal(_ref(), baseline=start, started_at=_at(0), at=_at(5))

    assert start == UncapturedRuntime(reason=UncapturedReason.NO_SOURCE)
    assert stop == UncapturedRuntime(reason=UncapturedReason.NO_BASELINE)


def test_meas_003_absent_baseline_is_never_differenced_as_zero(roots: tuple[Path, Path]) -> None:
    _transcript(roots, [_message(1, "m", output=50)])

    stop = capture_run_terminal(_ref(), baseline=None, started_at=_at(0), at=_at(5))

    assert stop == UncapturedRuntime(reason=UncapturedReason.NO_BASELINE)


def test_meas_003_source_lost_at_stop_records_no_source(roots: tuple[Path, Path]) -> None:
    path = _transcript(roots, [_message(1, "m", output=50)])
    baseline = capture_run_start(_ref(), at=_at(2), concurrent_run_count=1)
    path.unlink()

    stop = capture_run_terminal(_ref(), baseline=baseline, started_at=_at(2), at=_at(5))

    assert stop == UncapturedRuntime(reason=UncapturedReason.NO_SOURCE)


# ---- MEAS-004: every row names harness and model, or unknown -----------------


def test_meas_004_rows_name_harness_and_model(roots: tuple[Path, Path]) -> None:
    _transcript(roots, [_message(1, "m", output=5)])

    snapshot = capture_run_start(_ref(), at=_at(2), concurrent_run_count=1)

    assert isinstance(snapshot, CounterSnapshot)
    assert (snapshot.harness, snapshot.model) == ("claude-code", MODEL)


def test_meas_004_unnameable_attribution_is_unknown_not_defaulted(
    roots: tuple[Path, Path],
) -> None:
    _sidecar(roots, RuntimeCounters(output_tokens=1, measure_version=101))

    snapshot = capture_run_start(_ref(), at=_at(2), concurrent_run_count=1)

    assert isinstance(snapshot, CounterSnapshot)
    assert (snapshot.harness, snapshot.model) == (UNKNOWN_ATTRIBUTION, UNKNOWN_ATTRIBUTION)


def test_meas_004_snapshot_refuses_missing_or_blank_attribution() -> None:
    fields = _snapshot_fields()
    with pytest.raises(ValidationError):
        CounterSnapshot.model_validate({**fields, "harness": "  "})
    fields.pop("model")
    with pytest.raises(ValidationError):
        CounterSnapshot.model_validate(fields)


# ---- MEAS-005: the baseline's concurrent-run count is non-nullable -----------


def _snapshot_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "captured_at": _at(0),
        "source": "transcript",
        "harness": "claude-code",
        "model": MODEL,
        "measurement_version": MEASURE_VERSION,
        "concurrent_run_count": 1,
        "counters": {name.value: {"state": "observed", "value": "0"} for name in CounterName},
    }
    return {**fields, **overrides}


@pytest.mark.parametrize("count", [None, 0, -1, "2", 1.0])
def test_meas_005_concurrent_run_count_is_required_and_positive(count: object) -> None:
    with pytest.raises(ValidationError):
        CounterSnapshot.model_validate(_snapshot_fields(concurrent_run_count=count))


def test_meas_005_missing_concurrent_run_count_is_refused() -> None:
    fields = _snapshot_fields()
    fields.pop("concurrent_run_count")

    with pytest.raises(ValidationError):
        CounterSnapshot.model_validate(fields)


def test_meas_005_snapshot_refuses_a_counter_left_out() -> None:
    counters = _snapshot_fields()["counters"]
    counters.pop("cost_usd")

    with pytest.raises(ValidationError, match="omits"):
        CounterSnapshot.model_validate(_snapshot_fields(counters=counters))


# ---- MEAS-006: a Run claimed mid-turn does not bank the turn's earlier part --


def test_meas_006_mid_turn_start_banks_only_the_share_after_it(roots: tuple[Path, Path]) -> None:
    # The turn runs 0s..10s and Claude reports it only at its end; the Run
    # starts at 4s, when no duration row exists yet.
    head = [_prompt(0), _message(2, "m1", output=10)]
    _transcript(roots, head)
    baseline = capture_run_start(_ref(), at=_at(4), concurrent_run_count=1)
    _transcript(roots, [*head, _message(8, "m2", output=10), _turn(10, 10_000)])

    captured = capture_run_terminal(_ref(), baseline=baseline, started_at=_at(4), at=_at(12))

    assert isinstance(baseline, CounterSnapshot)
    assert _observed(baseline, CounterName.DURATION_MS) == 0
    assert isinstance(captured, MeasuredRuntime)
    assert captured.derived is True
    assert captured.measurement_quality == "estimated"
    assert captured.reconstruction_basis == "recorded_in_transcript"
    assert _observed(captured, CounterName.DURATION_MS) < 10_000
    assert _observed(captured, CounterName.DURATION_MS) == 8000


def test_meas_006_start_between_turns_is_read_not_derived(roots: tuple[Path, Path]) -> None:
    head = [_prompt(0), _message(1, "m1", output=10), _turn(2, 2000)]
    _transcript(roots, head)
    baseline = capture_run_start(_ref(), at=_at(3), concurrent_run_count=1)
    _transcript(roots, [*head, _prompt(4), _message(5, "m2", output=3), _turn(6, 2000)])

    captured = capture_run_terminal(_ref(), baseline=baseline, started_at=_at(3), at=_at(7))

    assert isinstance(captured, MeasuredRuntime)
    assert captured.derived is False
    assert captured.measurement_quality == "measured"
    assert captured.reconstruction_basis is None


# ---- MEAS-008: the divisor is recorded when the interval is shared -----------


def test_meas_008_shared_session_divides_by_the_start_time_divisor(
    roots: tuple[Path, Path],
) -> None:
    head = [_prompt(0), _message(1, "m1", output=10), _turn(2, 2000)]
    _transcript(roots, head)
    baseline = capture_run_start(_ref(), at=_at(3), concurrent_run_count=3)
    _transcript(roots, [*head, _prompt(4), _message(5, "m2", output=30), _turn(6, 3000)])

    captured = capture_run_terminal(_ref(), baseline=baseline, started_at=_at(3), at=_at(7))

    assert isinstance(captured, MeasuredRuntime)
    assert captured.divisor == 3
    assert captured.measurement_quality == "derived"
    assert captured.reconstruction_basis == "recorded_in_transcript"
    assert _observed(captured, CounterName.OUTPUT_TOKENS) == 10
    assert _observed(captured, CounterName.DURATION_MS) == 1000


# ---- MEAS-009 / MEAS-010: exclusions say why; rows carry their version -------


def _snapshot(**overrides: Any) -> CounterSnapshot:
    return CounterSnapshot.model_validate(_snapshot_fields(**overrides))


def _spans() -> SpanSummary | Unobserved:
    return Unobserved(reason="not under test")


@pytest.mark.parametrize(
    ("stop", "reason"),
    [
        ({"measurement_version": MEASURE_VERSION + 1}, ExclusionReason.MEASUREMENT_VERSION_CHANGED),
        ({"source": "sidecar"}, ExclusionReason.SOURCE_CHANGED),
    ],
)
def test_meas_009_excluded_row_records_why(stop: dict[str, Any], reason: ExclusionReason) -> None:
    excluded = measure_run(_snapshot(), _snapshot(**stop), spans=_spans())

    assert isinstance(excluded, ExcludedRuntime)
    assert excluded.reason is reason
    assert excluded.detail


def test_meas_009_counter_falling_is_an_excluded_reset() -> None:
    counters = _snapshot_fields()["counters"]
    start = _snapshot(counters={**counters, "output_tokens": {"state": "observed", "value": "5"}})

    excluded = measure_run(start, _snapshot(), spans=_spans())

    assert isinstance(excluded, ExcludedRuntime)
    assert excluded.reason is ExclusionReason.COUNTER_RESET
    assert "output_tokens" in excluded.detail


def test_meas_010_rows_carry_measurement_version() -> None:
    measured = measure_run(_snapshot(), _snapshot(), spans=_spans())
    excluded = measure_run(
        _snapshot(), _snapshot(measurement_version=MEASURE_VERSION + 1), spans=_spans()
    )

    assert isinstance(measured, MeasuredRuntime)
    assert measured.measurement_version == MEASURE_VERSION
    assert isinstance(excluded, ExcludedRuntime)
    assert (excluded.baseline_version, excluded.terminal_version) == (
        MEASURE_VERSION,
        MEASURE_VERSION + 1,
    )


def test_meas_010_measured_row_refuses_missing_version() -> None:
    measured = measure_run(_snapshot(), _snapshot(), spans=_spans())
    payload = measured.model_dump(mode="json")
    payload.pop("measurement_version")

    with pytest.raises(ValidationError):
        MeasuredRuntime.model_validate(payload)


# ---- MEAS-053: absence and zero are distinct at capture ----------------------


def test_meas_053_sidecar_field_absent_is_unobserved_not_zero(roots: tuple[Path, Path]) -> None:
    _sidecar(roots, RuntimeCounters(output_tokens=0, measure_version=101))

    snapshot = capture_run_start(_ref(), at=_at(2), concurrent_run_count=1)

    assert isinstance(snapshot, CounterSnapshot)
    assert snapshot.counters[CounterName.OUTPUT_TOKENS] == Observed(value=Decimal(0))
    cost = snapshot.counters[CounterName.COST_USD]
    assert isinstance(cost, Unobserved)
    assert "cost_usd" in cost.reason


def test_meas_053_transcript_class_no_message_reports_is_unobserved(
    roots: tuple[Path, Path],
) -> None:
    row = _message(1, "m", output=5)
    del row["message"]["usage"]["cache_read_input_tokens"]
    _transcript(roots, [row])

    snapshot = capture_run_start(_ref(), at=_at(2), concurrent_run_count=1)

    assert isinstance(snapshot, CounterSnapshot)
    assert isinstance(snapshot.counters[CounterName.CACHE_READ_INPUT_TOKENS], Unobserved)
    assert _observed(snapshot, CounterName.OUTPUT_TOKENS) == 5


def test_meas_053_unpriced_model_cost_is_unobserved(roots: tuple[Path, Path]) -> None:
    _transcript(roots, [_message(1, "m", output=5, model="mystery-model")])

    snapshot = capture_run_start(_ref(), at=_at(2), concurrent_run_count=1)

    assert isinstance(snapshot, CounterSnapshot)
    cost = snapshot.counters[CounterName.COST_USD]
    assert isinstance(cost, Unobserved)
    assert "mystery-model" in cost.reason


def test_meas_053_fresh_transcript_is_an_observed_zero(roots: tuple[Path, Path]) -> None:
    _transcript(roots, [_prompt(0)])

    snapshot = capture_run_start(_ref(), at=_at(1), concurrent_run_count=1)

    assert isinstance(snapshot, CounterSnapshot)
    assert all(snapshot.counters[name] == Observed(value=Decimal(0)) for name in CounterName)


def test_meas_053_unobserved_start_makes_an_unobserved_delta() -> None:
    counters = _snapshot_fields()["counters"]
    start = _snapshot(counters={**counters, "cost_usd": {"state": "unobserved", "reason": "x"}})

    measured = measure_run(start, _snapshot(), spans=_spans())

    assert isinstance(measured, MeasuredRuntime)
    assert isinstance(measured.counters[CounterName.COST_USD], Unobserved)


def test_meas_053_observed_counter_refuses_a_negative_value() -> None:
    with pytest.raises(ValidationError):
        Observed(value=Decimal(-1))
    with pytest.raises(ValidationError):
        Unobserved(reason=" ")
