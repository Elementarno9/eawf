"""Shared EAWF022 coverage-lint helper: criteria vs a wave's planned steps.

The coverage lint diffs a wave's authored success criteria against the
``planned_steps`` of its :class:`~eawf.kernel.spec.intent.IntentBrief`: every
planned step is a brief span the criteria must topically account for, and a
span no criterion covers is a silently-dropped planned step (the TUI
brief-criteria-drift incident). The cover-set is decided by deterministic
significant-token overlap, never by an LLM self-report, so the diff is
reproducible over identical input.

This module is the single home for that diff so both the daemon ``spec.sync``
path and the ``/roadmap propose`` render call one implementation. The daemon
rejects a coverage gap at sync time; the propose render surfaces it as an
advisory finding and the apply render refuses on it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from eawf.platform.lint.eawf022_propose_coverage import CoverageGapViolation, check_coverage
from eawf.workflow.propose.generator import extract_units

if TYPE_CHECKING:
    from eawf.kernel.spec.common import CriterionSpec, DeferredDeliverable

#: Minimum length for a "significant" token in the coverage overlap check.
#: Tokens shorter than this (``the``, ``a``, ``to``, ``via``, ``and``, ...)
#: carry no topical signal, so they are dropped before matching a criterion
#: against a planned step -- otherwise every step would trivially "match" any
#: criterion through a shared article.
_COVERAGE_TOKEN_MIN_LEN: Final[int] = 4

#: Word-token pattern for the coverage overlap check (alphanumeric runs).
_COVERAGE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9]+")


def significant_tokens(text: str) -> set[str]:
    """Return the lowercased significant tokens of *text*.

    A significant token is an alphanumeric run of at least
    :data:`_COVERAGE_TOKEN_MIN_LEN` characters; short connective words are
    dropped so the coverage overlap check keys on topical content, not shared
    articles.

    Args:
        text: The source string to tokenise.

    Returns:
        The set of lowercased significant tokens.
    """
    return {
        token.lower()
        for token in _COVERAGE_TOKEN_RE.findall(text)
        if len(token) >= _COVERAGE_TOKEN_MIN_LEN
    }


def coverage_gaps(
    criteria: list[CriterionSpec],
    *,
    planned_steps: list[str],
) -> list[CoverageGapViolation]:
    """Return every EAWF022 coverage gap of *planned_steps* by *criteria*.

    Each planned step is extracted into a
    :class:`~eawf.kernel.spec.common.SourceUnit` via
    :func:`~eawf.workflow.propose.generator.extract_units`, and a span is
    COVERED when at least one criterion's ``text`` or ``measurable_signal``
    shares a significant token (see :func:`significant_tokens`) with the step.
    A span no criterion topically addresses is a finding so a silently-dropped
    planned step surfaces.

    The cover-set is decided by deterministic token overlap, never by an LLM's
    self-report: the diff is reproducible over identical input. A wave with no
    planned steps has nothing to cover, so the lint is a clean no-op.

    Args:
        criteria: The wave's authored criterion rows.
        planned_steps: The wave's ``IntentBrief.planned_steps`` (empty when the
            wave carries no intent or no steps).

    Returns:
        One finding per uncovered planned-step span; empty when every step is
        covered.
    """
    if not planned_steps:
        return []
    units = extract_units("\n".join(planned_steps))
    criterion_tokens = [
        significant_tokens(f"{criterion.text} {criterion.measurable_signal}")
        for criterion in criteria
    ]
    cover_set = {
        unit.span_id
        for unit in units
        if (step_tokens := significant_tokens(unit.quote))
        and any(step_tokens & ctokens for ctokens in criterion_tokens)
    }
    deferrals: list[DeferredDeliverable] = []
    return check_coverage(units, covered_span_ids=cover_set, deferrals=deferrals)


__all__ = [
    "coverage_gaps",
    "significant_tokens",
]
