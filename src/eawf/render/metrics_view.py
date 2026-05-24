"""Shared rich-table renderer for the ``eawf metrics`` payload.

Three consumers feed off the same :func:`render_metrics_table` and
:func:`render_metrics_plain` helpers:

1. The CLI handler in :mod:`eawf.cli.commands.metrics` (today).
2. The TUI overlay (P20 W04 brief — pending wave).
3. The release-notes renderer in :mod:`eawf.render.release_notes`
   (P20 W13 brief — pending wave).

Keeping the renderer pure (input: :class:`MetricsSummary`; output: ``str``)
means the three callers stay byte-stable: the same metrics payload yields
the same table regardless of where it is displayed. Tests assert this by
snapshotting the rendered string.

``--plain`` callers (terminals without ANSI support) bypass Rich entirely
via :func:`render_metrics_plain`. The plain branch must remain visually
parseable so a release-notes paste preserves the table semantics even when
dropped into a markdown viewer that ignores ANSI escapes.
"""

from __future__ import annotations

import io

from rich.console import Console
from rich.table import Table

from eawf.workflow.estimation.metrics import MetricsSummary

# Header strings are module-level so tests can grep for them without
# re-deriving the literal. Order matches the four-row CLI table.
_METRIC_LABEL_EU_VARIANCE: str = "EU variance"
_METRIC_LABEL_AUDIT_PASS: str = "Audit pass rate"
_METRIC_LABEL_WAVE_ELAPSED: str = "Wave elapsed (min)"
_METRIC_LABEL_PLANNED_REACTIVE: str = "Planned vs reactive"


def _format_eu_variance_value(summary: MetricsSummary) -> str:
    """Format the EU-variance row's value column.

    Empty-sample state renders as ``"n/a"`` so the operator can tell
    the difference between "zero variance" (calibration centred) and
    "no closed waves yet".
    """
    m = summary.eu_variance
    if m.sample_count == 0:
        return "n/a"
    return (
        f"n={m.sample_count} mean_delta={m.mean_delta_eu:+.2f} EU "
        f"stdev={m.stdev_delta_eu:.2f} inside_pess={m.inside_pessimistic_share:.0%}"
    )


def _format_audit_pass_value(summary: MetricsSummary) -> str:
    """Format the audit-pass row's value column."""
    m = summary.audit_pass_rate
    if m.decided_count == 0:
        return "n/a"
    return (
        f"n={m.decided_count} pass={m.pass_count} minor={m.minor_count} "
        f"major={m.major_count} share={m.pass_share:.0%}"
    )


def _format_wave_elapsed_value(summary: MetricsSummary) -> str:
    """Format the wave-elapsed row's value column."""
    m = summary.wave_elapsed
    if m.sample_count == 0:
        return "n/a"
    return (
        f"n={m.sample_count} mean={m.mean_minutes:.1f} "
        f"median={m.median_minutes:.1f} max={m.max_minutes:.1f}"
    )


def _format_planned_reactive_value(summary: MetricsSummary) -> str:
    """Format the planned-vs-reactive row's value column."""
    m = summary.planned_vs_reactive
    total = m.planned_count + m.reactive_count
    if total == 0:
        return "n/a"
    return f"planned={m.planned_count} reactive={m.reactive_count} share={m.reactive_share:.0%}"


def _rows(summary: MetricsSummary) -> list[tuple[str, str]]:
    """Return the four (label, value) rows in canonical render order.

    Centralised so the rich-table branch and the plain-text branch stay
    in lock-step — adding a row in one place would otherwise drift.
    """
    return [
        (_METRIC_LABEL_EU_VARIANCE, _format_eu_variance_value(summary)),
        (_METRIC_LABEL_AUDIT_PASS, _format_audit_pass_value(summary)),
        (_METRIC_LABEL_WAVE_ELAPSED, _format_wave_elapsed_value(summary)),
        (_METRIC_LABEL_PLANNED_REACTIVE, _format_planned_reactive_value(summary)),
    ]


def render_metrics_table(summary: MetricsSummary, *, width: int = 100) -> str:
    """Render the metrics summary as a Rich table (ANSI on TTY, plain off-TTY).

    Args:
        summary: Typed :class:`MetricsSummary` to render.
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
    for label, value in _rows(summary):
        table.add_row(label, value)
    console.print(table)
    return buf.getvalue().rstrip()


def render_metrics_plain(summary: MetricsSummary) -> str:
    """Render the metrics summary as a plain-text table (no ANSI).

    Used when the caller passes ``--plain`` or when the consumer (e.g.
    release-notes generator) needs a markdown-safe paste. The header
    matches the rich branch column order so a side-by-side comparison
    of the two outputs is trivial.
    """
    lines = ["eawf metrics", "metric                 value"]
    for label, value in _rows(summary):
        lines.append(f"{label:<22} {value}")
    return "\n".join(lines)


__all__ = [
    "render_metrics_plain",
    "render_metrics_table",
]
