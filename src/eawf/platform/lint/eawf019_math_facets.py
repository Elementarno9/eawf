"""EAWF019 — facet-presence + citation-resolution lint for math-explainer docs.

The structure / binding / render layer of the verification-grounding contract
(the math-explainer doc-type). A :class:`~eawf.kernel.spec.math.MathExplainer`
is constructible only when every :class:`~eawf.kernel.spec.math.MathClaim`
carries all four facets (intuition, runnable example gate, regime, citation) —
Pydantic enforces that at ingestion. This lint is the *promotion-time*
structural backstop that catches the failure modes ingestion cannot: a facet
present-but-cosmetic, a citation that points at nothing, a runnable example the
test runner would silently skip, and a malformed formula.

The four checks (one owner each, no double-enforcement with the gate-runner or
the L3 judge):

1. **Facet presence** — every claim's example gate is *actually runnable* (a
   ``command_exit_zero`` gate with a non-empty argv), not a cosmetic pin. The
   other three facets are guaranteed present by the model's required fields;
   the runnable-example facet is the one that can be present-but-dead, so it is
   the one this check verifies (reusing :meth:`MathClaim.gate_is_runnable`).
2. **Citation resolution** — each claim's ``citation`` (an ``EvidenceRef``)
   resolves through the canonical EviBound resolver
   (:func:`eawf.workflow.evidence.resolve.resolve`): a URN parses, a
   repo-relative path exists on disk, an external URL is portable. A citation
   that resolves to no row is flagged.
3. **Collected gate** — a claim whose example gate shells ``pytest`` against a
   ``.py`` file must name a file the test runner will actually *collect*. This
   is the kappa failure mode: a ``test_*.py`` example renamed (or authored)
   such that it does not match the configured ``python_files`` patterns
   collects zero tests and passes silently. The check parses the gate argv and
   flags a pytest target whose basename matches no ``python_files`` glob.
4. **Formula well-formedness** — the claim ``statement`` (and inline math in
   the ``intuition``) has balanced ``$``/``$$`` math delimiters and balanced
   braces inside each math span. A malformed formula is flagged. Deep KaTeX /
   LaTeX semantics are out of scope — the check guards delimiter/brace balance
   only, deferring runnable-correctness to the gate-runner.

What this layer does NOT do: run the math (the gate-runner owns correctness)
or read meaning (the L3 judge owns entailment). A passing EAWF019 guarantees a
claim *has* a runnable verifier of the right type and a resolving citation, not
that the math holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.workflow.evidence.resolve import ResolveStatus, resolve

if TYPE_CHECKING:
    from eawf.kernel.spec.math import MathClaim, MathExplainer

RULE_CODE = "EAWF019"

# Default pytest collection globs. ``[tool.pytest.ini_options]`` in this repo
# sets no ``python_files`` override, so pytest's built-in patterns apply: a
# file is collected only when its basename matches one of these. The kappa
# failure mode is an example file (e.g. ``explainer_snippets.py``) that matches
# neither and is therefore silently skipped.
DEFAULT_PYTHON_FILES: tuple[str, ...] = ("test_*.py", "*_test.py")

# Math-delimiter / brace pairs the well-formedness check balances.
_BRACE_OPEN = "{"
_BRACE_CLOSE = "}"


@dataclass(frozen=True)
class MathFacetViolation:
    """One EAWF019 finding against a math-explainer claim.

    Attributes:
        claim_index: Zero-based position of the offending claim in the
            explainer's ``claims`` list (the render carrier so an operator can
            locate the claim without a physical line number — a typed claim has
            no source line of its own).
        col_offset: Always ``0`` — kept for shape-parity with the source-line
            EAWF lints (EAWF013 / EAWF015) whose ``render`` is ``line:col:``.
        snippet: The offending text (the claim statement, citation ref, or gate
            target), truncated for a one-line render.
        reason: Human-readable explanation of which check failed.
    """

    claim_index: int
    col_offset: int
    snippet: str
    reason: str

    @property
    def code(self) -> str:
        """Return the rule code."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``claim:col: CODE reason: snippet`` one-liner body.

        The leading field is the zero-based claim index rather than a source
        line (a typed claim carries no line of its own), keeping the
        ``<n>:<col>: CODE ...`` shape the static-lint envelope renders for the
        source-line lints.
        """
        return f"{self.claim_index}:{self.col_offset}: {RULE_CODE} {self.reason}: {self.snippet!r}"


def _facet_violation(claim_index: int, snippet: str, reason: str) -> MathFacetViolation:
    """Build a :class:`MathFacetViolation` with the truncated snippet."""
    return MathFacetViolation(
        claim_index=claim_index,
        col_offset=0,
        snippet=snippet[:100],
        reason=reason,
    )


def _check_facet_presence(claim: MathClaim, index: int) -> MathFacetViolation | None:
    """Flag a claim whose runnable-example facet is present-but-dead.

    Reuses :meth:`MathClaim.gate_is_runnable`: a gate that is not a
    ``command_exit_zero`` with a non-empty argv is a cosmetic pin, so the
    runnable-example facet (facet (b)) is effectively missing even though the
    model required the field. Facets (a) intuition, (c) regime, and (d)
    citation are guaranteed non-empty by the model's required fields, so only
    the runnable-example facet can be present-but-dead.
    """
    if claim.gate_is_runnable():
        return None
    return _facet_violation(
        index,
        f"gate kind={claim.example_gate.kind!r}",
        "claim is missing the runnable-example facet (gate is not a runnable "
        "command_exit_zero with a non-empty argv)",
    )


def _url_citation_error(claim_index: int, ref: str) -> MathFacetViolation | None:
    """Resolve an ``external_url`` citation structurally via portability.

    The deterministic resolver routes a non-URN, non-marker ref to the
    disk-exists check, which a ``http(s)`` URL always fails (there is no such
    file under the project root). A URL citation instead resolves at this
    structural layer when it is *portable* (a non-local ``http(s)`` scheme):
    deep liveness (does the URL 200) is a network check the structural lint
    does not own, exactly as URN-record dereference is deferred. Reuses
    :meth:`Citation._ref_must_be_portable` so the local-host / ``file:`` /
    absolute-path rejection stays the single authoritative copy.
    """
    from urllib.parse import urlsplit

    from eawf.platform.artifacts.references import Citation

    try:
        Citation._ref_must_be_portable(ref)
    except ValueError as exc:
        return _facet_violation(claim_index, ref, f"citation URL is not portable ({exc})")
    if urlsplit(ref).scheme not in {"http", "https"}:
        return _facet_violation(claim_index, ref, "external_url citation is not an http(s) URL")
    return None


def _check_citation_resolution(
    claim: MathClaim, index: int, *, project_root: Path
) -> MathFacetViolation | None:
    """Flag a claim whose citation resolves to no reference row.

    Branches on the citation's :data:`~eawf.kernel.spec.common.EvidenceKind`:

    * ``external_url`` — resolves via portability only (a non-local ``http(s)``
      URL is structurally resolved; liveness is a deferred network check, like
      URN-record dereference). See :func:`_url_citation_error`.
    * everything else (``audit`` / ``artifact`` / ``decision`` /
      ``store_record``) — routes the ``ref`` through the canonical EviBound
      resolver :func:`eawf.workflow.evidence.resolve.resolve` with the
      ``deterministic`` flavor (a URN parses, a repo-relative path exists). A
      ``RESOLVED`` status passes (its ``deferred_aspects`` are shape-only
      follow-ons owned by a later rung, not a failure); ``UNRESOLVED`` is the
      finding.
    """
    ref = claim.citation.ref
    if not ref.strip():
        return _facet_violation(
            index,
            ref,
            "citation ref is empty (resolves to no reference row)",
        )
    if claim.citation.kind == "external_url":
        return _url_citation_error(index, ref)
    result = resolve(ref, "deterministic", project_root=project_root)
    if result.status is ResolveStatus.UNRESOLVED:
        return _facet_violation(
            index,
            ref,
            f"citation resolves to no reference row ({result.reason})",
        )
    return None


def _pytest_targets(argv: list[str]) -> list[str]:
    """Return the ``.py`` file targets a pytest gate argv would collect.

    Recognises a pytest invocation anywhere in the argv (``uv run pytest ...``,
    ``python -m pytest ...``, a bare ``pytest ...``) and returns the positional
    ``.py`` path arguments after the ``pytest`` token. A path bearing a
    ``::node`` selector is reduced to its file portion. Returns ``[]`` when the
    argv is not a pytest invocation (the collected-gate check then does not
    apply — a CAS / SMT / units verifier is not pytest-collected).
    """
    lowered = [token.lower() for token in argv]
    if "pytest" not in lowered and "py.test" not in lowered:
        return []
    pytest_at = next(i for i, t in enumerate(lowered) if t in {"pytest", "py.test"})
    targets: list[str] = []
    for token in argv[pytest_at + 1 :]:
        if token.startswith("-"):
            continue
        path = token.split("::", 1)[0]
        if path.endswith(".py"):
            targets.append(path)
    return targets


def _matches_python_files(basename: str, patterns: tuple[str, ...]) -> bool:
    """Return whether *basename* matches any pytest ``python_files`` glob."""
    return any(Path(basename).match(pattern) for pattern in patterns)


def _check_collected_gate(
    claim: MathClaim, index: int, *, python_files: tuple[str, ...]
) -> MathFacetViolation | None:
    """Flag a claim whose pytest example gate would collect zero tests.

    The kappa regression: a ``test_*.py`` snippet file renamed (or authored)
    so its basename matches none of the configured ``python_files`` globs is
    silently skipped by pytest — it collects zero tests and the gate passes
    without running anything. The check parses the gate argv (only when the
    gate is runnable), finds the ``.py`` pytest targets, and flags any whose
    basename matches no ``python_files`` pattern.
    """
    if not claim.gate_is_runnable():
        # Facet-presence already flagged a non-runnable gate; the collected
        # check has no argv to parse.
        return None
    argv = claim.example_gate.args.get("argv")
    if not isinstance(argv, list):  # pragma: no cover - gate_is_runnable guarantees a list
        return None
    targets = _pytest_targets([str(token) for token in argv])
    uncollected = [t for t in targets if not _matches_python_files(Path(t).name, python_files)]
    if not uncollected:
        return None
    return _facet_violation(
        index,
        ", ".join(uncollected),
        "example gate targets a pytest file the runner will not collect "
        f"(basename matches no python_files glob {list(python_files)}); the "
        "gate would silently pass having run zero tests",
    )


def _math_spans(text: str) -> tuple[list[str], str | None]:
    """Split *text* into inline ``$...$`` / ``$$...$$`` math spans.

    Returns ``(spans, error)``. ``error`` is a non-``None`` reason string when
    the ``$`` delimiters are unbalanced (an odd count of single ``$`` after
    pairing off the ``$$`` display delimiters). ``spans`` is the list of
    inner-math substrings (between matched delimiters) for the brace-balance
    check; it is best-effort and empty when ``error`` is set.
    """
    # Pair off $$ display delimiters first, then single $ inline delimiters.
    display_count = text.count("$$")
    single_count = text.count("$") - 2 * display_count
    if display_count % 2 != 0:
        return [], "unbalanced $$ display-math delimiters"
    if single_count % 2 != 0:
        return [], "unbalanced $ inline-math delimiters"
    spans: list[str] = []
    remainder = text
    while "$$" in remainder:
        _, _, rest = remainder.partition("$$")
        inner, _, after = rest.partition("$$")
        spans.append(inner)
        remainder = after
    while "$" in remainder:
        _, _, rest = remainder.partition("$")
        inner, _, after = rest.partition("$")
        spans.append(inner)
        remainder = after
    return spans, None


def _brace_balance_error(span: str) -> str | None:
    """Return a reason string when *span* has unbalanced ``{`` / ``}`` braces.

    A LaTeX math span with a mismatched brace count (or a ``}`` before any
    ``{``) is malformed. Escaped ``\\{`` / ``\\}`` are not LaTeX grouping
    braces and are skipped.
    """
    depth = 0
    index = 0
    while index < len(span):
        char = span[index]
        if char == "\\":
            index += 2
            continue
        if char == _BRACE_OPEN:
            depth += 1
        elif char == _BRACE_CLOSE:
            depth -= 1
            if depth < 0:
                return "unbalanced braces in math span (closing '}' before '{')"
        index += 1
    if depth != 0:
        return "unbalanced braces in math span"
    return None


def _check_formula_wellformed(claim: MathClaim, index: int) -> MathFacetViolation | None:
    """Flag a claim whose statement / intuition has malformed math markup.

    Guards delimiter balance (``$`` / ``$$``) and brace balance inside each
    math span across the ``statement`` and ``intuition`` text. Deep KaTeX
    semantics (does ``\\frac`` have two args, is ``\\alpha`` a real command)
    are out of scope — the gate-runner / judge own correctness.
    """
    for field_name, text in (("statement", claim.statement), ("intuition", claim.intuition)):
        spans, delimiter_error = _math_spans(text)
        if delimiter_error is not None:
            return _facet_violation(
                index, text, f"malformed math in {field_name}: {delimiter_error}"
            )
        for span in spans:
            brace_error = _brace_balance_error(span)
            if brace_error is not None:
                return _facet_violation(
                    index, span, f"malformed math in {field_name}: {brace_error}"
                )
    return None


def check_explainer(
    explainer: MathExplainer,
    *,
    project_root: Path,
    python_files: tuple[str, ...] = DEFAULT_PYTHON_FILES,
) -> list[MathFacetViolation]:
    """Return EAWF019 violations for a typed math-explainer.

    Runs the four structural checks over every claim in source order:
    facet-presence (runnable example), citation-resolution (against the
    canonical EviBound resolver), collected-gate (the kappa silent-skip
    regression), and formula well-formedness (delimiter / brace balance). At
    most one violation per claim per check is returned; a claim that fails
    several checks yields one finding per failing check.

    Args:
        explainer: The typed :class:`~eawf.kernel.spec.math.MathExplainer`
            (already constructed, so all four facets are present as fields).
        project_root: Absolute path the citation disk-exists check resolves
            repo-relative refs against.
        python_files: The pytest ``python_files`` collection globs the
            collected-gate check tests example targets against. Defaults to
            pytest's built-in :data:`DEFAULT_PYTHON_FILES`.

    Returns:
        Violations in claim order; empty for a clean explainer.
    """
    violations: list[MathFacetViolation] = []
    for index, claim in enumerate(explainer.claims):
        for finding in (
            _check_facet_presence(claim, index),
            _check_citation_resolution(claim, index, project_root=project_root),
            _check_collected_gate(claim, index, python_files=python_files),
            _check_formula_wellformed(claim, index),
        ):
            if finding is not None:
                violations.append(finding)
    return violations


__all__ = [
    "DEFAULT_PYTHON_FILES",
    "RULE_CODE",
    "MathFacetViolation",
    "check_explainer",
]
