"""Tests for the EAWF021 measurability lint.

EAWF021 rejects an unmeasurable success criterion via two legs: a banned-vague
token (caught even at the 20-char length floor, proving the floor alone is
insufficient) and a missing observation contract (no typed response clause and
no parseable verb plus proof locus). The rewritten measurable form passes both
legs. The lint composes behind the ``validate_prose`` chokepoint with the
existing fail-open / fail-closed switch.
"""

from __future__ import annotations

from eawf.kernel.spec.common import (
    CriterionSpec,
    ObserveVerb,
    ProofLocus,
    QualityDimension,
    ResponseClause,
)
from eawf.platform.lint.eawf021_measurable_criterion import (
    BANNED_VAGUE_TOKENS,
    check_criterion,
)
from eawf.platform.lint.eawf021_measurable_criterion import (
    RULE_CODE as EAWF021_CODE,
)
from eawf.platform.lint.eawf021_measurable_criterion import (
    check_source as check_eawf021_source,
)
from eawf.platform.lint.validate_prose import COMPOSED_RULES, validate_prose


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


# ---- composition behind validate_prose --------------------------------------


def test_composed_rules_lists_eawf021() -> None:
    assert COMPOSED_RULES == ("EAWF013", "EAWF014", "EAWF017", "EAWF021")


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
    # modes with EAWF021 registered.
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
