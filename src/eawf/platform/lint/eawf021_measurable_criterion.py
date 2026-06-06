"""EAWF021 -- reject unmeasurable success criteria.

Layer 2 of the doc-clarity enforcement stack and the Fidelity-Spine L0
typed-criteria layer: a success criterion is only useful when something
deterministic can falsify it. Two prose shapes make a criterion
unmeasurable and trip this rule:

- **banned-vague token** -- a word from the closed vague set
  ({``works``, ``robust``, ``gracefully``, ``properly``, ``as expected``,
  ``is performant``}) carries no observable signal. ``the widget works
  properly`` reads fine but cannot be checked; the rewritten
  ``returns 200 for a valid request; pytest tests/x.py::test_ok`` can.
  The match is case-insensitive and word-boundary anchored, so a 20-char
  vague phrase is caught even though it clears the ``measurable_signal``
  length floor -- the length floor alone is insufficient.
- **missing observation contract** -- a criterion whose typed
  :class:`~eawf.kernel.spec.common.ResponseClause` is ``None`` AND whose
  text carries no parseable observation verb plus proof locus has no way
  to be observed at all. The verb / locus vocabularies mirror
  :class:`~eawf.kernel.spec.common.ObserveVerb` and
  :class:`~eawf.kernel.spec.common.ProofLocus` so the prose check accepts
  exactly the surface forms the typed clause encodes.

The module exposes two surfaces:

- :func:`check_criterion` -- the typed entry the Fidelity-Spine criteria
  gate calls with a :class:`CriterionSpec` (or a text + optional
  response pair). It is the primary surface; the typed response clause
  short-circuits the missing-contract leg.
- :func:`check_source` -- the prose-string adapter the
  :func:`~eawf.platform.lint.validate_prose.validate_prose` chokepoint
  composes. It scans Markdown prose for banned-vague tokens only (the
  missing-contract leg needs the typed response, which a raw prose
  surface does not carry), so the composed leg is the vague-token
  backstop over rendered criteria text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eawf.kernel.spec.common import CriterionSpec, ResponseClause

RULE_CODE = "EAWF021"

#: Closed set of vague tokens that make a criterion unmeasurable. Each is a
#: word-boundary, case-insensitive match; the multi-word phrases match as a
#: contiguous run. Sorted longest-first so ``is performant`` and ``as
#: expected`` win over any future single-word overlap.
BANNED_VAGUE_TOKENS: tuple[str, ...] = (
    "as expected",
    "is performant",
    "gracefully",
    "properly",
    "robust",
    "works",
)

# One alternation over the banned tokens, word-boundary anchored at each end so
# ``framework`` does not match ``works`` and ``approperly`` does not match
# ``properly``. ``re.escape`` keeps the literal phrases safe; ``\s+`` is not
# needed because every phrase uses a single space.
_VAGUE = re.compile(
    r"\b(?:" + "|".join(re.escape(token) for token in BANNED_VAGUE_TOKENS) + r")\b",
    re.IGNORECASE,
)

# Observation-verb surface forms, mirroring ObserveVerb. The underscore enum
# values (``holds_for_all``) plus their natural-prose spellings (``holds for
# all``) are both accepted so a criterion authored in prose still parses.
_OBSERVE_VERBS: frozenset[str] = frozenset(
    {
        "returns",
        "raises",
        "holds for all",
        "holds_for_all",
        "exits",
        "emits",
        "validates",
        "matches pattern",
        "matches_pattern",
        "transitions to",
        "transitions_to",
        "renders token",
        "renders_token",
        "triggers action",
        "triggers_action",
        "file matches",
        "file_matches",
        "judged",
    }
)

# Proof-locus surface forms, mirroring ProofLocus.
_PROOF_LOCI: frozenset[str] = frozenset(
    {
        "pytest",
        "hypothesis",
        "cli_exit",
        "cli exit",
        "log_capture",
        "log capture",
        "schema",
        "source",
        "state_json",
        "state json",
        "tui_snapshot",
        "tui snapshot",
        "tui_pilot",
        "tui pilot",
        "golden",
        "human",
        "jury",
    }
)

_VERB_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(v) for v in sorted(_OBSERVE_VERBS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)
_LOCUS_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(loc) for loc in sorted(_PROOF_LOCI, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MeasurabilityViolation:
    """One EAWF021 finding against a success criterion or prose line.

    Attributes:
        lineno: 1-based line the finding anchors to (``1`` for a single
            criterion passed as a one-line string; the real source line for a
            composed prose scan).
        col_offset: 0-based column of the matched vague token; ``0`` for a
            missing-observation-contract finding (block-level).
        snippet: The offending token (the vague word) or a short descriptor
            of the criterion text for a missing-contract finding.
        reason: Lowercase-led, period-free explanation of the rule tripped.
    """

    lineno: int
    col_offset: int
    snippet: str
    reason: str

    @property
    def code(self) -> str:
        """Return the rule code."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``line:col: CODE reason: snippet`` style one-liner body."""
        return f"{self.lineno}:{self.col_offset}: {RULE_CODE} {self.reason}: {self.snippet!r}"


def _has_observation_contract(text: str, response: ResponseClause | None) -> bool:
    """Return whether *text* / *response* carry an observation verb + locus.

    A typed response clause is a complete contract on its own (it always
    pairs an :class:`ObserveVerb` with a :class:`ProofLocus`). When the clause
    is ``None`` the prose must supply both a parseable verb AND a parseable
    proof locus for the criterion to be observable.
    """
    if response is not None:
        return True
    return bool(_VERB_RE.search(text)) and bool(_LOCUS_RE.search(text))


def _vague_violations(text: str, lineno: int) -> list[MeasurabilityViolation]:
    """Return one violation per banned-vague token found in *text*."""
    violations: list[MeasurabilityViolation] = []
    for match in _VAGUE.finditer(text):
        violations.append(
            MeasurabilityViolation(
                lineno=lineno,
                col_offset=match.start(),
                snippet=match.group(0),
                reason="banned vague token; state an observable signal instead",
            )
        )
    return violations


def check_criterion(
    text: str,
    *,
    response: ResponseClause | None = None,
    measurable_signal: str | None = None,
    lineno: int = 1,
) -> list[MeasurabilityViolation]:
    """Return EAWF021 violations for one success criterion.

    A criterion is unmeasurable when it EITHER carries a banned-vague token
    (in its ``text`` or its ``measurable_signal``) OR lacks an observation
    contract: no typed *response* clause and no parseable observation verb
    plus proof locus in the text. The length floor on ``measurable_signal``
    is insufficient on its own -- a 20-char vague phrase clears the floor yet
    still fails here.

    Args:
        text: The criterion's ``text`` field.
        response: The criterion's typed :class:`ResponseClause`, or ``None``.
            A non-``None`` clause satisfies the observation-contract leg.
        measurable_signal: The criterion's ``measurable_signal`` field, also
            scanned for vague tokens. ``None`` skips the signal scan.
        lineno: 1-based line the findings anchor to (defaults to ``1`` for a
            single-criterion call).

    Returns:
        Violations in source order: every vague-token hit in ``text`` then in
        ``measurable_signal``, then a single missing-contract finding when no
        observation verb + locus is present.
    """
    violations = _vague_violations(text, lineno)
    if measurable_signal is not None:
        violations.extend(_vague_violations(measurable_signal, lineno))
    if not _has_observation_contract(text, response):
        violations.append(
            MeasurabilityViolation(
                lineno=lineno,
                col_offset=0,
                snippet=text[:80],
                reason=(
                    "unmeasurable criterion; add a response clause or an observation "
                    "verb plus proof locus"
                ),
            )
        )
    return violations


def check_criterion_spec(criterion: CriterionSpec) -> list[MeasurabilityViolation]:
    """Return EAWF021 violations for a typed :class:`CriterionSpec`.

    Thin adapter over :func:`check_criterion` that reads ``text``,
    ``response``, and ``measurable_signal`` straight off the typed row.

    Args:
        criterion: The typed success-criterion row to inspect.

    Returns:
        Violations in source order (see :func:`check_criterion`).
    """
    return check_criterion(
        criterion.text,
        response=criterion.response,
        measurable_signal=criterion.measurable_signal,
    )


def check_source(source: str) -> list[MeasurabilityViolation]:
    """Return EAWF021 vague-token violations over a Markdown prose surface.

    The prose-string adapter the :func:`~eawf.platform.lint.validate_prose`
    chokepoint composes. Only the banned-vague-token leg runs here: the
    missing-observation-contract leg needs the typed response clause, which a
    raw prose surface does not carry, so it is the :func:`check_criterion`
    surface's responsibility. Fenced code blocks are skipped so a vague word
    inside an example is exempt.

    Args:
        source: Markdown text to inspect.

    Returns:
        One violation per banned-vague token found in running prose, in
        source order.
    """
    violations: list[MeasurabilityViolation] = []
    in_fence = False
    for lineno, raw in enumerate(source.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        violations.extend(_vague_violations(raw, lineno))
    return violations


__all__ = [
    "BANNED_VAGUE_TOKENS",
    "RULE_CODE",
    "MeasurabilityViolation",
    "check_criterion",
    "check_criterion_spec",
    "check_source",
]
