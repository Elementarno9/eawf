"""Doc-clarity standard: the newcomer test, approved glossary, and the
internal-code blocklist that the prose lints consume.

This module is the single, queryable source of truth for *Layer 0* of the
doc-clarity enforcement stack (the ``clarity-contract`` render block in the
always-enabled ``core`` profile renders the same standard as prose). The
deterministic title / prose lints that land in later waves read the typed
data here so the rendered rule text and the machine-checked rule never
drift:

- :data:`NEWCOMER_TEST_DIMENSIONS` — the six clarity dimensions the
  newcomer test scores (audience-fit, jargon-defined-on-first-use,
  why-present, scannable, reference-hygiene, not-a-title-duplicate). Both
  the deterministic lints and the Layer-3 clarity judge anchor on this
  closed set.
- :data:`APPROVED_TERMS` — the glossary every newcomer-facing artifact may
  use without first defining the term. A term on this list is "common
  enough that a competent engineer who joined today already knows it"; an
  internal code (see below) is the opposite and must be glossed on first
  use.
- :data:`INTERNAL_CODE_BLOCKLIST` — the typed patterns for the internal
  codes forbidden in newcomer-facing prose *unless glossed on first use*:
  lifecycle ids (``P<NN>`` / ``I<NN>`` / ``W<NN>``), cluster / decision
  codes (``C0<N>`` / ``D<NN>`` / ``D-SUP-<NN>``), hypothesis ids
  (``H<NN>-<NN>``), and screaming-snake feature flags
  (``SWITCH_*`` / ``EAWF_*``). Commit-subject prefixes are deliberately
  *exempt* (:data:`COMMIT_SUBJECT_PREFIX_EXEMPT`) because the
  commit-prefix rule *requires* them — they are tooling-parsed metadata,
  not prose.

Why a typed module and not loose constants: the lints in W02+ need to
iterate the blocklist, compile each pattern once, and surface a stable
per-code id in a violation message. A frozen Pydantic record per code
gives each pattern an id, a human label, and the compiled matcher in one
place, and ``extra="forbid"`` keeps a typo in a future blocklist row a
``ValidationError`` at import time rather than a silently dead rule.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

#: The single gate the whole doc-clarity standard reduces to. Rendered
#: verbatim in the ``clarity-contract`` core-profile block and quoted by the
#: clarity judge's rubric so the prose and the machine check share one
#: sentence.
NEWCOMER_TEST: str = "would someone who joined today understand this without opening state.json?"


class ClarityDimension(BaseModel):
    """One scored dimension of the newcomer test.

    The deterministic lints (W02+) and the Layer-3 clarity judge both
    anchor on this closed set so a dimension means the same thing whether
    a regex or a model scores it.

    Attributes:
        key: Stable machine id for the dimension (snake_case). Used as the
            criterion-name slot when the judge emits a per-dimension
            verdict, and as the violation-code suffix when a deterministic
            lint flags the dimension.
        label: Short human-readable name rendered in the contract prose.
        blocking_for_description: Whether a failure on this dimension is
            blocking specifically for the *entity-description* surface
            (the worst class per the doc-clarity findings). ``why`` and
            ``not_a_title_duplicate`` are blocking there; the rest are
            advisory on that surface.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Annotated[str, StringConstraints(min_length=1, max_length=48)]
    label: Annotated[str, StringConstraints(min_length=1, max_length=72)]
    blocking_for_description: bool = False


#: The six clarity dimensions the newcomer test scores, in rendered order.
NEWCOMER_TEST_DIMENSIONS: tuple[ClarityDimension, ...] = (
    ClarityDimension(
        key="audience_fit",
        label="Audience-fit: written for a newcomer, not an insider",
    ),
    ClarityDimension(
        key="jargon_defined",
        label="Jargon defined on first use",
    ),
    ClarityDimension(
        key="why_present",
        label="Why-present: the motivation, not just the what",
        blocking_for_description=True,
    ),
    ClarityDimension(
        key="scannable",
        label="Scannable: short paragraphs, headings, lists",
    ),
    ClarityDimension(
        key="reference_hygiene",
        label="Reference-hygiene: dense [N] markers, no inline path/link soup",
    ),
    ClarityDimension(
        key="not_a_title_duplicate",
        label="Description is not a restatement of the title",
        blocking_for_description=True,
    ),
)


#: Glossary of terms a newcomer-facing artifact may use without first
#: defining them. Sorted for diff hygiene; the lints treat membership as
#: "needs no gloss". The set is deliberately the project's own lifecycle
#: vocabulary plus the load-bearing eawf nouns — everything else that looks
#: like an internal code is caught by :data:`INTERNAL_CODE_BLOCKLIST`.
APPROVED_TERMS: tuple[str, ...] = (
    "audit",
    "daemon",
    "decision",
    "dispatch",
    "effort unit",
    "evidence",
    "gate",
    "hypothesis",
    "iter",
    "lint",
    "phase",
    "profile",
    "render block",
    "roadmap",
    "scope",
    "spike",
    "wave",
    "worktree",
)


class InternalCodePattern(BaseModel):
    """One internal-code family forbidden in un-glossed newcomer prose.

    Each row pairs a stable id and a human label with a compiled regex the
    prose lints (W02+) run over a prose surface. A match that is not
    accompanied by a first-use gloss is a finding; the lint owns the
    "is it glossed nearby" decision, this module owns the *recognition* of
    what counts as an internal code.

    Attributes:
        code: Stable machine id for the family (e.g. ``"lifecycle_id"``).
            Surfaces as the violation-code suffix so an operator can
            grep the finding back to the family.
        label: One-line human-readable description of the family.
        pattern: Compiled, word-bounded regex matching one token of the
            family. Compiled once at import so the lint never recompiles
            per-line.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    code: Annotated[str, StringConstraints(min_length=1, max_length=48)]
    label: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    pattern: re.Pattern[str]

    @field_validator("pattern", mode="before")
    @classmethod
    def _compile_pattern(cls, value: object) -> re.Pattern[str]:
        """Compile a raw pattern string; pass an already-compiled pattern through.

        Raises:
            ValueError: when *value* is neither a ``str`` nor a compiled
                :class:`re.Pattern`, or is an un-compilable regex source.
        """
        if isinstance(value, re.Pattern):
            return value
        if isinstance(value, str):
            try:
                return re.compile(value)
            except re.error as exc:
                raise ValueError(f"un-compilable blocklist pattern {value!r}: {exc}") from exc
        raise ValueError(f"blocklist pattern must be str or re.Pattern, got {type(value).__name__}")


#: Internal-code families forbidden in newcomer-facing prose unless glossed
#: on first use. Each pattern matches one whole token (``\b`` word-bounded)
#: so it does not fire mid-identifier. The ``D-SUP-<NN>`` family is listed
#: before the bare ``D<NN>`` decision family so a lint that wants the most
#: specific match can prefer it.
INTERNAL_CODE_BLOCKLIST: tuple[InternalCodePattern, ...] = (
    InternalCodePattern(
        code="lifecycle_id",
        label="lifecycle id (phase P<NN> / iter I<NN> / wave W<NN>)",
        pattern=re.compile(r"\b[PIW]\d{2,}\b"),
    ),
    InternalCodePattern(
        code="cluster_code",
        label="cluster code (C0<N>)",
        pattern=re.compile(r"\bC0\d+\b"),
    ),
    InternalCodePattern(
        code="decision_supersede_code",
        label="supersede-decision code (D-SUP-<NN>)",
        pattern=re.compile(r"\bD-SUP-\d{2,}\b"),
    ),
    InternalCodePattern(
        code="decision_code",
        label="decision code (D<NN>)",
        pattern=re.compile(r"\bD\d{2,}\b"),
    ),
    InternalCodePattern(
        code="hypothesis_id",
        label="hypothesis id (H<NN>-<NN>)",
        pattern=re.compile(r"\bH\d{2,}-\d{2,}\b"),
    ),
    InternalCodePattern(
        code="screaming_snake_flag",
        label="screaming-snake feature flag (SWITCH_* / EAWF_*)",
        pattern=re.compile(r"\b(?:SWITCH|EAWF)_[A-Z0-9_]+\b"),
    ),
)


#: Commit-subject conventional-commit type prefixes. These are EXEMPT from
#: the internal-code blocklist because the commit-prefix rule *requires*
#: them on a commit subject — they are tooling-parsed metadata, not prose.
#: A prose lint that scans a commit body (not the subject) still applies the
#: blocklist; only the subject's leading type token is exempt.
COMMIT_SUBJECT_PREFIX_EXEMPT: frozenset[str] = frozenset(
    {
        "build",
        "chore",
        "ci",
        "docs",
        "feat",
        "fix",
        "perf",
        "refactor",
        "revert",
        "state",
        "test",
    }
)


def is_approved_term(term: str) -> bool:
    """Return whether *term* is an approved glossary term (case-insensitive).

    A glossary term needs no first-use gloss in newcomer-facing prose.

    Args:
        term: The candidate term, matched case-insensitively against
            :data:`APPROVED_TERMS`.

    Returns:
        ``True`` when *term* (case-folded) is in the approved glossary.
    """
    folded = term.casefold()
    return any(folded == t.casefold() for t in APPROVED_TERMS)


def internal_codes_in(text: str) -> list[str]:
    """Return every internal-code token found in *text*, in match order.

    A convenience the W02+ lints layer their gloss-detection on top of: it
    surfaces *what* internal codes appear, leaving the "is each one
    glossed" decision to the caller. Commit-subject prefixes are not
    internal codes, so they never appear here.

    Args:
        text: The prose surface to scan.

    Returns:
        The matched code tokens, in order of appearance. Each match
        position contributes at most one token, attributed to the first
        family in :data:`INTERNAL_CODE_BLOCKLIST` that matches there, so a
        token matched by two overlapping families is not double-counted.
    """
    claimed: set[int] = set()
    found: list[tuple[int, str]] = []
    for entry in INTERNAL_CODE_BLOCKLIST:
        for match in entry.pattern.finditer(text):
            start = match.start()
            if start in claimed:
                continue
            claimed.add(start)
            found.append((start, match.group(0)))
    found.sort(key=lambda pair: pair[0])
    return [token for _, token in found]


__all__ = [
    "APPROVED_TERMS",
    "COMMIT_SUBJECT_PREFIX_EXEMPT",
    "INTERNAL_CODE_BLOCKLIST",
    "NEWCOMER_TEST",
    "NEWCOMER_TEST_DIMENSIONS",
    "ClarityDimension",
    "InternalCodePattern",
    "internal_codes_in",
    "is_approved_term",
]
