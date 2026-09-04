"""Unit tests for the ``eawf bench`` harness library + CLI surface.

Covers the three load-bearing guarantees from the C09 spec § 5.5:

- **Determinism** — re-seeding the same size produces byte-identical
  output (the bench baselines depend on it). The committed
  ``tests/fixtures/bench/small.json`` must equal a fresh seed.
- **Regression flagging** — ``compare_results`` flags exactly when
  ``after >= before * (1 + threshold)`` and not otherwise.
- **Per-OS thresholds** — ``thresholds.yaml`` maps Linux 0.10 /
  macOS 0.20 / Windows 0.15 and resolves by running OS.

Plus CLI smoke for ``list`` / ``run`` / ``compare`` / ``fixture seed``
and the regression exit code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError
from typer.testing import CliRunner

from eawf.kernel.state.enums import AgentSessionRole, ScopeKind, WaveStatus
from eawf.kernel.state.models import CurrentPointers, State, Wave
from eawf.observability.bench.harness import (
    HARNESS_CATALOG,
    BenchResult,
    compare_results,
    load_thresholds,
    run_all,
    run_harness,
    threshold_for_os,
)
from eawf.observability.bench.seed import FIXTURE_SIZES, seed_corpus, seed_fixture
from eawf.observability.bench.turn_cost import (
    COMPARABILITY_FIELDS,
    LIVE_FIXTURE_ID,
    TURN_COST_HARNESS_REVISION,
    TurnCostVerdict,
    baseline_from_record,
    build_corpus_record,
    collect_live_corpus,
    compare_turn_cost,
    load_baseline,
    seed_turn_cost_corpus,
    write_baseline,
)
from eawf.observability.telemetry.models import RuntimeName, TelemetrySession
from eawf.observability.telemetry.turn_cost import TurnCostRecord
from eawf.surfaces.cli.app import app

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "bench"
_THRESHOLDS_YAML = _REPO_ROOT / ".ea" / "bench" / "thresholds.yaml"
_TURN_COST_FIXTURE = "turn-cost-small"


# --- seed determinism ------------------------------------------------------


@pytest.mark.parametrize("size", FIXTURE_SIZES)
def test_seed_fixture_byte_identical_on_reseed(size: str, tmp_path: Path) -> None:
    """Re-seeding the same size yields byte-identical files."""
    first_state, first_event = seed_fixture(size, tmp_path / "a")  # type: ignore[arg-type]
    second_state, second_event = seed_fixture(size, tmp_path / "b")  # type: ignore[arg-type]

    assert first_state.read_bytes() == second_state.read_bytes()
    assert first_event.read_bytes() == second_event.read_bytes()


def test_seed_fixture_overwrite_in_place_is_stable(tmp_path: Path) -> None:
    """Seeding twice into the same dir overwrites byte-identically."""
    state_path, event_path = seed_fixture("small", tmp_path)
    before_state = state_path.read_bytes()
    before_event = event_path.read_bytes()

    seed_fixture("small", tmp_path)
    assert state_path.read_bytes() == before_state
    assert event_path.read_bytes() == before_event


def test_committed_small_fixture_matches_fresh_seed(tmp_path: Path) -> None:
    """The committed small.json equals a fresh seed (drift guard)."""
    fresh_state, fresh_event = seed_fixture("small", tmp_path)
    committed_state = (_FIXTURE_DIR / "small.json").read_bytes()
    committed_event = (_FIXTURE_DIR / "small-event.jsonl").read_bytes()

    assert fresh_state.read_bytes() == committed_state
    assert fresh_event.read_bytes() == committed_event


def test_seed_corpus_dimensions_match_size_table() -> None:
    """Each size emits the spec-table wave / phase / event counts."""
    small = seed_corpus("small")
    assert small.state["counts"] == {"phases": 1, "waves": 10, "events": 200}
    assert len(small.events) == 200

    large = seed_corpus("large")
    assert large.state["counts"] == {"phases": 8, "waves": 200, "events": 20_000}


def test_seed_corpus_rejects_unknown_size() -> None:
    """An unknown size raises ValueError with the offending value."""
    with pytest.raises(ValueError, match="unknown fixture size: 'jumbo'"):
        seed_corpus("jumbo")  # type: ignore[arg-type]


def test_seed_fixture_rejects_unknown_size(tmp_path: Path) -> None:
    """seed_fixture surfaces the same guard as seed_corpus."""
    with pytest.raises(ValueError, match="unknown fixture size"):
        seed_fixture("tiny", tmp_path)  # type: ignore[arg-type]


# --- harness measurement ---------------------------------------------------


def test_run_harness_returns_result_for_each_catalog_entry() -> None:
    """Every catalog harness runs and returns a populated result."""
    corpus = seed_corpus("small")
    for name in HARNESS_CATALOG:
        result = run_harness(name, corpus, iterations=3)
        assert result.name == name
        assert result.size == "small"
        assert result.iterations == 3
        assert result.best_ms >= 0.0


def test_run_all_covers_full_catalog_in_order() -> None:
    """run_all returns one result per catalog entry, in catalog order."""
    corpus = seed_corpus("small")
    results = run_all(corpus, iterations=2)
    assert [r.name for r in results] == list(HARNESS_CATALOG)


def test_run_harness_rejects_unknown_harness() -> None:
    """An unknown harness name raises ValueError."""
    corpus = seed_corpus("small")
    with pytest.raises(ValueError, match="unknown harness: 'nope'"):
        run_harness("nope", corpus)


def test_run_harness_rejects_zero_iterations() -> None:
    """iterations < 1 raises ValueError (off-by-one boundary)."""
    corpus = seed_corpus("small")
    with pytest.raises(ValueError, match="iterations must be >= 1"):
        run_harness("state_load_validate", corpus, iterations=0)


# --- regression comparison -------------------------------------------------


def _result(name: str, best_ms: float) -> BenchResult:
    return BenchResult(name=name, size="small", iterations=1, best_ms=best_ms)


def test_compare_flags_regression_at_threshold_boundary() -> None:
    """after == before * (1 + threshold) is a regression (>=, not >)."""
    before = [_result("h", 100.0)]
    after = [_result("h", 110.0)]  # exactly +10%
    comparisons = compare_results(before, after, threshold=0.10)
    assert len(comparisons) == 1
    assert comparisons[0].regressed is True
    assert comparisons[0].ratio == pytest.approx(1.10)


def test_compare_no_regression_just_below_threshold() -> None:
    """Just under the threshold is not a regression."""
    before = [_result("h", 100.0)]
    after = [_result("h", 109.99)]
    comparisons = compare_results(before, after, threshold=0.10)
    assert comparisons[0].regressed is False


def test_compare_improvement_not_a_regression() -> None:
    """A faster after-run never regresses."""
    before = [_result("h", 100.0)]
    after = [_result("h", 80.0)]
    comparisons = compare_results(before, after, threshold=0.10)
    assert comparisons[0].regressed is False
    assert comparisons[0].ratio == pytest.approx(0.80)


def test_compare_only_shared_harnesses() -> None:
    """Harnesses unique to one side are skipped."""
    before = [_result("a", 1.0), _result("b", 1.0)]
    after = [_result("b", 1.0), _result("c", 1.0)]
    comparisons = compare_results(before, after, threshold=0.10)
    assert [c.name for c in comparisons] == ["b"]


def test_compare_rejects_negative_threshold() -> None:
    """A negative threshold is rejected."""
    with pytest.raises(ValueError, match="threshold must be >= 0"):
        compare_results([_result("h", 1.0)], [_result("h", 1.0)], threshold=-0.1)


def test_compare_empty_inputs_yield_no_comparisons() -> None:
    """Empty result sets compare cleanly to an empty list (boundary)."""
    assert compare_results([], [], threshold=0.10) == []


# --- threshold resolution --------------------------------------------------


def test_thresholds_yaml_has_per_os_values() -> None:
    """The committed thresholds.yaml carries the spec per-OS values."""
    parsed = yaml.safe_load(_THRESHOLDS_YAML.read_text(encoding="utf-8"))
    assert parsed["thresholds"] == {"linux": 0.10, "macos": 0.20, "windows": 0.15}


def test_load_thresholds_resolves_per_os() -> None:
    """threshold_for_os maps platform.system() onto the friendly keys."""
    thresholds = load_thresholds(_THRESHOLDS_YAML)
    assert threshold_for_os(thresholds, "Linux") == pytest.approx(0.10)
    assert threshold_for_os(thresholds, "Darwin") == pytest.approx(0.20)
    assert threshold_for_os(thresholds, "Windows") == pytest.approx(0.15)


def test_load_thresholds_missing_file_uses_defaults(tmp_path: Path) -> None:
    """An absent thresholds file degrades to built-in defaults."""
    thresholds = load_thresholds(tmp_path / "nope.yaml")
    assert threshold_for_os(thresholds, "Linux") == pytest.approx(0.10)
    assert threshold_for_os(thresholds, "Darwin") == pytest.approx(0.20)


def test_threshold_for_os_unknown_os_falls_back_to_linux() -> None:
    """An unmapped OS falls back to the Linux default."""
    thresholds = load_thresholds(_THRESHOLDS_YAML)
    assert threshold_for_os(thresholds, "Plan9") == pytest.approx(0.10)


def test_load_thresholds_rejects_non_mapping(tmp_path: Path) -> None:
    """A YAML lacking a top-level 'thresholds' mapping is rejected."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("just-a-scalar\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed thresholds file"):
        load_thresholds(bad)


def test_load_thresholds_rejects_non_numeric_value(tmp_path: Path) -> None:
    """A non-numeric threshold value is rejected."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("thresholds:\n  linux: high\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a number"):
        load_thresholds(bad)


# --- CLI smoke -------------------------------------------------------------


def test_cli_bench_list_json() -> None:
    """`eawf bench list --json` lists sizes + harnesses."""
    result = runner.invoke(app, ["--json", "bench", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sizes"] == list(FIXTURE_SIZES)
    assert {h["name"] for h in payload["harnesses"]} == set(HARNESS_CATALOG)


def test_cli_bench_run_json() -> None:
    """`eawf bench run --json` emits a result per harness."""
    result = runner.invoke(app, ["--json", "bench", "run", "--size", "small", "--iterations", "2"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["size"] == "small"
    assert {r["name"] for r in payload["results"]} == set(HARNESS_CATALOG)


def test_cli_bench_run_rejects_unknown_size() -> None:
    """`eawf bench run --size bogus` exits 1 (USER_ERROR)."""
    result = runner.invoke(app, ["bench", "run", "--size", "bogus"])
    assert result.exit_code == 1, result.output


def test_cli_fixture_seed_writes_byte_identical(tmp_path: Path) -> None:
    """`eawf bench fixture seed` writes the deterministic corpus."""
    out = tmp_path / "corpus"
    first = runner.invoke(app, ["bench", "fixture", "seed", "--size", "small", "--out", str(out)])
    assert first.exit_code == 0, first.output
    state = (out / "small.json").read_bytes()

    second = runner.invoke(app, ["bench", "fixture", "seed", "--size", "small", "--out", str(out)])
    assert second.exit_code == 0, second.output
    assert (out / "small.json").read_bytes() == state


def test_cli_bench_compare_flags_regression(tmp_path: Path) -> None:
    """`eawf bench compare` exits 2 when a harness regresses."""
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_text(
        json.dumps({"results": [{"name": "h", "size": "small", "iterations": 1, "best_ms": 10.0}]})
    )
    after.write_text(
        json.dumps({"results": [{"name": "h", "size": "small", "iterations": 1, "best_ms": 50.0}]})
    )
    result = runner.invoke(
        app,
        ["--json", "bench", "compare", "--before", str(before), "--after", str(after)],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["regressed"] is True


def test_cli_bench_compare_clean_when_stable(tmp_path: Path) -> None:
    """`eawf bench compare` exits 0 when nothing regresses."""
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    rows = {"results": [{"name": "h", "size": "small", "iterations": 1, "best_ms": 10.0}]}
    before.write_text(json.dumps(rows))
    after.write_text(json.dumps(rows))
    result = runner.invoke(
        app,
        ["bench", "compare", "--before", str(before), "--after", str(after), "--threshold", "0.10"],
    )
    assert result.exit_code == 0, result.output


def test_cli_bench_compare_missing_file_exits_user_error(tmp_path: Path) -> None:
    """A missing results file exits 1 (USER_ERROR)."""
    after = tmp_path / "after.json"
    after.write_text(json.dumps({"results": []}))
    result = runner.invoke(
        app,
        ["bench", "compare", "--before", str(tmp_path / "nope.json"), "--after", str(after)],
    )
    assert result.exit_code == 1, result.output


# --- turn-cost: library ----------------------------------------------------


def _turn_cost_record() -> TurnCostRecord:
    """Build the record for the committed deterministic fixture."""
    return build_corpus_record(seed_turn_cost_corpus(_TURN_COST_FIXTURE))


def _live_state(waves: list[Wave]) -> State:
    """Build the smallest State that carries *waves*."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:TC",
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
            "project": None,
            "current": CurrentPointers().model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {w.id: w.model_dump(mode="json") for w in waves},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _live_wave(
    wave_id: str,
    *,
    status: WaveStatus = WaveStatus.CLOSED,
    role: AgentSessionRole | None = AgentSessionRole.EXECUTOR,
) -> Wave:
    return Wave(
        id=wave_id,
        iter_id=wave_id.rsplit("-", 1)[0],
        title=f"Live unit {wave_id}",
        status=status,
        agent_role=role,
        opened_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _live_session(
    session_id: str,
    *,
    wave_id: str | None,
    runtime: RuntimeName = "claude",
    model: str | None = "claude-opus-4-7",
) -> TelemetrySession:
    return TelemetrySession(
        session_id=session_id,
        project_id="proj",
        runtime=runtime,
        wave_id=wave_id,
        attempt_id=None,
        session_log_path="urn:eawf:v1:session-log:claude:x",
        started_at=None,
        ended_at=None,
        duration_ms=4_000,
        model_primary=model,
        total_input_tokens=100,
        total_output_tokens=10,
        total_cost_usd=Decimal("0.40"),
        end_marker="clean_stop",
    )


def test_seed_turn_cost_corpus_is_the_documented_fixture_shape() -> None:
    """The fixture pins every field the render surface reports."""
    record = _turn_cost_record()
    assert record.fixture_id == _TURN_COST_FIXTURE
    assert record.harness_revision == TURN_COST_HARNESS_REVISION
    assert (record.runtime, record.model) == ("claude", "fixture-model-v1")
    assert record.unit_count == 10
    assert (record.p50_wall_clock_ms, record.p90_wall_clock_ms) == (5_000, 9_000)
    assert (record.p50_cost_usd, record.p90_cost_usd) == (Decimal("0.06"), Decimal("0.09"))
    assert record.verification_cost_usd == Decimal("0.05")
    assert record.execution_cost_usd == Decimal("0.55")
    assert (record.unattributed_run_count, record.unpriced_run_count) == (1, 1)


def test_seed_turn_cost_corpus_rejects_unknown_fixture() -> None:
    """An unknown fixture id raises ValueError naming the offending value."""
    with pytest.raises(ValueError, match="unknown turn-cost fixture: 'jumbo'"):
        seed_turn_cost_corpus("jumbo")


def test_seed_turn_cost_corpus_rejects_empty_fixture_id() -> None:
    """The empty string is not a fixture id (boundary)."""
    with pytest.raises(ValueError, match="unknown turn-cost fixture"):
        seed_turn_cost_corpus("")


def test_baseline_from_record_rejects_negative_threshold() -> None:
    """A negative tolerance is rejected before it can be recorded."""
    with pytest.raises(ValueError, match="threshold must be >= 0"):
        baseline_from_record(_turn_cost_record(), threshold=Decimal("-0.01"))


def test_baseline_from_record_accepts_zero_threshold() -> None:
    """Zero tolerance is the tightest legal baseline (boundary)."""
    baseline = baseline_from_record(_turn_cost_record(), threshold=Decimal("0"))
    assert baseline.threshold == Decimal("0")


def test_compare_turn_cost_zero_threshold_blocks_any_rise() -> None:
    """At zero tolerance an unchanged record passes and a raised one blocks."""
    record = _turn_cost_record()
    baseline = baseline_from_record(record, threshold=Decimal("0"))
    assert compare_turn_cost(baseline=baseline, record=record).verdict is TurnCostVerdict.OK

    tighter = baseline.model_copy(update={"p90_wall_clock_ms": record.p90_wall_clock_ms - 1})
    assert compare_turn_cost(baseline=tighter, record=record).verdict is TurnCostVerdict.REGRESSED


def test_compare_turn_cost_zero_baseline_regresses_on_any_rise() -> None:
    """A zero baseline is crossed by anything above zero (division boundary)."""
    record = _turn_cost_record()
    baseline = baseline_from_record(record, threshold=Decimal("0.10")).model_copy(
        update={"p90_wall_clock_ms": 0, "p90_cost_usd": Decimal("0")}
    )
    comparison = compare_turn_cost(baseline=baseline, record=record)
    assert comparison.verdict is TurnCostVerdict.REGRESSED
    assert comparison.wall_clock_regressed is True
    assert comparison.cost_regressed is True


def test_compare_turn_cost_improvement_is_not_a_regression() -> None:
    """A cheaper, faster record passes."""
    record = _turn_cost_record()
    baseline = baseline_from_record(record, threshold=Decimal("0.10")).model_copy(
        update={"p90_wall_clock_ms": 99_000, "p90_cost_usd": Decimal("9.99")}
    )
    assert compare_turn_cost(baseline=baseline, record=record).verdict is TurnCostVerdict.OK


def test_compare_turn_cost_names_every_field_on_comparison_invalid() -> None:
    """All four comparability fields surface, in declaration order."""
    record = _turn_cost_record()
    baseline = baseline_from_record(record, threshold=Decimal("0.10")).model_copy(
        update={
            "fixture_id": "other",
            "harness_revision": "other",
            "runtime": "codex",
            "model": "other",
        }
    )
    comparison = compare_turn_cost(baseline=baseline, record=record)
    assert comparison.verdict is TurnCostVerdict.COMPARISON_INVALID
    assert comparison.mismatched_fields == COMPARABILITY_FIELDS
    assert comparison.candidate_p90_wall_clock_ms is None
    assert comparison.candidate_p90_cost_usd is None


def test_load_baseline_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    """An absent baseline is a missing file, not an empty one."""
    with pytest.raises(FileNotFoundError, match="turn-cost baseline not found"):
        load_baseline(tmp_path / "nope.json")


def test_load_baseline_malformed_json_raises_value_error(tmp_path: Path) -> None:
    """Unparseable JSON is rejected with the offending path."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed turn-cost baseline JSON"):
        load_baseline(bad)


def test_load_baseline_rejects_unknown_key(tmp_path: Path) -> None:
    """extra='forbid' rejects a baseline carrying an unknown field."""
    path = tmp_path / "b.json"
    payload = baseline_from_record(_turn_cost_record(), threshold=Decimal("0.10")).model_dump(
        mode="json"
    )
    payload["p50_cost_usd"] = "0.01"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PydanticValidationError, match="p50_cost_usd"):
        load_baseline(path)


def test_load_baseline_rejects_missing_key(tmp_path: Path) -> None:
    """A baseline without its threshold cannot be checked against."""
    path = tmp_path / "b.json"
    payload = baseline_from_record(_turn_cost_record(), threshold=Decimal("0.10")).model_dump(
        mode="json"
    )
    del payload["threshold"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PydanticValidationError, match="threshold"):
        load_baseline(path)


def test_load_baseline_round_trips_written_artifact(tmp_path: Path) -> None:
    """write_baseline / load_baseline is an identity round trip."""
    path = tmp_path / "nested" / "b.json"
    baseline = baseline_from_record(_turn_cost_record(), threshold=Decimal("0.10"))
    write_baseline(baseline, path)
    assert load_baseline(path) == baseline


def test_collect_live_corpus_without_sessions_is_honest_empty() -> None:
    """No projected session means nothing to measure, not an error."""
    resolution = collect_live_corpus(state=_live_state([]), sessions=[])
    assert resolution.corpus is None
    assert resolution.skipped_session_count == 0
    assert resolution.reason is not None


def test_collect_live_corpus_skips_sessions_that_cannot_become_runs() -> None:
    """Sessions with no wave, an open wave, no role or no model are counted out."""
    waves = [
        _live_wave("P31-I01-W01"),
        _live_wave("P31-I01-W02", status=WaveStatus.IN_PROGRESS),
        _live_wave("P31-I01-W03", role=None),
    ]
    sessions = [
        _live_session("s-none", wave_id=None),
        _live_session("s-unknown", wave_id="P31-I01-W99"),
        _live_session("s-open", wave_id="P31-I01-W02"),
        _live_session("s-roleless", wave_id="P31-I01-W03"),
        _live_session("s-modelless", wave_id="P31-I01-W01", model=None),
        _live_session("s-good", wave_id="P31-I01-W01"),
    ]
    resolution = collect_live_corpus(state=_live_state(waves), sessions=sessions)
    assert resolution.skipped_session_count == 5
    assert resolution.corpus is not None
    assert [run.run_id for run in resolution.corpus.runs] == ["s-good"]


def test_collect_live_corpus_refuses_a_mixed_runtime_model_corpus() -> None:
    """A percentile across models measures the mix, so the corpus is refused."""
    waves = [_live_wave("P31-I01-W01"), _live_wave("P31-I01-W02")]
    sessions = [
        _live_session("s-a", wave_id="P31-I01-W01"),
        _live_session("s-b", wave_id="P31-I01-W02", runtime="codex", model="gpt-x"),
    ]
    resolution = collect_live_corpus(state=_live_state(waves), sessions=sessions)
    assert resolution.corpus is None
    assert resolution.reason is not None
    assert "runtime/model tuples" in resolution.reason


def test_collect_live_corpus_builds_a_single_tuple_corpus() -> None:
    """A homogeneous corpus measures, declaring the tuple its runs share."""
    waves = [_live_wave("P31-I01-W01")]
    resolution = collect_live_corpus(
        state=_live_state(waves), sessions=[_live_session("s-a", wave_id="P31-I01-W01")]
    )
    assert resolution.corpus is not None
    assert (resolution.corpus.runtime, resolution.corpus.model) == ("claude", "claude-opus-4-7")
    record = build_corpus_record(resolution.corpus)
    assert record.fixture_id == LIVE_FIXTURE_ID
    assert record.unit_count == 1


# --- turn-cost: CLI render -------------------------------------------------


def test_cli_bench_turn_cost_render_json_emits_the_producer_record() -> None:
    """`eawf bench turn-cost --json` emits every W17 record field."""
    result = runner.invoke(app, ["--json", "bench", "turn-cost", "--fixture", _TURN_COST_FIXTURE])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)["record"]
    assert record["fixture_id"] == _TURN_COST_FIXTURE
    assert record["harness_revision"] == TURN_COST_HARNESS_REVISION
    assert (record["runtime"], record["model"]) == ("claude", "fixture-model-v1")
    assert (record["p50_wall_clock_ms"], record["p90_wall_clock_ms"]) == (5_000, 9_000)
    assert (record["p50_cost_usd"], record["p90_cost_usd"]) == ("0.06", "0.09")
    assert record["verification_cost_usd"] == "0.05"
    assert record["execution_cost_usd"] == "0.55"
    assert record["unattributed_run_count"] == 1
    assert record["unpriced_run_count"] == 1


def test_cli_bench_turn_cost_render_replay_is_byte_identical() -> None:
    """Replaying the fixture twice renders the same bytes."""
    argv = ["--json", "bench", "turn-cost", "--fixture", _TURN_COST_FIXTURE]
    first = runner.invoke(app, argv)
    second = runner.invoke(app, argv)
    assert first.exit_code == 0, first.output
    assert first.output == second.output


def test_cli_bench_turn_cost_render_unknown_fixture_exits_user_error() -> None:
    """An unknown fixture exits 1 (USER_ERROR)."""
    result = runner.invoke(app, ["bench", "turn-cost", "--fixture", "jumbo"])
    assert result.exit_code == 1, result.output


def test_cli_bench_turn_cost_render_rejects_baseline_without_check() -> None:
    """`--baseline` alone is a flag error, not a silent no-op."""
    result = runner.invoke(app, ["bench", "turn-cost", "--baseline", "b.json"])
    assert result.exit_code == 1, result.output
    assert "--check" in result.output


def test_cli_bench_turn_cost_render_rejects_negative_threshold() -> None:
    """A negative recorded tolerance is refused at the boundary."""
    result = runner.invoke(
        app,
        ["bench", "turn-cost", "--fixture", _TURN_COST_FIXTURE, "--threshold", "-0.1"],
    )
    assert result.exit_code == 1, result.output


def test_cli_bench_turn_cost_render_without_cache_is_honest_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project with no projected telemetry renders nothing-measured, exit 0."""
    monkeypatch.delenv("EA_STATE", raising=False)
    result = runner.invoke(app, ["--json", "--workspace", str(tmp_path), "bench", "turn-cost"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["measured"] is False
    assert "telemetry cache" in payload["reason"]
    assert not (tmp_path / ".ea" / "telemetry.db").exists()


# --- turn-cost: CLI check --------------------------------------------------


def _write_turn_cost_baseline(path: Path, **overrides: object) -> None:
    """Write the fixture's own baseline to *path*, with field overrides."""
    payload = baseline_from_record(_turn_cost_record(), threshold=Decimal("0.10")).model_dump(
        mode="json"
    )
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _check_argv(path: Path, *, json_output: bool = False) -> list[str]:
    """Build the `bench turn-cost --check` argv against baseline *path*."""
    prefix = ["--json"] if json_output else []
    return [
        *prefix,
        "bench",
        "turn-cost",
        "--fixture",
        _TURN_COST_FIXTURE,
        "--check",
        "--baseline",
        str(path),
    ]


def test_cli_bench_turn_cost_check_passes_within_threshold(tmp_path: Path) -> None:
    """A baseline written from the current record checks clean (exit 0)."""
    path = tmp_path / "baseline.json"
    written = runner.invoke(
        app,
        ["bench", "turn-cost", "--fixture", _TURN_COST_FIXTURE, "--write-baseline", str(path)],
    )
    assert written.exit_code == 0, written.output

    result = runner.invoke(app, _check_argv(path, json_output=True))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["check"]["verdict"] == "ok"


def test_cli_bench_turn_cost_check_blocks_beyond_threshold(tmp_path: Path) -> None:
    """A p90 past the recorded threshold exits 2 (VALIDATION_ERROR)."""
    path = tmp_path / "baseline.json"
    _write_turn_cost_baseline(path, p90_wall_clock_ms=1_000, p90_cost_usd="0.01")

    result = runner.invoke(app, _check_argv(path, json_output=True))
    assert result.exit_code == 2, result.output
    check = json.loads(result.output)["check"]
    assert check["verdict"] == "regressed"
    assert check["wall_clock_regressed"] is True
    assert check["cost_regressed"] is True


def test_cli_bench_turn_cost_check_blocks_exactly_at_threshold(tmp_path: Path) -> None:
    """candidate == baseline * (1 + threshold) blocks (>=, not >)."""
    path = tmp_path / "baseline.json"
    _write_turn_cost_baseline(path, threshold="0.5", p90_wall_clock_ms=6_000, p90_cost_usd="0.06")
    result = runner.invoke(app, _check_argv(path))
    assert result.exit_code == 2, result.output


def test_cli_bench_turn_cost_check_passes_just_below_threshold(tmp_path: Path) -> None:
    """Just under the recorded threshold is not a regression (off-by-one)."""
    path = tmp_path / "baseline.json"
    _write_turn_cost_baseline(path, threshold="0.5", p90_wall_clock_ms=6_001, p90_cost_usd="0.061")
    result = runner.invoke(app, _check_argv(path))
    assert result.exit_code == 0, result.output


def test_cli_bench_turn_cost_check_fixture_mismatch_is_comparison_invalid(
    tmp_path: Path,
) -> None:
    """A baseline from another fixture refuses instead of comparing."""
    path = tmp_path / "baseline.json"
    _write_turn_cost_baseline(path, fixture_id="turn-cost-other")

    result = runner.invoke(app, _check_argv(path))
    assert result.exit_code == 1, result.output
    assert "comparison_invalid" in result.output
    assert "fixture_id" in result.output


def test_cli_bench_turn_cost_check_harness_mismatch_is_comparison_invalid(
    tmp_path: Path,
) -> None:
    """A baseline from another harness revision refuses too."""
    path = tmp_path / "baseline.json"
    _write_turn_cost_baseline(path, harness_revision="turn-cost-0")

    result = runner.invoke(app, _check_argv(path, json_output=True))
    assert result.exit_code == 1, result.output
    data = json.loads(result.output)["data"]
    assert data["verdict"] == "comparison_invalid"
    assert data["mismatched_fields"] == ["harness_revision"]
    assert data["candidate_p90_wall_clock_ms"] is None


def test_cli_bench_turn_cost_check_model_mismatch_is_comparison_invalid(
    tmp_path: Path,
) -> None:
    """A baseline measured against another model is not comparable."""
    path = tmp_path / "baseline.json"
    _write_turn_cost_baseline(path, model="some-other-model")

    result = runner.invoke(app, _check_argv(path))
    assert result.exit_code == 1, result.output
    assert "comparison_invalid" in result.output
    assert "model" in result.output


def test_cli_bench_turn_cost_check_comparison_invalid_never_rebaselines(
    tmp_path: Path,
) -> None:
    """A refused comparison leaves the baseline artifact byte-identical."""
    path = tmp_path / "baseline.json"
    _write_turn_cost_baseline(path, fixture_id="turn-cost-other")
    before = path.read_bytes()

    runner.invoke(app, _check_argv(path))
    assert path.read_bytes() == before


def test_cli_bench_turn_cost_check_without_baseline_exits_user_error() -> None:
    """`--check` with no baseline is a flag error, not a pass."""
    result = runner.invoke(app, ["bench", "turn-cost", "--check"])
    assert result.exit_code == 1, result.output
    assert "--baseline" in result.output


def test_cli_bench_turn_cost_check_missing_baseline_file_exits_user_error(
    tmp_path: Path,
) -> None:
    """A baseline path that does not exist exits 1."""
    result = runner.invoke(app, _check_argv(tmp_path / "nope.json"))
    assert result.exit_code == 1, result.output


def test_cli_bench_turn_cost_check_malformed_baseline_exits_user_error(
    tmp_path: Path,
) -> None:
    """An unparseable baseline exits 1 rather than crashing."""
    path = tmp_path / "baseline.json"
    path.write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, _check_argv(path))
    assert result.exit_code == 1, result.output


def test_cli_bench_turn_cost_check_empty_corpus_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to measure is a refusal under --check, never a false green."""
    monkeypatch.delenv("EA_STATE", raising=False)
    path = tmp_path / "baseline.json"
    _write_turn_cost_baseline(path)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "bench",
            "turn-cost",
            "--check",
            "--baseline",
            str(path),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "cannot check turn-cost" in result.output
