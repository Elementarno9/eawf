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
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from eawf.bench.harness import (
    HARNESS_CATALOG,
    BenchResult,
    compare_results,
    load_thresholds,
    run_all,
    run_harness,
    threshold_for_os,
)
from eawf.bench.seed import FIXTURE_SIZES, seed_corpus, seed_fixture
from eawf.cli.app import app

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "bench"
_THRESHOLDS_YAML = _REPO_ROOT / ".ea" / "bench" / "thresholds.yaml"


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
