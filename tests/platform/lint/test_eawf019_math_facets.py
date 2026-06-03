"""Tests for the EAWF019 math-explainer facet-presence + resolution lint.

Covers the four structural checks over a typed
:class:`~eawf.kernel.spec.math.MathExplainer`:

1. facet-presence — a claim whose example gate is not a runnable
   ``command_exit_zero`` is flagged (the runnable-example facet is dead);
2. citation-resolution — a citation that resolves to no reference row (a
   nonexistent path, a malformed URN, an empty ref, a non-portable URL) is
   flagged, while a resolving URN / disk path / portable URL passes;
3. collected-gate — the kappa regression: a pytest example gate targeting a
   ``.py`` file the runner will not collect (basename matches no
   ``python_files`` glob) is flagged;
4. formula well-formedness — an unbalanced ``$`` / ``$$`` delimiter or brace in
   the statement / intuition is flagged.

Plus the zero-claim (model-rejected) and single-claim boundaries and a clean
explainer passing all four checks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.math import MathExplainer
from eawf.platform.lint.eawf019_math_facets import (
    DEFAULT_PYTHON_FILES,
    RULE_CODE,
    MathFacetViolation,
    check_explainer,
)

# A known-clean argv: an allowlisted wrapper (``uv run``) + tool (``pytest``)
# that passes the L0 argv-policy the GateSpec model-validator enforces, and
# targets a ``*_test.py`` file pytest will collect.
_COLLECTED_ARGV = ["uv", "run", "pytest", "tests/explainer_snippets_test.py", "-q"]


def _runnable_gate(argv: list[str] | None = None, gate_id: str = "G1") -> dict[str, Any]:
    """Return a runnable ``command_exit_zero`` GateSpec payload (clean argv)."""
    return {
        "id": gate_id,
        "criterion_id": "C1",
        "kind": "command_exit_zero",
        "args": {"argv": list(argv if argv is not None else _COLLECTED_ARGV)},
        "policy": "block",
        "cadence": "every-wave",
    }


def _non_runnable_gate(gate_id: str = "G1") -> dict[str, Any]:
    """Return a non-``command_exit_zero`` GateSpec payload (not runnable)."""
    return {
        "id": gate_id,
        "criterion_id": "C1",
        "kind": "regex_match",
        "args": {"pattern": r"^OK$", "input": "OK"},
        "policy": "block",
        "cadence": "every-wave",
    }


def _citation(ref: str = "src/eawf/kernel/spec/math.py", kind: str = "artifact") -> dict[str, Any]:
    """Return an EvidenceRef payload with the given ``ref`` + ``kind``."""
    return {"kind": kind, "ref": ref, "summary": "canonical source for the claim"}


def _claim_payload(**overrides: Any) -> dict[str, Any]:
    """Return a minimal-valid MathClaim payload with all four facets present.

    The default citation points at a real repo file so the citation-resolution
    check passes against a ``project_root`` of the repo root, and the default
    gate targets a ``*_test.py`` file so the collected-gate check passes.
    """
    payload: dict[str, Any] = {
        "statement": "the hedge delta equals (C_S - C/S)/S",
        "intuition": "the hedge ratio is the spot sensitivity net of payoff scaling",
        "example_gate": _runnable_gate(),
        "regime": "real positive spot; continuous payoff differentiable in S",
        "assumptions": ["spot S > 0"],
        "citation": _citation(),
        "verifier": "sympy+mpmath",
        "assurance": "refute",
    }
    payload.update(overrides)
    return payload


def _explainer(**claim_overrides: Any) -> MathExplainer:
    """Build a one-claim MathExplainer; claim overrides flow to the single claim."""
    return MathExplainer.model_validate(
        {
            "title": "Inverse-payoff hedge delta",
            "slug": "inverse-payoff-delta",
            "claims": [_claim_payload(**claim_overrides)],
        }
    )


@pytest.fixture
def repo_root() -> Path:
    """Return the repository root the citation disk-exists check resolves against."""
    # tests/platform/lint/test_eawf019_math_facets.py -> repo root is parents[3].
    return Path(__file__).resolve().parents[3]


def _reasons(violations: list[MathFacetViolation]) -> list[str]:
    return [v.reason for v in violations]


# ---- clean explainer passes -------------------------------------------------


def test_clean_explainer_passes(repo_root: Path) -> None:
    """A fully-grounded, well-formed single-claim explainer yields no findings."""
    assert check_explainer(_explainer(), project_root=repo_root) == []


# ---- 1. facet-presence ------------------------------------------------------


def test_missing_runnable_facet_flagged(repo_root: Path) -> None:
    """A non-runnable example gate flags the missing runnable-example facet."""
    explainer = _explainer(example_gate=_non_runnable_gate())
    reasons = _reasons(check_explainer(explainer, project_root=repo_root))
    assert any("missing the runnable-example facet" in r for r in reasons)


def test_missing_facet_finding_is_eawf019(repo_root: Path) -> None:
    """The facet-presence finding carries the EAWF019 code."""
    explainer = _explainer(example_gate=_non_runnable_gate())
    violations = check_explainer(explainer, project_root=repo_root)
    assert violations[0].code == RULE_CODE == "EAWF019"


# ---- 2. citation-resolution -------------------------------------------------


def test_unresolved_citation_path_flagged(repo_root: Path) -> None:
    """A citation pointing at a nonexistent repo path is flagged."""
    explainer = _explainer(citation=_citation(ref="src/eawf/does/not/exist.py"))
    reasons = _reasons(check_explainer(explainer, project_root=repo_root))
    assert any("resolves to no reference row" in r for r in reasons)


def test_unresolved_citation_malformed_urn_flagged(repo_root: Path) -> None:
    """A malformed URN citation is flagged (grammar fails)."""
    explainer = _explainer(citation=_citation(ref="urn:eawf:v1:not-a-real-kind:X/Y"))
    reasons = _reasons(check_explainer(explainer, project_root=repo_root))
    assert any("resolves to no reference row" in r for r in reasons)


def test_resolving_urn_citation_passes(repo_root: Path) -> None:
    """A well-formed artifact URN citation resolves (grammar-only) and passes."""
    explainer = _explainer(citation=_citation(ref="urn:eawf:v1:artifact:QR/ART-x"))
    assert check_explainer(explainer, project_root=repo_root) == []


def test_resolving_external_url_citation_passes(repo_root: Path) -> None:
    """A portable http(s) URL citation resolves structurally and passes.

    The bare deterministic resolver would route a URL to the disk-exists check
    (which a URL always fails); EAWF019 resolves an ``external_url`` citation
    via portability instead, so a real arXiv URL is not a false positive.
    """
    explainer = _explainer(
        citation=_citation(ref="https://arxiv.org/abs/2503.21934", kind="external_url")
    )
    assert check_explainer(explainer, project_root=repo_root) == []


def test_non_portable_url_citation_flagged(repo_root: Path) -> None:
    """A local-host URL citation is not portable and is flagged."""
    explainer = _explainer(
        citation=_citation(ref="http://localhost:8000/paper", kind="external_url")
    )
    reasons = _reasons(check_explainer(explainer, project_root=repo_root))
    assert any("not portable" in r for r in reasons)


# ---- 3. collected-gate (the kappa regression) ------------------------------


def test_uncollected_gate_flagged(repo_root: Path) -> None:
    """A pytest gate targeting a file pytest will not collect is flagged.

    ``explainer_snippets.py`` matches neither ``test_*.py`` nor ``*_test.py``,
    so pytest collects zero tests and the gate passes silently — the exact
    kappa failure mode EAWF019 catches.
    """
    explainer = _explainer(
        example_gate=_runnable_gate(
            argv=["uv", "run", "pytest", "tests/explainer_snippets.py", "-q"]
        )
    )
    reasons = _reasons(check_explainer(explainer, project_root=repo_root))
    assert any("the runner will not collect" in r for r in reasons)


def test_collected_gate_finding_names_uncollected_target(repo_root: Path) -> None:
    """The collected-gate finding snippet names the uncollected target file."""
    explainer = _explainer(
        example_gate=_runnable_gate(argv=["uv", "run", "pytest", "tests/explainer_snippets.py"])
    )
    collected = [
        v for v in check_explainer(explainer, project_root=repo_root) if "collect" in v.reason
    ]
    assert len(collected) == 1
    assert "tests/explainer_snippets.py" in collected[0].snippet


def test_collected_gate_passes_for_test_prefixed_target(repo_root: Path) -> None:
    """A ``test_*.py`` pytest target collects, so the collected-gate check passes."""
    explainer = _explainer(
        example_gate=_runnable_gate(argv=["uv", "run", "pytest", "tests/test_snippets.py"])
    )
    assert check_explainer(explainer, project_root=repo_root) == []


def test_collected_gate_skips_non_pytest_verifier(repo_root: Path) -> None:
    """A non-pytest verifier has no pytest target, so it is not flagged.

    The collected-gate check applies only to pytest invocations; a
    ``command_exit_zero`` gate shelling a different (allowlisted) verifier —
    here a ruff-style check standing in for a CAS / units / SMT check — carries
    no ``python_files`` obligation.
    """
    explainer = _explainer(
        example_gate=_runnable_gate(argv=["uv", "run", "ruff", "check", "src/eawf"])
    )
    collected = [
        v for v in check_explainer(explainer, project_root=repo_root) if "collect" in v.reason
    ]
    assert collected == []


def test_default_python_files_globs() -> None:
    """The collected-gate check defaults to pytest's built-in collection globs."""
    assert DEFAULT_PYTHON_FILES == ("test_*.py", "*_test.py")


def test_custom_python_files_collects_snippet_pattern(repo_root: Path) -> None:
    """A broadened ``python_files`` (matching ``*_snippets.py``) un-flags the gate.

    Mirrors the kappa fix's alternative: broaden ``python_files`` instead of
    renaming. With the broadened glob the previously-uncollected target now
    collects, so the collected-gate check passes.
    """
    explainer = _explainer(
        example_gate=_runnable_gate(argv=["uv", "run", "pytest", "tests/explainer_snippets.py"])
    )
    violations = check_explainer(
        explainer,
        project_root=repo_root,
        python_files=("test_*.py", "*_test.py", "*_snippets.py"),
    )
    assert [v for v in violations if "collect" in v.reason] == []


# ---- 4. formula well-formedness --------------------------------------------


def test_malformed_latex_unbalanced_dollar_flagged(repo_root: Path) -> None:
    """An unbalanced single ``$`` math delimiter in the statement is flagged."""
    explainer = _explainer(statement="the delta is $\\frac{dC}{dS} with no closing delimiter")
    reasons = _reasons(check_explainer(explainer, project_root=repo_root))
    assert any("malformed math in statement" in r for r in reasons)


def test_malformed_latex_unbalanced_brace_flagged(repo_root: Path) -> None:
    """An unbalanced brace inside a math span is flagged."""
    explainer = _explainer(statement="the ratio is $\\frac{dC}{dS$")
    reasons = _reasons(check_explainer(explainer, project_root=repo_root))
    assert any("malformed math" in r and "brace" in r for r in reasons)


def test_malformed_latex_in_intuition_flagged(repo_root: Path) -> None:
    """Malformed math in the intuition (not just the statement) is flagged."""
    explainer = _explainer(
        intuition="the hedge ratio $\\partial_S C$ scales the $unclosed inline math"
    )
    reasons = _reasons(check_explainer(explainer, project_root=repo_root))
    assert any("malformed math in intuition" in r for r in reasons)


def test_balanced_latex_passes(repo_root: Path) -> None:
    """A claim with balanced ``$...$`` math and braces passes the formula check."""
    explainer = _explainer(
        statement="the hedge delta is $\\frac{\\partial C}{\\partial S}$ at the money"
    )
    assert check_explainer(explainer, project_root=repo_root) == []


def test_display_math_balanced_passes(repo_root: Path) -> None:
    """Balanced ``$$...$$`` display math passes."""
    explainer = _explainer(statement="the SDE is $$dS = \\mu S dt + \\sigma S dW$$ under P")
    assert check_explainer(explainer, project_root=repo_root) == []


# ---- boundaries: zero-claim (model-rejected) + single-claim -----------------


def test_zero_claim_explainer_rejected_at_construction() -> None:
    """An explainer with no claims fails the model bound before any lint runs.

    The zero-claim boundary is the model's responsibility (``min_length=1`` on
    ``claims``); EAWF019 never sees an empty explainer, so the boundary is
    pinned at construction.
    """
    with pytest.raises(ValueError, match="claims"):
        MathExplainer.model_validate({"title": "Empty", "slug": "empty", "claims": []})


def test_single_claim_boundary_clean(repo_root: Path) -> None:
    """The single-claim explainer (the minimum) passes when grounded + well-formed."""
    violations = check_explainer(_explainer(), project_root=repo_root)
    assert violations == []


def test_multiple_findings_one_per_failing_check(repo_root: Path) -> None:
    """A claim failing several checks yields one finding per failing check.

    A non-runnable gate trips facet-presence; a nonexistent citation trips
    citation-resolution; a malformed statement trips the formula check — three
    distinct findings on the one claim. (The collected-gate check is skipped
    because the gate is not runnable.)
    """
    explainer = _explainer(
        example_gate=_non_runnable_gate(),
        citation=_citation(ref="src/eawf/does/not/exist.py"),
        statement="broken $ math",
    )
    violations = check_explainer(explainer, project_root=repo_root)
    reasons = _reasons(violations)
    assert any("runnable-example facet" in r for r in reasons)
    assert any("resolves to no reference row" in r for r in reasons)
    assert any("malformed math" in r for r in reasons)
    assert all(v.claim_index == 0 for v in violations)


# ---- render shape -----------------------------------------------------------


def test_violation_render_shape(repo_root: Path) -> None:
    """The render is a ``<claim_index>:<col>: EAWF019 reason: snippet`` one-liner."""
    explainer = _explainer(example_gate=_non_runnable_gate())
    rendered = check_explainer(explainer, project_root=repo_root)[0].render()
    assert rendered.startswith("0:0:")
    assert "EAWF019" in rendered
