"""Tests for the test-kind -> gate-tier ladder and the red-to-green check.

Covers the kind-to-tier mapping's totality and drift detection, the
EAWF024 tier-ladder rule that flags a kind marker filed on the wrong
rung, and the red-then-green check a task boundary applies to its named
test's runs.
"""

from __future__ import annotations

import pytest

from eawf.platform.lint.eawf024_test_tier_contract import (
    RULE_CODE,
    TierLadderViolation,
    check_tier_ladder,
)
from eawf.platform.lint.kind_taxonomy import (
    KIND_GATE_TIERS,
    KIND_MARKERS,
    TIER_RUNTIME_BUDGET_SECONDS,
    GateTier,
    RunOutcome,
    TaskTestRun,
    gate_tier_for_kind,
    gate_tier_for_test_path,
    red_to_green_finding,
    taxonomy_drift,
    tier_rank,
)
from eawf.platform.lint.kind_taxonomy import TestKind as Kind

_ALL_LANES = {kind.value: "default" for kind in Kind}


def _tier_drift(**overrides: object) -> list[str]:
    """Run the taxonomy check with every non-tier surface held clean."""
    return taxonomy_drift(
        markers=KIND_MARKERS,
        directories=frozenset(),
        kind_gates=_ALL_LANES,
        **overrides,  # type: ignore[arg-type]
    )


# --- the kind -> tier mapping ----------------------------------------------


def test_kind_gate_tiers_maps_every_kind_to_exactly_one_tier() -> None:
    assert set(KIND_GATE_TIERS) == set(Kind)
    assert all(isinstance(tier, GateTier) for tier in KIND_GATE_TIERS.values())


def test_kind_gate_tiers_pins_the_ladder() -> None:
    assert {kind.value: tier.value for kind, tier in KIND_GATE_TIERS.items()} == {
        "unit": "wave",
        "contract": "wave",
        "integration": "wave",
        "property": "wave",
        "metamorphic": "wave",
        "golden": "iter",
        "tui": "iter",
        "conformance": "iter",
        "e2e": "release",
        "perf": "release",
    }


def test_tier_runtime_budget_rises_with_each_rung() -> None:
    budgets = [TIER_RUNTIME_BUDGET_SECONDS[tier] for tier in GateTier]
    assert budgets == [60, 600, 2700]
    assert budgets == sorted(set(budgets))


def test_tier_rank_orders_wave_iter_release() -> None:
    assert [tier_rank(tier) for tier in GateTier] == [0, 1, 2]
    assert tier_rank(GateTier.WAVE) < tier_rank(GateTier.RELEASE)


def test_gate_tier_for_kind_rejects_a_bare_string() -> None:
    with pytest.raises(TypeError, match="expected a TestKind"):
        gate_tier_for_kind("unit")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("path", "tier"),
    [
        ("tests/unit/test_x.py", GateTier.WAVE),
        ("/repo/tests/golden/deep/test_x.py", GateTier.ITER),
        ("tests\\e2e\\test_x.py", GateTier.RELEASE),
        ("tests/lint/test_x.py", None),
        ("tests/unit/data.json", None),
        ("tests/test_root.py", None),
        ("", None),
    ],
)
def test_gate_tier_for_test_path_boundaries(path: str, tier: GateTier | None) -> None:
    assert gate_tier_for_test_path(path) is tier


def test_taxonomy_drift_holds_for_the_declared_ladder() -> None:
    assert _tier_drift() == []


def test_taxonomy_drift_flags_a_kind_with_no_tier() -> None:
    tiers = {kind: tier for kind, tier in KIND_GATE_TIERS.items() if kind is not Kind.PERF}
    assert "kind 'perf' has no gate tier" in _tier_drift(kind_tiers=tiers)


def test_taxonomy_drift_flags_an_extra_tier_row() -> None:
    tiers = dict(KIND_GATE_TIERS)
    tiers["fuzz"] = GateTier.WAVE  # type: ignore[index]
    assert any(
        "gate-tier mapping declares kind 'fuzz'" in row for row in _tier_drift(kind_tiers=tiers)
    )


def test_taxonomy_drift_flags_a_non_gatetier_value() -> None:
    tiers = {**KIND_GATE_TIERS, Kind.UNIT: "nightly"}
    assert any("not a GateTier" in row for row in _tier_drift(kind_tiers=tiers))


def test_taxonomy_drift_flags_a_tier_without_budget() -> None:
    budgets = {GateTier.WAVE: 60, GateTier.ITER: 0}
    drift = _tier_drift(tier_budgets=budgets)
    assert "gate tier 'iter' has no positive runtime budget" in drift
    assert "gate tier 'release' has no positive runtime budget" in drift


def test_taxonomy_drift_on_an_empty_ladder_reports_every_kind_and_tier() -> None:
    drift = _tier_drift(kind_tiers={}, tier_budgets={})
    assert len(drift) == len(Kind) + len(GateTier)


# --- the tier-ladder lint --------------------------------------------------


def test_check_tier_ladder_flags_release_kind_in_wave_tier_path() -> None:
    source = "import pytest\n\npytestmark = pytest.mark.e2e\n"
    findings = check_tier_ladder(source, path="tests/unit/workflow/test_x.py")
    assert findings == [
        TierLadderViolation(
            lineno=3, col_offset=13, directory_kind=Kind.UNIT, declared_kind=Kind.E2E
        )
    ]
    assert findings[0].code == RULE_CODE
    rendered = findings[0].render()
    assert "release tier (2700s budget)" in rendered
    assert "wave tier (60s budget)" in rendered
    assert "tests/e2e/" in rendered


def test_check_tier_ladder_flags_every_off_rung_kind_from_the_wave_tier() -> None:
    for kind in Kind:
        source = f"import pytest\n\n@pytest.mark.{kind.value}\ndef test_x() -> None: ...\n"
        findings = check_tier_ladder(source, path="tests/unit/test_x.py")
        expected = [] if KIND_GATE_TIERS[kind] is GateTier.WAVE else [kind]
        assert [finding.declared_kind for finding in findings] == expected


def test_check_tier_ladder_flags_a_wave_kind_filed_in_the_release_tier() -> None:
    findings = check_tier_ladder("pytestmark = pytest.mark.unit\n", path="tests/perf/test_x.py")
    assert [(f.directory_kind, f.declared_kind) for f in findings] == [(Kind.PERF, Kind.UNIT)]


def test_check_tier_ladder_reads_a_bare_mark_import() -> None:
    source = "from pytest import mark\n\npytestmark = [mark.slow, mark.tui]\n"
    findings = check_tier_ladder(source, path="tests/contract/test_x.py")
    assert [finding.declared_kind for finding in findings] == [Kind.TUI]


def test_check_tier_ladder_sorts_multiple_findings_by_position() -> None:
    source = "pytestmark = [pytest.mark.perf, pytest.mark.golden]\n"
    findings = check_tier_ladder(source, path="tests/unit/test_x.py")
    assert [finding.declared_kind for finding in findings] == [Kind.PERF, Kind.GOLDEN]


@pytest.mark.parametrize(
    ("source", "path"),
    [
        ("", "tests/unit/test_x.py"),
        ("pytestmark = pytest.mark.integration\n", "tests/unit/test_x.py"),
        ("pytestmark = pytest.mark.slow\n", "tests/unit/test_x.py"),
        ("x = other.mark.e2e\nmarker = 'pytest.mark.e2e'\n", "tests/unit/test_x.py"),
        ("pytestmark = pytest.mark.e2e\n", "tests/lint/test_x.py"),
        ("pytestmark = pytest.mark.e2e  # noqa: EAWF024 fixture\n", "tests/unit/test_x.py"),
        ("pytestmark = pytest.mark.unit\n", "tests/perf/test_turn_cost_harness.py"),
    ],
    ids=[
        "empty",
        "same-tier",
        "non-kind",
        "not-a-mark",
        "non-kind-dir",
        "waived",
        "grandfathered",
    ],
)
def test_check_tier_ladder_clean_cases(source: str, path: str) -> None:
    assert check_tier_ladder(source, path=path) == []


def test_check_tier_ladder_honours_an_injected_grandfather() -> None:
    source = "pytestmark = pytest.mark.e2e\n"
    path = "/repo/tests/unit/test_x.py"
    assert (
        check_tier_ladder(source, path=path, grandfather=frozenset({"tests/unit/test_x.py"})) == []
    )


def test_check_tier_ladder_raises_on_unparseable_source() -> None:
    with pytest.raises(SyntaxError):
        check_tier_ladder("def (:\n", path="tests/unit/test_x.py")


# --- red-to-green at the task boundary -------------------------------------

_TEST = "tests/unit/test_x.py::test_repro_b1"


def _runs(*outcomes: RunOutcome) -> list[TaskTestRun]:
    return [
        TaskTestRun(test_id=_TEST, outcome=outcome, revision=f"r{index}")
        for index, outcome in enumerate(outcomes)
    ]


def test_red_to_green_finding_passes_a_red_then_green_pair() -> None:
    assert red_to_green_finding(_runs(RunOutcome.RED, RunOutcome.GREEN)) is None


def test_red_to_green_finding_passes_repeated_reds_before_green() -> None:
    runs = _runs(RunOutcome.RED, RunOutcome.RED, RunOutcome.GREEN, RunOutcome.GREEN)
    assert red_to_green_finding(runs) is None


def test_red_to_green_finding_flags_a_test_that_never_ran_red() -> None:
    finding = red_to_green_finding(_runs(RunOutcome.GREEN))
    assert finding is not None
    assert finding.test_id == _TEST
    assert finding.render() == f"red-to-green: {_TEST}: first green at r0 has no earlier red run"


def test_red_to_green_finding_flags_a_red_only_after_the_first_green() -> None:
    finding = red_to_green_finding(_runs(RunOutcome.GREEN, RunOutcome.RED, RunOutcome.GREEN))
    assert finding is not None
    assert "has no earlier red run" in finding.reason


def test_red_to_green_finding_flags_a_test_that_never_went_green() -> None:
    finding = red_to_green_finding(_runs(RunOutcome.RED))
    assert finding is not None
    assert finding.reason == "the test never ran green"


def test_red_to_green_finding_flags_a_final_red_run() -> None:
    finding = red_to_green_finding(_runs(RunOutcome.RED, RunOutcome.GREEN, RunOutcome.RED))
    assert finding is not None
    assert finding.reason == "the last run at r2 is red"


def test_red_to_green_finding_flags_an_empty_run_list() -> None:
    finding = red_to_green_finding([])
    assert finding is not None
    assert finding.render() == "red-to-green: <no test>: no run of the named test was recorded"


def test_red_to_green_finding_rejects_mixed_test_ids() -> None:
    runs = [
        TaskTestRun(test_id="a", outcome=RunOutcome.RED, revision="r0"),
        TaskTestRun(test_id="b", outcome=RunOutcome.GREEN, revision="r1"),
    ]
    with pytest.raises(ValueError, match="one task names one test"):
        red_to_green_finding(runs)
