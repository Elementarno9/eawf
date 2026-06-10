"""Per-attempt telemetry-timeline projection for the wave-detail ``evidence`` tab.

A wave's :class:`~eawf.workflow.agent_report.rollup.PerWaveAttemptRollup`
carries one row per dispatch attempt (runtime, start / end stamps, exit
status, retry + blocked counts, token total). This module projects that
rollup into the reused detail-card ``(label, value)`` row shape: a compact
attempt / retry / blocked / token summary, an error-kind breakdown, and an
aligned columnar per-attempt timeline table. Every figure is a pure function
of the rollup so the projection is unit-testable without mounting Textual; the
detail modal stays a thin view over it.
"""

from __future__ import annotations

from eawf.workflow.agent_report.rollup import PerWaveAttemptRollup


def attempt_rollup_rows(rollup: PerWaveAttemptRollup) -> tuple[tuple[str, str], ...]:
    """Build evidence-tab rows for a wave's per-attempt timeline.

    Args:
        rollup: The per-wave attempt rollup.

    Returns:
        Ordered ``(label, value)`` rows: the attempt summary, the error-kind
        breakdown, and the aligned per-attempt timeline table.
    """
    return (
        ("attempts", _attempt_summary(rollup)),
        ("error kinds", _error_kind_breakdown(rollup)),
        ("attempt timeline", _attempt_timeline_table(rollup)),
    )


def _attempt_summary(rollup: PerWaveAttemptRollup) -> str:
    """Return compact attempt/retry/blocked/token summary text."""
    return (
        f"{_count_label(rollup.attempt_count, 'attempt')}, "
        f"{_count_label(rollup.retry_count, 'retry', plural='retries')}, "
        f"{rollup.blocked_count} blocked, "
        f"{_count_label(rollup.token_total, 'token')}"
    )


def _count_label(count: int, singular: str, *, plural: str | None = None) -> str:
    """Return a count plus singular/plural noun."""
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {noun}"


def _error_kind_breakdown(rollup: PerWaveAttemptRollup) -> str:
    """Return error-kind breakdown text for the rollup."""
    if not rollup.error_kind_breakdown:
        return "none"
    return ", ".join(f"{kind}={count}" for kind, count in rollup.error_kind_breakdown.items())


def _attempt_timeline_table(rollup: PerWaveAttemptRollup) -> str:
    """Render the 8-column per-attempt timeline table."""
    if not rollup.attempts:
        return "no attempts recorded"
    columns = ("att", "runtime", "started", "ended", "exit", "retry", "blocked", "tokens")
    raw_rows = [
        (
            str(row.attempt),
            row.runtime,
            row.started,
            row.ended,
            row.exit_status,
            row.retry,
            row.blocked,
            row.tokens,
        )
        for row in rollup.attempts
    ]
    widths = [len(value) for value in columns]
    for raw_row in raw_rows:
        widths = [max(width, len(value)) for width, value in zip(widths, raw_row, strict=True)]
    rendered = [_format_attempt_table_row(columns, widths)]
    rendered.extend(_format_attempt_table_row(row, widths) for row in raw_rows)
    return "\n" + "\n".join(f"  {line}" for line in rendered)


def _format_attempt_table_row(row: tuple[str, ...], widths: list[int]) -> str:
    """Format one attempt-table row with padded columns."""
    cells = [value.ljust(width) for value, width in zip(row, widths, strict=True)]
    return "  ".join(cells)


__all__ = ["attempt_rollup_rows"]
