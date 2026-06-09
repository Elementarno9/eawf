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

from eawf.platform.lint.eawf022_propose_coverage import (
    CoverageGapViolation,
    check_coverage,
    check_source_brief_coverage,
)
from eawf.workflow.propose.generator import extract_units

if TYPE_CHECKING:
    from pathlib import Path

    from eawf.kernel.spec.common import CriterionSpec, DeferredDeliverable
    from eawf.kernel.spec.intent import IntentBrief

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


def source_brief_coverage_gaps(
    criteria: list[CriterionSpec],
    *,
    intent: IntentBrief,
    repo_root: Path,
    deferrals: list[DeferredDeliverable] | None = None,
) -> list[CoverageGapViolation]:
    """Return every uncovered unit of a wave's source-brief document(s).

    Closes the boundary the per-wave :func:`coverage_gaps` diff cannot see: a
    source-brief deliverable the planner never wrote a ``planned_steps`` entry
    for. The units come from the referenced source-brief document itself (via
    :func:`~eawf.kernel.spec.intent.source_brief_units`), and a unit is COVERED
    when it shares a significant token (see :func:`significant_tokens`) with at
    least one criterion (``text`` or ``measurable_signal``), planned step, or
    explicit ``backlog_ids`` token. A unit covered by none and named by no
    :class:`~eawf.kernel.spec.common.DeferredDeliverable` is a finding.

    Unlike :func:`coverage_gaps`, an empty ``planned_steps`` is NOT a clean
    no-op for a required-intent brief: the source-brief document still
    enumerates deliverables, so the diff runs against the brief even when the
    planner authored no steps. When the brief is not required-intent (no
    ``source_brief_ids``) the gate is a clean no-op -- there is no source-brief
    document to diff against.

    The cover-set is decided by deterministic token overlap, never by an LLM's
    self-report, so the diff is reproducible over identical input.

    Args:
        criteria: The wave's authored criterion rows.
        intent: The wave's :class:`~eawf.kernel.spec.intent.IntentBrief`; its
            ``source_brief_ids`` documents are the unit source.
        repo_root: The repo working-tree root the ``source_brief_ids`` paths
            resolve under.
        deferrals: Explicit deferral rows suppressing named source-brief
            units. ``None`` (the default) means no unit is deferred.

    Returns:
        One finding per uncovered source-brief unit; empty when every unit is
        covered, deferred, or the brief names no resolvable source document.
    """
    from eawf.kernel.spec.intent import source_brief_units

    if not intent.is_required_intent:
        return []
    units = source_brief_units(intent, repo_root=repo_root)
    if not units:
        return []
    target_tokens = [
        significant_tokens(f"{criterion.text} {criterion.measurable_signal}")
        for criterion in criteria
    ]
    target_tokens += [significant_tokens(step) for step in intent.planned_steps]
    cover_set = {
        unit.span_id
        for unit in units
        if (unit_tokens := significant_tokens(unit.quote))
        and any(unit_tokens & ttokens for ttokens in target_tokens)
    }
    return check_source_brief_coverage(
        units,
        covered_span_ids=cover_set,
        deferrals=list(deferrals) if deferrals is not None else [],
    )


__all__ = [
    "coverage_gaps",
    "significant_tokens",
    "source_brief_coverage_gaps",
]
