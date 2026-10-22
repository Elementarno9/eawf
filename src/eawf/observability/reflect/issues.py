"""The highlighted issues of a reflection report, each typed and backed by its Runs.

An issue names the entity it is about and the Run rows that show it, so a reader can
check every issue against the sessions listed beside it instead of trusting prose.
Only the listed sessions are read: an excluded session is out of the baseline, so it
raises no issue either.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.state.epoch2.base import RunKey
from eawf.kernel.state.epoch2.run import RunStatus, TaskScope
from eawf.kernel.state.epoch2.urns import BatchUrn, MilestoneUrn, RunUrn, TaskUrn
from eawf.observability.reflect.runs import RunReading
from eawf.observability.reflect.totals import RunSpend

logger = logging.getLogger(__name__)


class IssueKind(StrEnum):
    """What a highlighted issue reports."""

    RUN_FAILED = "run_failed"
    UNPRICED_SPEND = "unpriced_spend"
    TASK_REWORKED = "task_reworked"


class HighlightedIssue(BaseModel):
    """One issue the report highlights.

    Attributes:
        kind: What the issue reports.
        subject: The Run, Task, Batch or Milestone the issue is about.
        evidence_rows: The listed sessions that show it, by Run key.
        quotable: Whether every evidence row is the tree's own project's.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: IssueKind
    subject: RunUrn | TaskUrn | BatchUrn | MilestoneUrn
    evidence_rows: Annotated[tuple[RunKey, ...], Field(min_length=1)]
    quotable: bool


def _run_issue(kind: IssueKind, reading: RunReading) -> HighlightedIssue:
    """Return an issue about one Run, evidenced by its own row."""
    return HighlightedIssue(
        kind=kind,
        subject=reading.run.urn,
        evidence_rows=(reading.run.key,),
        quotable=reading.own_project,
    )


def highlight_issues(
    readings: Sequence[RunReading], spends: Sequence[RunSpend]
) -> tuple[HighlightedIssue, ...]:
    """Return the issues the listed sessions show.

    A failed Run is an issue; so is a Run that reported tokens no reading priced,
    because its spend is missing from every total; and so is a Task executed by more
    than one listed Run, because the later Runs redid its work.

    Args:
        readings: The listed sessions, in Run key order.
        spends: One spend per reading, in the same order.

    Returns:
        The issues: the Run issues in Run key order, then the Task issues in Task
        key order.
    """
    issues: list[HighlightedIssue] = []
    task_refs: dict[str, QualifiedUrn] = {}
    task_runs: dict[str, list[RunReading]] = {}
    for reading, spend in zip(readings, spends, strict=True):
        if reading.run.status is RunStatus.FAILED:
            issues.append(_run_issue(IssueKind.RUN_FAILED, reading))
        tokens = (spend.input_tokens, spend.output_tokens, spend.cache_tokens)
        if spend.cost_microusd is None and any(value is not None for value in tokens):
            issues.append(_run_issue(IssueKind.UNPRICED_SPEND, reading))
        scope = reading.run.scope
        if isinstance(scope, TaskScope):
            task_refs[scope.task_ref.entity_key] = scope.task_ref
            task_runs.setdefault(scope.task_ref.entity_key, []).append(reading)
    issues.extend(
        HighlightedIssue(
            kind=IssueKind.TASK_REWORKED,
            subject=task_refs[key],
            evidence_rows=tuple(item.run.key for item in runs),
            quotable=all(item.own_project for item in runs),
        )
        for key, runs in sorted(task_runs.items())
        if len(runs) > 1
    )
    logger.debug(f"highlight_issues sessions={len(readings)} issues={len(issues)}")
    return tuple(issues)


__all__ = ["HighlightedIssue", "IssueKind", "highlight_issues"]
