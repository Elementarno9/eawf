"""Per-attempt telemetry-timeline projection for the wave-detail ``evidence`` tab.

A wave's :class:`~eawf.workflow.agent_report.rollup.PerWaveAttemptRollup`
carries one row per dispatch attempt (runtime, start / end stamps, exit
status, retry + blocked counts, token total). This module projects that
rollup into the reused detail-card ``(label, value)`` row shape: a compact
attempt / retry / blocked / token summary, an error-kind breakdown, and an
aligned columnar per-attempt timeline table. Every figure is a pure function
of the rollup so the projection is unit-testable without mounting Textual; the
detail modal stays a thin view over it.

Two shared renderer conventions ride here so the evidence tab reads like the
cost tab beside it (DRY: one humanizer, one table treatment across the modal):

* every token figure -- the summary tally and the timeline ``tokens`` column
  -- routes through the shared W05 units humanizer
  (:func:`~eawf.surfaces.render.units.format_tokens`) so a large count reads
  compact (``352.1k``) rather than a raw six-digit int; and
* the timeline table rides the cost tab's table treatment: an accent-bold
  header, escaped data rows, and a :class:`~eawf.surfaces.render.link_wrap.PreMarkedText`
  wrapper so the detail overlay's escaping choke point passes the header markup
  through verbatim. Timestamps are compacted so a full attempt row fits the
  modal width without wrapping mid-row.
"""

from __future__ import annotations

from datetime import datetime

from rich.markup import escape

from eawf.surfaces.render.link_wrap import PreMarkedText
from eawf.surfaces.render.units import format_tokens
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
        f"{_token_summary_label(rollup.token_total)}"
    )


def _count_label(count: int, singular: str, *, plural: str | None = None) -> str:
    """Return a count plus singular/plural noun."""
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {noun}"


def _token_summary_label(count: int) -> str:
    """Return the humanized token tally plus its singular/plural noun.

    The token figure rides the shared W05 units humanizer so a large tally
    reads compact (``352.1k tokens``) rather than a raw six-digit int; the
    noun still pluralizes off the raw count so a single token reads
    ``1 token``.
    """
    noun = "token" if count == 1 else "tokens"
    return f"{format_tokens(count)} {noun}"


def _humanize_token_cell(cell: str) -> str:
    """Return the humanized form of a raw token-count cell.

    The rollup stores each attempt's token tally as its raw integer string
    (or the ``-`` absence sentinel when no session priced it). Route the
    numeric form through the shared W05 units humanizer so a large tally reads
    compact (``352.1k``) rather than a raw six-digit int; the sentinel and any
    non-numeric cell pass through untouched.
    """
    return format_tokens(int(cell)) if cell.isdigit() else cell


def _compact_timestamp(cell: str) -> str:
    """Return a compact ``MM-DDTHH:MM:SS`` form of an ISO timestamp cell.

    The rollup stores each attempt's start / end stamp as a full ISO-8601
    string (``2026-05-27T12:00:00+00:00``), which is wide enough that a full
    attempt row overflows the detail modal and soft-wraps mid-row. The year
    and UTC offset carry no signal in a per-attempt timeline whose rows are
    minutes apart, so drop them to keep the row inside the modal width. The
    ``-`` absence sentinel (and any value the rollup could not format as ISO)
    passes through untouched.
    """
    try:
        parsed = datetime.fromisoformat(cell)
    except ValueError:
        return cell
    return parsed.strftime("%m-%dT%H:%M:%S")


def _error_kind_breakdown(rollup: PerWaveAttemptRollup) -> str:
    """Return error-kind breakdown text for the rollup."""
    if not rollup.error_kind_breakdown:
        return "none"
    return ", ".join(f"{kind}={count}" for kind, count in rollup.error_kind_breakdown.items())


def _attempt_timeline_table(rollup: PerWaveAttemptRollup) -> str:
    """Render the 8-column per-attempt timeline table.

    Mirrors the cost tab's table treatment: the column headers ride the
    accent-bold metrics-title convention, the data lines are escaped, and the
    whole block is wrapped in :class:`~eawf.surfaces.render.link_wrap.PreMarkedText`
    so the detail overlay's escaping choke point passes the header markup
    through verbatim. Each attempt's timestamps are compacted and its token
    tally humanized so a full row fits the modal width without wrapping.
    """
    if not rollup.attempts:
        return "no attempts recorded"
    columns = ("att", "runtime", "started", "ended", "exit", "retry", "blocked", "tokens")
    raw_rows = [
        (
            str(row.attempt),
            row.runtime,
            _compact_timestamp(row.started),
            _compact_timestamp(row.ended),
            row.exit_status,
            row.retry,
            row.blocked,
            _humanize_token_cell(row.tokens),
        )
        for row in rollup.attempts
    ]
    widths = [len(value) for value in columns]
    for raw_row in raw_rows:
        widths = [max(width, len(value)) for width, value in zip(widths, raw_row, strict=True)]
    # The header rides the metrics-title accent-bold convention; the value
    # therefore carries its own markup (PreMarkedText opts out of the detail
    # overlay's escaping) with the data lines escaped here instead.
    header = f"[$accent][b]{escape(_format_attempt_table_row(columns, widths))}[/][/]"
    rendered = [header]
    rendered.extend(escape(_format_attempt_table_row(row, widths)) for row in raw_rows)
    return PreMarkedText("\n" + "\n".join(f"  {line}" for line in rendered))


def _format_attempt_table_row(row: tuple[str, ...], widths: list[int]) -> str:
    """Format one attempt-table row with padded columns."""
    cells = [value.ljust(width) for value, width in zip(row, widths, strict=True)]
    return "  ".join(cells)


__all__ = ["attempt_rollup_rows"]
