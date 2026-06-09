"""Tests for the EAWF021 measurability lint and EAWF022 propose-coverage gate.

EAWF021 rejects an unmeasurable success criterion via two legs: a banned-vague
token (caught even at the 20-char length floor, proving the floor alone is
insufficient) and a missing observation contract (no typed response clause and
no parseable verb plus proof locus). The rewritten measurable form passes both
legs. EAWF022 surfaces each silently-dropped brief span (coverage_diff
``uncovered``) as a finding. Both compose behind the ``validate_prose``
chokepoint with the existing fail-open / fail-closed switch.
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.common import (
    CriterionSpec,
    DeferredDeliverable,
    ObserveVerb,
    ProofLocus,
    QualityDimension,
    ResponseClause,
    SourceUnit,
    grandfather_criterion,
)
from eawf.platform.lint.eawf021_measurable_criterion import (
    BANNED_VAGUE_TOKENS,
    check_criterion,
    check_criterion_spec,
)
from eawf.platform.lint.eawf021_measurable_criterion import (
    RULE_CODE as EAWF021_CODE,
)
from eawf.platform.lint.eawf021_measurable_criterion import (
    check_source as check_eawf021_source,
)
from eawf.platform.lint.eawf022_propose_coverage import (
    RULE_CODE as EAWF022_CODE,
)
from eawf.platform.lint.eawf022_propose_coverage import (
    check_coverage,
)
from eawf.platform.lint.eawf022_propose_coverage import (
    check_source as check_eawf022_source,
)
from eawf.platform.lint.validate_prose import COMPOSED_RULES, validate_prose
from eawf.workflow.lifecycle._errors import (
    LifecycleError,
    check_criteria_measurability,
)
from eawf.workflow.propose.generator import extract_units


def _reasons(violations: list) -> list[str]:
    return [v.reason for v in violations]


# ---- EAWF021: banned-vague-token leg ----------------------------------------


def test_check_criterion_flags_vague_works_properly() -> None:
    # CR-1 (returns, T4): the vague form is rejected.
    violations = check_criterion("the widget works properly")
    assert any("banned vague token" in r for r in _reasons(violations))


def test_check_criterion_vague_phrase_at_length_floor_still_caught() -> None:
    # Boundary: "works as expected" is exactly 17 chars; a 20-char-padded vague
    # signal clears the CriterionSpec measurable_signal floor yet still fails.
    signal = "works as expected   "
    assert len(signal) == 20
    violations = check_criterion(
        "returns 200 for a valid request; pytest tests/x.py::test_ok",
        response=ResponseClause(
            observe=ObserveVerb.RETURNS, object="200 for a valid request", locus=ProofLocus.PYTEST
        ),
        measurable_signal=signal,
    )
    # The length floor would pass it; the vague-token leg still catches it.
    assert any("banned vague token" in r for r in _reasons(violations))


def test_check_criterion_each_banned_token_is_caught() -> None:
    for token in BANNED_VAGUE_TOKENS:
        violations = check_criterion(
            f"the request {token} when the daemon restarts and emits a log; log_capture"
        )
        assert any(v.code == EAWF021_CODE for v in violations), token


def test_check_criterion_vague_match_is_case_insensitive() -> None:
    violations = check_criterion("the WIDGET Works PROPERLY now; returns ok; pytest")
    assert any("banned vague token" in r for r in _reasons(violations))


def test_check_criterion_word_boundary_does_not_false_positive() -> None:
    # "framework" must not match "works"; this criterion has a verb + locus and
    # no banned token, so it is clean.
    violations = check_criterion("the framework returns 200; pytest tests/x.py::test_ok")
    assert violations == []


# ---- EAWF021: missing-observation-contract leg ------------------------------


def test_check_criterion_missing_verb_and_locus_is_unmeasurable() -> None:
    # No response clause AND no parseable verb/locus -> unmeasurable.
    violations = check_criterion("the dashboard shows the right numbers")
    assert any("unmeasurable criterion" in r for r in _reasons(violations))


def test_check_criterion_measurable_verb_plus_locus_passes() -> None:
    # CR-1 (returns, T4): the rewritten measurable form passes both legs.
    violations = check_criterion("returns 200 for a valid request; pytest tests/x.py::test_ok")
    assert violations == []


def test_check_criterion_typed_response_satisfies_contract_leg() -> None:
    # A typed response clause completes the observation contract even when the
    # text alone carries no parseable verb/locus.
    violations = check_criterion(
        "the create path persists the row",
        response=ResponseClause(
            observe=ObserveVerb.RETURNS, object="the persisted row", locus=ProofLocus.PYTEST
        ),
    )
    assert violations == []


def test_check_criterion_same_text_fails_then_passes_when_rewritten() -> None:
    # Error-path: a banned phrase fails, the rewritten measurable form passes.
    vague = check_criterion("the importer works as expected")
    assert vague != []
    rewritten = check_criterion("returns 0 rows on an empty file; cli_exit 0")
    assert rewritten == []


# ---- EAWF021: typed CriterionSpec adapter -----------------------------------


def _spec(text: str, signal: str, *, response: ResponseClause | None = None) -> CriterionSpec:
    return CriterionSpec(
        id="CR-01",
        text=text,
        kind="functional",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal=signal,
        response=response,
    )


def test_check_criterion_spec_flags_vague_signal() -> None:
    spec = _spec(
        "returns 200 for a valid request",
        "the endpoint is performant under load",
        response=ResponseClause(observe=ObserveVerb.RETURNS, object="200", locus=ProofLocus.PYTEST),
    )
    violations = check_criterion_spec(spec)
    assert any("banned vague token" in r for r in _reasons(violations))


def test_check_criterion_spec_clean_for_measurable_row() -> None:
    spec = _spec(
        "returns 200 for a valid request; pytest tests/x.py::test_ok",
        "exit code 0 on a clean corpus; cli_exit",
        response=ResponseClause(observe=ObserveVerb.RETURNS, object="200", locus=ProofLocus.PYTEST),
    )
    assert check_criterion_spec(spec) == []


# ---- EAWF021 binding at the wave-plan boundary (check_criteria_measurability) -


def test_check_criteria_measurability_flags_unmeasurable_authored_row() -> None:
    # An authored (non-legacy) row with no observation contract raises, naming
    # the EAWF021 finding body.
    spec = _spec("the widget works properly", "the widget works properly under load")
    with pytest.raises(LifecycleError, match="unmeasurable success criteria") as exc:
        check_criteria_measurability([spec], entity_kind="wave", entity_id="P01-I01-W01")
    assert EAWF021_CODE in str(exc.value)


def test_check_criteria_measurability_exempts_grandfathered_legacy_row() -> None:
    # A grandfathered legacy row carries no observation contract by
    # construction, yet the exemption keeps it clean so existing waves
    # round-trip (no raise).
    legacy = grandfather_criterion("ship the thing", index=1)
    assert legacy.kind == "legacy"
    check_criteria_measurability([legacy], entity_kind="wave", entity_id="P01-I01-W01")


def test_check_criteria_measurability_clean_for_measurable_authored_row() -> None:
    spec = _spec(
        "returns 200 for a valid request; pytest tests/x.py::test_ok",
        "exit code 0 on a clean corpus; cli_exit",
        response=ResponseClause(observe=ObserveVerb.RETURNS, object="200", locus=ProofLocus.PYTEST),
    )
    check_criteria_measurability([spec], entity_kind="wave", entity_id="P01-I01-W01")


# ---- EAWF022: propose coverage gate -----------------------------------------


def test_check_coverage_surfaces_uncovered_span_as_finding() -> None:
    # Error-path: a span covered by neither a criterion nor a deferral surfaces.
    units = [
        SourceUnit(span_id="U-000", quote="first detail", char_offset=0),
        SourceUnit(span_id="U-001", quote="dropped detail", char_offset=20),
    ]
    violations = check_coverage(units, covered_span_ids={"U-000"}, deferrals=[])
    assert len(violations) == 1
    assert violations[0].code == EAWF022_CODE
    assert violations[0].snippet == "U-001"
    assert "dropped" in violations[0].reason


def test_check_coverage_clean_when_all_spans_covered() -> None:
    units = [
        SourceUnit(span_id="U-000", quote="first detail", char_offset=0),
        SourceUnit(span_id="U-001", quote="second detail", char_offset=20),
    ]
    violations = check_coverage(units, covered_span_ids={"U-000", "U-001"}, deferrals=[])
    assert violations == []


def test_check_coverage_deferred_span_is_not_a_finding() -> None:
    units = [SourceUnit(span_id="U-000", quote="filed elsewhere", char_offset=0)]
    deferral = DeferredDeliverable(
        span_id="U-000",
        reason="tracked in the next phase backlog item",
        target="P30",
    )
    assert check_coverage(units, covered_span_ids=set(), deferrals=[deferral]) == []


def test_check_coverage_over_extracted_brief_spans() -> None:
    # End-to-end: extract a brief, cover the first span only, drop the rest.
    brief = "First claim holds. Second claim is dropped. Third claim is dropped."
    units = extract_units(brief)
    assert len(units) == 3
    violations = check_coverage(units, covered_span_ids={"U-000"}, deferrals=[])
    assert {v.snippet for v in violations} == {"U-001", "U-002"}


def test_check_eawf022_source_is_a_noop_over_prose() -> None:
    # Coverage needs typed inputs; the prose adapter is always empty.
    assert check_eawf022_source("any prose with a dropped detail\n") == []


# ---- composition behind validate_prose --------------------------------------


def test_composed_rules_lists_eawf021_and_eawf022() -> None:
    assert COMPOSED_RULES == ("EAWF013", "EAWF014", "EAWF017", "EAWF021", "EAWF022")


def test_validate_prose_aggregates_eawf021_vague_token() -> None:
    report = validate_prose("The importer works properly under load.\n")
    assert "EAWF021" in report.codes()


def test_validate_prose_eawf021_fail_open_is_advisory() -> None:
    # CR-2 contract mirror: fail-open surfaces the finding but exit stays 0.
    report = validate_prose("The importer works properly.\n", strict=False)
    assert report.has_findings
    assert report.exit_code == 0


def test_validate_prose_eawf021_fail_closed_blocks() -> None:
    report = validate_prose("The importer works properly.\n", strict=True)
    assert report.exit_code == 1
    assert "EAWF021" in report.codes()


def test_validate_prose_clean_measurable_corpus_runs_clean() -> None:
    # CR-2 (exits, T2, cli_exit==0): a measurable corpus runs clean in both
    # modes with EAWF021/022 registered.
    corpus = (
        "Returns 200 for a valid request, verified by pytest [a].\n"
        "\n"
        "## References\n"
        "\n"
        "[a] `tests/x.py:42`\n"
    )
    for strict in (False, True):
        report = validate_prose(corpus, strict=strict)
        assert not report.has_findings, report.render()
        assert report.exit_code == 0


def test_validate_prose_eawf021_source_skips_fenced_code() -> None:
    # A vague word inside a fenced example is exempt.
    source = "```\nthe widget works properly\n```\n"
    assert check_eawf021_source(source) == []
