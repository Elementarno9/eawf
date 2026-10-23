"""Renderers for the ``eawf metrics`` payloads.

:func:`render_metrics_table` and :func:`render_metrics_plain` render the
:class:`~eawf.workflow.estimation.metrics.LedgerMetrics` roll-up of a
generation's estimate and actual ledgers; the refit renderers render what a
re-fit request did. Each is pure (typed input, ``str`` output), so the same
payload yields the same text wherever it is displayed.

``--plain`` callers (terminals without ANSI support) bypass Rich entirely
via :func:`render_metrics_plain`, which stays visually parseable when
pasted into a markdown viewer that ignores ANSI escapes.
"""

from __future__ import annotations

import io

from rich.console import Console
from rich.table import Table

from eawf.workflow.estimation.calibration import RefitDisposition, RefitOutcome
from eawf.workflow.estimation.mapping_revisions import RefitAnswer
from eawf.workflow.estimation.metrics import LedgerMetrics

# Labels are module-level so tests can grep for them without re-deriving
# the literal. Order matches the three-row CLI table.
_METRIC_LABEL_ESTIMATES: str = "Estimates"
_METRIC_LABEL_ACTUALS: str = "Actuals"
_METRIC_LABEL_VARIANCE: str = "Estimate-actual variance"


def _format_variance_value(metrics: LedgerMetrics) -> str:
    """Format the variance row's value column; no sample reads ``n/a``, never ``0%``."""
    m = metrics.variance
    if m.variance_pct is None:
        return f"n/a (samples={m.sample_count})"
    return f"{m.variance_pct:+.1f}% (samples={m.sample_count})"


def _rows(metrics: LedgerMetrics) -> list[tuple[str, str]]:
    """Return the (label, value) rows in canonical render order.

    Centralised so the rich-table branch and the plain-text branch stay
    in lock-step.
    """
    return [
        (_METRIC_LABEL_ESTIMATES, str(metrics.estimate_count)),
        (_METRIC_LABEL_ACTUALS, str(metrics.actual_count)),
        (_METRIC_LABEL_VARIANCE, _format_variance_value(metrics)),
    ]


def render_metrics_table(metrics: LedgerMetrics, *, width: int = 100) -> str:
    """Render the ledger roll-up as a Rich table (ANSI on TTY, plain off-TTY).

    Args:
        metrics: The roll-up to render.
        width: Terminal width to size the table to. Defaults to 100 so the
            row text fits in standard terminals and PR-body markdown.

    The returned string is the captured output of a Rich Console with
    ``force_terminal=False``; this keeps tests deterministic and lets
    callers print the string verbatim without Rich re-wrapping it.
    """
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, record=False)
    table = Table(title="eawf metrics", show_lines=False)
    table.add_column("metric", style="bold")
    table.add_column("value")
    for label, value in _rows(metrics):
        table.add_row(label, value)
    console.print(table)
    return buf.getvalue().rstrip()


def render_metrics_plain(metrics: LedgerMetrics) -> str:
    """Render the ledger roll-up as a plain-text table (no ANSI).

    Used when the caller passes ``--plain``. The header matches the rich
    branch column order so a side-by-side comparison is trivial.
    """
    lines = ["eawf metrics", "metric                    value"]
    for label, value in _rows(metrics):
        lines.append(f"{label:<25} {value}")
    return "\n".join(lines)


def render_refit(outcome: RefitOutcome) -> str:
    """Render an effort-mapping re-fit outcome as plain lines.

    The first line names the disposition and the revision the re-fit started
    from; the exclusions are always listed, so no fit reads as resting on an
    unstated sample.
    """
    lines = [
        f"effort mapping re-fit: {outcome.disposition.value} "
        f"(revision {outcome.current_revision}, {outcome.current_digest})",
        f"eligible actuals: {outcome.eligible_count}",
    ]
    excluded = ", ".join(f"{row.reason.value}={row.count}" for row in outcome.excluded)
    lines.append(f"excluded: {excluded or 'none'}")
    if outcome.disposition is RefitDisposition.NOT_DUE:
        lines.append(f"not due: {', '.join(reason.value for reason in outcome.not_due)}")
    elif outcome.disposition is RefitDisposition.APPLY:
        lines.append(f"notice: {outcome.notice}")
    else:
        lines.append(f"operator decision: {outcome.question}")
        lines.extend(f"  {option.option_id}: {option.label}" for option in outcome.options)
    return "\n".join(lines)


def render_refit_answer(answer: RefitAnswer) -> str:
    """Render what one re-fit request did, then the re-fit it computed.

    The first line names the result and the revision in force afterwards.
    """
    lines = [f"effort mapping: {answer.result.value}, revision {answer.revision} in force"]
    if answer.notice is not None:
        lines.append(f"notice: {answer.notice}")
    if answer.action_ref is not None:
        lines.append(f"operator decision: {answer.action_ref}")
    if answer.outcome is not None:
        lines.append(render_refit(answer.outcome))
    return "\n".join(lines)


__all__ = [
    "render_metrics_plain",
    "render_metrics_table",
    "render_refit",
    "render_refit_answer",
]
