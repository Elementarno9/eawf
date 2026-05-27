"""Recorded-corpus experiments for rule-adherence evaluation.

The module keeps experiment mechanics pure and deterministic: callers provide
recorded wave cases, and the runner returns typed reports that can be compared
against checked-in golden output.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.observability.eval.rule_adherence import (
    RULE_IDS,
    RecordedWaveCase,
    RuleAdherenceReport,
    RuleId,
    build_rule_adherence_baseline,
    check_rule_adherence,
    load_recorded_wave_cases,
)

RuleTier = Literal["advisory", "blocking"]

AB_MIN_DELTA_RATE: float = 0.02
AB_MIN_DELTA_PP: float = AB_MIN_DELTA_RATE * 100.0

_BLOCKING_RULES: frozenset[RuleId] = frozenset(
    (
        "no_local_path_leaks",
        "no_direct_state_json_edits",
        "no_destructive_git_reverts",
    )
)

_RULE_TIERS: dict[RuleId, RuleTier] = {
    rule_id: "blocking" if rule_id in _BLOCKING_RULES else "advisory" for rule_id in RULE_IDS
}

_RULE_WEIGHTS: dict[RuleId, int] = {
    rule_id: 3 if _RULE_TIERS[rule_id] == "blocking" else 1 for rule_id in RULE_IDS
}


class RuleBaselineRow(BaseModel):
    """Per-rule adherence baseline mined from the recorded corpus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: RuleId
    tier: RuleTier
    total_cases: int = Field(ge=0)
    pass_count: int = Field(ge=0)
    fail_count: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    failed_case_ids: tuple[str, ...]


class MinedViolation(BaseModel):
    """Violation cluster for one rule in Exp-1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: RuleId
    tier: RuleTier
    fail_count: int = Field(ge=1)
    failed_case_ids: tuple[str, ...]


class ViolationMiningReport(BaseModel):
    """Exp-1 violation-mining report over the recorded corpus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str = "Exp-1"
    name: Literal["violation_mining"] = "violation_mining"
    corpus_count: int = Field(ge=0)
    verify_count: int = Field(ge=0)
    baseline_pass_rate: float = Field(ge=0.0, le=1.0)
    per_rule_baselines: tuple[RuleBaselineRow, ...]
    top_violations: tuple[MinedViolation, ...]


class RuleProjection(BaseModel):
    """Projected per-rule adherence after a mechanism prevents matching failures."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: RuleId
    tier: RuleTier
    baseline_pass_rate: float = Field(ge=0.0, le=1.0)
    projected_pass_rate: float = Field(ge=0.0, le=1.0)
    delta_pp: float


class VariantResult(BaseModel):
    """A/B variant projection over the recorded corpus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variant_id: str
    label: str
    prevented_rules: tuple[RuleId, ...]
    prevented_violation_count: int = Field(ge=0)
    passed_checks: int = Field(ge=0)
    total_checks: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    rule_pass_rates: dict[RuleId, float]


class ABComparison(BaseModel):
    """A/B comparison with the 2pp promotion threshold encoded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    control_id: str
    treatment_id: str
    min_delta_rate: float = Field(ge=0.0)
    min_delta_pp: float = Field(ge=0.0)
    control_pass_rate: float = Field(ge=0.0, le=1.0)
    treatment_pass_rate: float = Field(ge=0.0, le=1.0)
    delta_rate: float
    delta_pp: float
    meets_threshold: bool


class TieredABReport(BaseModel):
    """Exp-2 tiered A/B report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str = "Exp-2"
    name: Literal["tiered_ab"] = "tiered_ab"
    control: VariantResult
    treatment: VariantResult
    comparison: ABComparison
    rule_projections: tuple[RuleProjection, ...]


class MechanismCandidate(BaseModel):
    """Mechanism candidate for Exp-3 bake-off."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism_id: str
    label: str
    covered_rules: tuple[RuleId, ...]


class MechanismResult(BaseModel):
    """Mechanism bake-off score for one candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism_id: str
    label: str
    covered_rules: tuple[RuleId, ...]
    prevented_violation_count: int = Field(ge=0)
    weighted_prevented_violation_count: int = Field(ge=0)
    projected_pass_rate: float = Field(ge=0.0, le=1.0)
    rule_projections: tuple[RuleProjection, ...]


class MechanismBakeoffReport(BaseModel):
    """Exp-3 mechanism bake-off report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str = "Exp-3"
    name: Literal["mechanism_bakeoff"] = "mechanism_bakeoff"
    candidates: tuple[MechanismResult, ...]
    winner_id: str | None


class RuleAdherenceExperimentSuite(BaseModel):
    """Exp-1..Exp-3 suite over the recorded rule-adherence corpus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_ids: tuple[str, ...]
    corpus_count: int = Field(ge=0)
    verify_count: int = Field(ge=0)
    violation_mining: ViolationMiningReport
    tiered_ab: TieredABReport
    mechanism_bakeoff: MechanismBakeoffReport


DEFAULT_MECHANISM_CANDIDATES: tuple[MechanismCandidate, ...] = (
    MechanismCandidate(
        mechanism_id="prompt_checklist",
        label="Prompt checklist",
        covered_rules=(
            "commands_use_rtk",
            "python_invocations_use_uv_run",
            "pydantic_models_forbid_extra",
        ),
    ),
    MechanismCandidate(
        mechanism_id="static_artifact_scan",
        label="Static artifact scan",
        covered_rules=("no_local_path_leaks", "pydantic_models_forbid_extra"),
    ),
    MechanismCandidate(
        mechanism_id="runtime_guardrails",
        label="Runtime guardrails",
        covered_rules=(
            "commands_use_rtk",
            "python_invocations_use_uv_run",
            "no_direct_state_json_edits",
            "no_destructive_git_reverts",
        ),
    ),
)


def _materialise_cases(
    cases: Iterable[RecordedWaveCase | Mapping[str, object]],
) -> tuple[RecordedWaveCase | Mapping[str, object], ...]:
    return tuple(cases)


def _score_reports(
    cases: Iterable[RecordedWaveCase | Mapping[str, object]],
) -> tuple[RuleAdherenceReport, ...]:
    return tuple(check_rule_adherence(case) for case in cases)


def _failed_case_ids(rule_id: RuleId, reports: Sequence[RuleAdherenceReport]) -> tuple[str, ...]:
    return tuple(report.case_id for report in reports if rule_id in report.failed_rules)


def _rule_pass_rate(rule_id: RuleId, reports: Sequence[RuleAdherenceReport]) -> float:
    if not reports:
        return 1.0
    return (len(reports) - len(_failed_case_ids(rule_id, reports))) / len(reports)


def _baseline_rows(reports: Sequence[RuleAdherenceReport]) -> tuple[RuleBaselineRow, ...]:
    total_cases = len(reports)
    rows: list[RuleBaselineRow] = []
    for rule_id in RULE_IDS:
        failed_case_ids = _failed_case_ids(rule_id, reports)
        fail_count = len(failed_case_ids)
        pass_count = total_cases - fail_count
        rows.append(
            RuleBaselineRow(
                rule_id=rule_id,
                tier=_RULE_TIERS[rule_id],
                total_cases=total_cases,
                pass_count=pass_count,
                fail_count=fail_count,
                pass_rate=pass_count / total_cases if total_cases else 1.0,
                failed_case_ids=failed_case_ids,
            )
        )
    return tuple(rows)


def _top_violations(rows: Sequence[RuleBaselineRow]) -> tuple[MinedViolation, ...]:
    violations = (
        MinedViolation(
            rule_id=row.rule_id,
            tier=row.tier,
            fail_count=row.fail_count,
            failed_case_ids=row.failed_case_ids,
        )
        for row in rows
        if row.fail_count > 0
    )
    return tuple(sorted(violations, key=lambda item: (-item.fail_count, item.rule_id)))


def _total_checks(reports: Sequence[RuleAdherenceReport]) -> int:
    return sum(report.checker_count for report in reports)


def _baseline_failed_count(reports: Sequence[RuleAdherenceReport]) -> int:
    return sum(len(report.failed_rules) for report in reports)


def _prevented_violation_count(
    reports: Sequence[RuleAdherenceReport],
    prevented_rules: frozenset[RuleId],
) -> int:
    return sum(
        1 for report in reports for rule_id in report.failed_rules if rule_id in prevented_rules
    )


def _weighted_prevented_violation_count(
    reports: Sequence[RuleAdherenceReport],
    prevented_rules: frozenset[RuleId],
) -> int:
    return sum(
        _RULE_WEIGHTS[rule_id]
        for report in reports
        for rule_id in report.failed_rules
        if rule_id in prevented_rules
    )


def _projected_passed_checks(
    reports: Sequence[RuleAdherenceReport],
    prevented_rules: frozenset[RuleId],
) -> int:
    baseline_passed = _total_checks(reports) - _baseline_failed_count(reports)
    return baseline_passed + _prevented_violation_count(reports, prevented_rules)


def _projected_pass_rate(
    reports: Sequence[RuleAdherenceReport],
    prevented_rules: frozenset[RuleId],
) -> float:
    total_checks = _total_checks(reports)
    if total_checks == 0:
        return 1.0
    return _projected_passed_checks(reports, prevented_rules) / total_checks


def _projected_rule_pass_rate(
    rule_id: RuleId,
    reports: Sequence[RuleAdherenceReport],
    prevented_rules: frozenset[RuleId],
) -> float:
    if rule_id in prevented_rules:
        return 1.0
    return _rule_pass_rate(rule_id, reports)


def _rule_pass_rates(
    reports: Sequence[RuleAdherenceReport],
    prevented_rules: frozenset[RuleId],
) -> dict[RuleId, float]:
    return {
        rule_id: _projected_rule_pass_rate(rule_id, reports, prevented_rules)
        for rule_id in RULE_IDS
    }


def _rule_projections(
    reports: Sequence[RuleAdherenceReport],
    prevented_rules: frozenset[RuleId],
) -> tuple[RuleProjection, ...]:
    projections: list[RuleProjection] = []
    for rule_id in RULE_IDS:
        baseline_rate = _rule_pass_rate(rule_id, reports)
        projected_rate = _projected_rule_pass_rate(rule_id, reports, prevented_rules)
        projections.append(
            RuleProjection(
                rule_id=rule_id,
                tier=_RULE_TIERS[rule_id],
                baseline_pass_rate=baseline_rate,
                projected_pass_rate=projected_rate,
                delta_pp=(projected_rate - baseline_rate) * 100.0,
            )
        )
    return tuple(projections)


def _variant_result(
    *,
    variant_id: str,
    label: str,
    reports: Sequence[RuleAdherenceReport],
    prevented_rules: frozenset[RuleId],
) -> VariantResult:
    total_checks = _total_checks(reports)
    passed_checks = _projected_passed_checks(reports, prevented_rules)
    return VariantResult(
        variant_id=variant_id,
        label=label,
        prevented_rules=tuple(rule_id for rule_id in RULE_IDS if rule_id in prevented_rules),
        prevented_violation_count=_prevented_violation_count(reports, prevented_rules),
        passed_checks=passed_checks,
        total_checks=total_checks,
        pass_rate=passed_checks / total_checks if total_checks else 1.0,
        rule_pass_rates=_rule_pass_rates(reports, prevented_rules),
    )


def _as_decimal(value: float) -> Decimal:
    return Decimal(str(value))


def meets_ab_threshold(
    control_pass_rate: float,
    treatment_pass_rate: float,
    *,
    min_delta_rate: float = AB_MIN_DELTA_RATE,
) -> bool:
    """Return whether treatment beats control by at least the configured rate delta."""
    if min_delta_rate < 0.0:
        raise ValueError(f"min_delta_rate must be >= 0: {min_delta_rate!r}")
    delta = _as_decimal(treatment_pass_rate) - _as_decimal(control_pass_rate)
    return delta >= _as_decimal(min_delta_rate)


def compare_ab_variants(
    control: VariantResult,
    treatment: VariantResult,
    *,
    min_delta_rate: float = AB_MIN_DELTA_RATE,
) -> ABComparison:
    """Compare two variants using the configured A/B promotion threshold."""
    delta_rate_decimal = _as_decimal(treatment.pass_rate) - _as_decimal(control.pass_rate)
    delta_rate = float(delta_rate_decimal)
    return ABComparison(
        control_id=control.variant_id,
        treatment_id=treatment.variant_id,
        min_delta_rate=min_delta_rate,
        min_delta_pp=min_delta_rate * 100.0,
        control_pass_rate=control.pass_rate,
        treatment_pass_rate=treatment.pass_rate,
        delta_rate=delta_rate,
        delta_pp=delta_rate * 100.0,
        meets_threshold=meets_ab_threshold(
            control.pass_rate,
            treatment.pass_rate,
            min_delta_rate=min_delta_rate,
        ),
    )


def run_violation_mining(
    cases: Iterable[RecordedWaveCase | Mapping[str, object]],
) -> ViolationMiningReport:
    """Run Exp-1 violation mining over recorded wave cases."""
    materialised = _materialise_cases(cases)
    reports = _score_reports(materialised)
    baseline = build_rule_adherence_baseline(materialised, experiment_id="Exp-1")
    rows = _baseline_rows(reports)
    return ViolationMiningReport(
        corpus_count=baseline.corpus_count,
        verify_count=baseline.verify_count,
        baseline_pass_rate=baseline.pass_rate,
        per_rule_baselines=rows,
        top_violations=_top_violations(rows),
    )


def run_tiered_ab(cases: Iterable[RecordedWaveCase | Mapping[str, object]]) -> TieredABReport:
    """Run Exp-2 tiered A/B projection over recorded wave cases."""
    reports = _score_reports(_materialise_cases(cases))
    control = _variant_result(
        variant_id="control",
        label="Advisory-only baseline",
        reports=reports,
        prevented_rules=frozenset(),
    )
    treatment = _variant_result(
        variant_id="tiered_blocking",
        label="Blocking tier prevents high-severity violations",
        reports=reports,
        prevented_rules=_BLOCKING_RULES,
    )
    return TieredABReport(
        control=control,
        treatment=treatment,
        comparison=compare_ab_variants(control, treatment),
        rule_projections=_rule_projections(reports, _BLOCKING_RULES),
    )


def run_mechanism_bakeoff(
    cases: Iterable[RecordedWaveCase | Mapping[str, object]],
    *,
    candidates: Sequence[MechanismCandidate] = DEFAULT_MECHANISM_CANDIDATES,
) -> MechanismBakeoffReport:
    """Run Exp-3 mechanism bake-off over recorded wave cases."""
    reports = _score_reports(_materialise_cases(cases))
    results: list[MechanismResult] = []
    for candidate in candidates:
        covered_rules = frozenset(candidate.covered_rules)
        results.append(
            MechanismResult(
                mechanism_id=candidate.mechanism_id,
                label=candidate.label,
                covered_rules=candidate.covered_rules,
                prevented_violation_count=_prevented_violation_count(reports, covered_rules),
                weighted_prevented_violation_count=_weighted_prevented_violation_count(
                    reports,
                    covered_rules,
                ),
                projected_pass_rate=_projected_pass_rate(reports, covered_rules),
                rule_projections=_rule_projections(reports, covered_rules),
            )
        )
    ordered = tuple(
        sorted(
            results,
            key=lambda item: (
                -item.weighted_prevented_violation_count,
                -item.prevented_violation_count,
                -item.projected_pass_rate,
                item.mechanism_id,
            ),
        )
    )
    winner_id = ordered[0].mechanism_id if ordered else None
    return MechanismBakeoffReport(candidates=ordered, winner_id=winner_id)


def run_rule_adherence_experiments(
    cases: Iterable[RecordedWaveCase | Mapping[str, object]],
) -> RuleAdherenceExperimentSuite:
    """Run Exp-1..Exp-3 over recorded wave cases."""
    materialised = _materialise_cases(cases)
    mining = run_violation_mining(materialised)
    tiered_ab = run_tiered_ab(materialised)
    mechanism_bakeoff = run_mechanism_bakeoff(materialised)
    return RuleAdherenceExperimentSuite(
        experiment_ids=(
            mining.experiment_id,
            tiered_ab.experiment_id,
            mechanism_bakeoff.experiment_id,
        ),
        corpus_count=mining.corpus_count,
        verify_count=mining.verify_count,
        violation_mining=mining,
        tiered_ab=tiered_ab,
        mechanism_bakeoff=mechanism_bakeoff,
    )


def run_recorded_rule_adherence_experiments(input_dir: Path) -> RuleAdherenceExperimentSuite:
    """Load a recorded corpus directory and run Exp-1..Exp-3."""
    return run_rule_adherence_experiments(load_recorded_wave_cases(input_dir))


def dump_rule_adherence_experiments(report: RuleAdherenceExperimentSuite) -> str:
    """Return deterministic JSON for Exp-1..Exp-3 output."""
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


__all__ = [
    "AB_MIN_DELTA_PP",
    "AB_MIN_DELTA_RATE",
    "DEFAULT_MECHANISM_CANDIDATES",
    "ABComparison",
    "MechanismBakeoffReport",
    "MechanismCandidate",
    "MechanismResult",
    "MinedViolation",
    "RuleAdherenceExperimentSuite",
    "RuleBaselineRow",
    "RuleProjection",
    "TieredABReport",
    "VariantResult",
    "ViolationMiningReport",
    "compare_ab_variants",
    "dump_rule_adherence_experiments",
    "meets_ab_threshold",
    "run_mechanism_bakeoff",
    "run_recorded_rule_adherence_experiments",
    "run_rule_adherence_experiments",
    "run_tiered_ab",
    "run_violation_mining",
]
